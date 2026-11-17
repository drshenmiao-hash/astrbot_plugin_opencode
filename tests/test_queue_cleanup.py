"""A dropped queued request must release its slot while a worker is busy."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opencode_plugin.engine import Engine
from opencode_plugin.errors import OpenCodeError
from opencode_plugin.settings import settings_from


class QueueCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg = settings_from(
            {"model_id": "mock/grok-4.6", "max_concurrency": 1, "queue_capacity": 1}
        )
        self.engine = Engine(cfg)
        self.engine.queue.reorder_window = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.addAsyncCleanup(self.engine.close)

        async def prompt(session_id, text, *, stats):
            self.started.set()
            await self.release.wait()
            return {"text": "好", "wire_bytes": 10}

        self.client = SimpleNamespace(
            closed=False,
            new_session=AsyncMock(return_value="test-session"),
            prompt=AsyncMock(side_effect=prompt),
            close=AsyncMock(),
        )
        self.engine.client = self.client
        self.first = self.submit("active")
        await asyncio.wait_for(self.started.wait(), 1)

    def submit(self, scope):
        task = asyncio.create_task(self.engine.submit([{"role": "user", "content": scope}], scope))
        self.addAsyncCleanup(self.cancel, task)
        return task

    @staticmethod
    async def cancel(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait_for_slot_to_fill(self):
        async with self.engine.queue.condition:
            await asyncio.wait_for(self.engine.queue.condition.wait_for(self.engine.queue.qsize), 1)

    async def assert_replacement_queued(self):
        self.assertEqual(self.engine.queue.qsize(), 0)
        self.assertEqual(self.client.prompt.await_count, 1)
        replacement = self.submit("replacement")
        await self.wait_for_slot_to_fill()
        self.release.set()
        self.assertEqual(await asyncio.wait_for(self.first, 1), "好")
        self.assertEqual(await asyncio.wait_for(replacement, 1), "好")
        self.assertEqual(self.client.prompt.await_count, 2)

    async def test_cancelled_pending_request_releases_its_queue_slot(self):
        queued = self.submit("cancelled")
        await self.wait_for_slot_to_fill()
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        await self.assert_replacement_queued()

    async def test_expired_pending_request_releases_its_queue_slot(self):
        self.engine.cfg["queue_timeout_seconds"] = 0.05
        queued = self.submit("expired")
        await self.wait_for_slot_to_fill()
        with self.assertRaises(OpenCodeError) as caught:
            await queued
        self.assertEqual(caught.exception.code, "queue_timeout")
        self.engine.cfg["queue_timeout_seconds"] = 1
        await self.assert_replacement_queued()

    async def test_close_records_running_and_queued_requests_once(self):
        queued = self.submit("queued")
        await self.wait_for_slot_to_fill()

        async def notify(*_args):
            self.release.set()

        self.client.notify = notify
        with patch("opencode_plugin.engine.record") as record:
            await self.engine.close()
        for task in (self.first, queued):
            with self.assertRaises(OpenCodeError) as error:
                await task
            self.assertEqual(error.exception.code, "closed")
        finished = [
            call.kwargs for call in record.call_args_list if call.args == ("request_finished",)
        ]
        self.assertCountEqual([entry["session"] for entry in finished], ["active", "queued"])
        self.assertTrue(all(entry["code"] == "closed" for entry in finished))
