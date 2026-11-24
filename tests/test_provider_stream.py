"""AstrBot Provider entry points against a controlled ACP stream."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.platform.astr_message_event import current_message_event

from opencode_plugin import runtime
from opencode_plugin.acp import ACPClient
from opencode_plugin.errors import OpenCodeError
from opencode_plugin.provider import OpenCodeCLIProvider
from tests.test_acp import MODEL, Peer


class ProviderStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name in (
            "_engine",
            "_settings",
            "_available_models",
            "_startup_failure",
            "_validation_task",
            "_validation_stopped",
            "_retry_validation_after",
        ):
            self.enterContext(patch.object(runtime, name, None))
        runtime.configure({"model_id": MODEL})
        with patch.object(runtime, "_fetch_available_models", AsyncMock(return_value=[MODEL])):
            await runtime.ensure_model_validated()
        self.provider = OpenCodeCLIProvider({"id": "native", "model": MODEL}, {})
        self.addAsyncCleanup(self.provider.terminate)
        self.engine = self.provider.engine
        self.peers = []

        def owner(_):
            peer = Peer()
            self.peers.append(peer)
            return SimpleNamespace(
                kind="test",
                usage_db=None,
                start=AsyncMock(return_value=(peer.process, {"cwd": "/work"})),
                close=AsyncMock(),
            )

        self.engine.owner_factory = owner
        await self.engine._new_client()
        self.peer = self.peers[0]
        self.assertIsInstance(self.engine.client, ACPClient)
        self.engine.queue.reorder_window = 0

    async def pending(self, sid="1"):
        async with asyncio.timeout(2):
            while sid not in self.peer.prompt_pending:
                await asyncio.sleep(0.001)
        return self.peer.prompt_pending[sid]

    def chunk(self, sid, text):
        self.peer.update(
            sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": text}
        )

    async def begin(self):
        self.peer.replies.append({"wait_for_cancel": True})
        stream = self.provider.text_chat_stream(prompt="hello", conversation_id="chat")
        self.addAsyncCleanup(stream.aclose)
        reading = asyncio.create_task(anext(stream))
        rid = await self.pending()
        self.chunk("1", "")
        self.chunk("1", "first")
        piece = await asyncio.wait_for(reading, 1)
        self.assertTrue(piece.is_chunk)
        self.assertEqual(piece.completion_text, "first")
        return stream, rid

    async def test_deltas_arrive_before_final_and_replay_returns_only_final(self):
        stream, rid = await self.begin()
        self.chunk("1", " second")
        self.peer.send(id=rid, result={"stopReason": "end_turn"})
        remaining = [item async for item in stream]
        self.assertEqual(
            [(x.is_chunk, x.completion_text) for x in remaining],
            [(True, " second"), (False, "first second")],
        )
        cached = [
            x async for x in self.provider.text_chat_stream(prompt="hello", conversation_id="chat")
        ]
        self.assertEqual(
            [(x.is_chunk, x.completion_text) for x in cached], [(False, "first second")]
        )
        self.assertEqual(len(self.peer.prompts), 1)

    async def test_closing_consumer_cancels_prompt_and_discards_session(self):
        stream, _ = await self.begin()
        await stream.aclose()
        async with asyncio.timeout(1):
            while self.engine.active:
                await asyncio.sleep(0.001)
        self.assertEqual(self.peer.cancelled, ["1"])
        self.assertFalse(self.engine.sessions)
        self.assertFalse(self.engine.client.pending)
        self.assertFalse(self.provider._pending)

    async def test_rpc_interruption_after_delta_does_not_repeat_generation(self):
        stream, rid = await self.begin()
        self.peer.send(
            id=rid, error={"code": -32603, "message": "RetriableError: connection stalled"}
        )
        with self.assertRaises(OpenCodeError) as caught:
            await anext(stream)
        self.assertEqual(caught.exception.code, "connection_stalled")
        self.assertEqual(len(self.peer.prompts), 1)
        self.assertFalse(self.engine.sessions)

    async def test_malformed_event_recovers_on_next_request(self):
        await self.check_recovery("protocol")

    async def test_disconnect_recovers_on_next_request(self):
        await self.check_recovery("process_exited")

    async def check_recovery(self, failure):
        self.peer = self.peers[-1]
        stream, _ = await self.begin()
        if failure == "protocol":
            self.peer.send(method="session/update", params={"sessionId": "1", "update": []})
        else:
            self.peer.stdout.feed_eof()
        with self.assertRaises(OpenCodeError) as caught:
            await anext(stream)
        self.assertEqual(caught.exception.code, failure)
        self.assertFalse(self.engine.sessions)
        # A public request leases a fresh peer and sends the host's full history.
        call = asyncio.create_task(
            self.provider.text_chat(
                prompt="after restart",
                conversation_id="chat",
                contexts=[{"role": "user", "content": "old turn"}],
            )
        )
        async with asyncio.timeout(1):
            while self.peers[-1] is self.peer:
                await asyncio.sleep(0)
        self.peer = self.peers[-1]
        self.peer.replies.append({"chunks": ["back"]})
        self.assertEqual((await call).completion_text, "back")
        self.assertIn("old turn", self.peer.prompts[0]["prompt"][0]["text"])

    async def test_tools_and_results_never_become_chat_text(self):
        for args in (
            {"func_tool": object()},
            {"tool_choice": {"type": "function", "function": {"name": "fixture"}}},
            {"tool_calls_result": {"name": "fixture"}},
            {"contexts": [{"role": "tool", "content": "fixture"}]},
        ):
            with self.assertRaises(OpenCodeError) as caught:
                await self.provider.text_chat(prompt="hello", **args)
            self.assertEqual(caught.exception.code, "unsupported_tool_calls")
        for kind in ("tool_call", "tool_call_update"):
            self.peer.replies.append({"wait_for_cancel": True})
            sid = str(self.peer.sessions + 1)
            stream = self.provider.text_chat_stream(prompt="tool")
            self.addAsyncCleanup(stream.aclose)
            reading = asyncio.create_task(anext(stream))
            await self.pending(sid)
            self.peer.update(sid, sessionUpdate=kind)
            self.chunk(sid, "ignored after tool")
            with self.assertRaises(OpenCodeError) as caught:
                await asyncio.wait_for(reading, 1)
            self.assertEqual(caught.exception.code, "tool_blocked")
            self.assertIn(sid, self.peer.cancelled)
        self.assertEqual(len(self.peer.prompts), 2)

    async def test_concurrent_users_and_conversations_get_distinct_sessions(self):
        async def chat(sender, cid):
            event = SimpleNamespace(unified_msg_origin="shared-group", get_sender_id=lambda: sender)
            token = current_message_event.set(event)
            try:
                return await self.provider.text_chat(
                    prompt="same", session_id="group", conversation_id=cid
                )
            finally:
                current_message_event.reset(token)

        self.peer.replies.extend([{"wait_for_cancel": True}] * 2)
        tasks = [asyncio.create_task(chat(user, "same-cid")) for user in ("a", "b")]
        ids = await asyncio.gather(self.pending("1"), self.pending("2"))
        for sid, rid in reversed(list(zip(("1", "2"), ids))):
            self.chunk(sid, sid)
            self.peer.send(id=rid, result={"stopReason": "end_turn"})
        results = await asyncio.gather(*tasks)
        self.assertCountEqual([r.completion_text for r in results], ["1", "2"])
        for user, result in zip(("a", "b"), results):
            self.assertEqual((await chat(user, "same-cid")).completion_text, result.completion_text)
        self.peer.replies.append({"chunks": ["other chat"]})
        self.assertEqual((await chat("a", "other-cid")).completion_text, "other chat")
        self.assertEqual([p["sessionId"] for p in self.peer.prompts], ["1", "2", "3"])
