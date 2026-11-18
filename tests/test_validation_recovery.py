"""Bounded startup recovery and ownership of the disposable ACP catalog probe."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opencode_plugin import runtime
from opencode_plugin.errors import OpenCodeError
from opencode_plugin.models import ModelResolutionError
from tests.test_acp import MODEL


class ValidationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name in (
            "_settings",
            "_engine",
            "_available_models",
            "_startup_failure",
            "_validation_task",
            "_retry_validation_after",
            "_validation_stopped",
        ):
            self.enterContext(patch.object(runtime, name))
            setattr(runtime, name, None)
        self.enterContext(patch.object(runtime, "_STARTUP_BACKOFF", (0, 0)))
        runtime.configure({"model_id": MODEL, "queue_timeout_seconds": 1})
        self.addAsyncCleanup(runtime.stop_validation)

    async def test_transient_startup_is_bounded_then_concurrent_callers_share_one_probe(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_catalog():
            started.set()
            await release.wait()
            return [MODEL]

        fetch = self.enterContext(
            patch.object(
                runtime,
                "_fetch_available_models",
                AsyncMock(side_effect=OpenCodeError("transport")),
            )
        )
        with self.assertRaises(OpenCodeError) as failure:
            await runtime.ensure_model_validated(startup=True)
        self.assertEqual(failure.exception.code, "transport")
        self.assertEqual(fetch.await_count, 3)
        with self.assertRaises(OpenCodeError):
            await runtime.available_model_ids()
        self.assertEqual(fetch.await_count, 3)
        runtime._retry_validation_after = 0
        fetch.side_effect = blocked_catalog
        first = asyncio.create_task(runtime.available_model_ids())
        await started.wait()
        others = [asyncio.create_task(runtime.available_model_ids()) for _ in range(4)]
        await asyncio.sleep(0)
        self.assertEqual(fetch.await_count, 4)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual(await asyncio.gather(*others), [[MODEL]] * 4)
        self.assertIsNone(runtime._startup_failure)
        self.assertEqual(fetch.await_count, 4)

    async def test_auth_and_missing_model_fail_without_retry(self):
        for kind, response in (
            ("not_logged_in", OpenCodeError("not_logged_in")),
            ("model_unavailable", ["other/actual"]),
        ):
            with self.subTest(kind=kind):
                runtime.configure({"model_id": MODEL})
                with patch.object(
                    runtime, "_fetch_available_models", AsyncMock(side_effect=[response])
                ) as fetch:
                    with self.assertRaises(OpenCodeError) as failure:
                        await runtime.ensure_model_validated(startup=True)
                    self.assertEqual(failure.exception.code, kind)
                    if kind == "model_unavailable":
                        self.assertIsInstance(failure.exception, ModelResolutionError)
                        self.assertIn("other/actual", str(failure.exception))
                    runtime._retry_validation_after = 0
                    with self.assertRaises(OpenCodeError) as later:
                        await runtime.available_model_ids()
                    self.assertEqual(later.exception.code, kind)
                    fetch.assert_awaited_once()

    async def test_probe_closes_before_ready_and_never_closes_live_engine(self):
        catalog = AsyncMock(return_value=[MODEL])
        disposable = SimpleNamespace(available_models=catalog, close=AsyncMock())
        live = SimpleNamespace(close=AsyncMock())
        runtime._engine = live
        with patch.object(runtime, "Engine", return_value=disposable):
            await runtime.ensure_model_validated()
        self.assertEqual(await runtime.available_model_ids(), [MODEL])
        disposable.close.assert_awaited_once()
        live.close.assert_not_awaited()

    async def test_stop_waits_for_cancelled_probe_to_close(self):
        entered = asyncio.Event()
        close_started = asyncio.Event()
        release = asyncio.Event()

        async def catalog():
            entered.set()
            await asyncio.Future()

        async def close():
            close_started.set()
            await release.wait()

        disposable = SimpleNamespace(
            available_models=AsyncMock(side_effect=catalog), close=AsyncMock(side_effect=close)
        )
        with patch.object(runtime, "Engine", return_value=disposable):
            waiter = asyncio.create_task(runtime.available_model_ids())
            await entered.wait()
            stop = asyncio.create_task(runtime.stop_validation())
            await close_started.wait()
            self.assertFalse(stop.done())
            release.set()
            await stop
            with self.assertRaises(OpenCodeError) as error:
                await waiter
            self.assertEqual(error.exception.code, "closed")
        disposable.close.assert_awaited_once()

    async def test_probe_cleanup_failure_cannot_announce_validated(self):
        disposable = SimpleNamespace(
            available_models=AsyncMock(return_value=[MODEL]),
            close=AsyncMock(side_effect=OpenCodeError("cleanup_timeout")),
        )
        with patch.object(runtime, "Engine", return_value=disposable):
            with self.assertRaises(OpenCodeError) as error:
                await runtime.ensure_model_validated(startup=True)
        self.assertEqual(error.exception.code, "cleanup_timeout")
        self.assertFalse(runtime.settings()["model"])
        disposable.close.assert_awaited_once()
