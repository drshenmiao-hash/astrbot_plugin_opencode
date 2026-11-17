"""Exercise the actual ACP reader and Engine against an in-memory OpenCode peer."""

import asyncio
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opencode_plugin.acp import ACPClient
from opencode_plugin.engine import Engine
from opencode_plugin.errors import OpenCodeError, retry_stream_errors
from opencode_plugin.launcher import CHAT_MODE, _copy_auth
from opencode_plugin.settings import settings_from

MODEL = "opencode/example"


class Peer:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.process = SimpleNamespace(
            stdin=SimpleNamespace(write=self.write, drain=AsyncMock()),
            stdout=self.stdout,
            stderr=asyncio.StreamReader(),
        )
        self.mode = CHAT_MODE
        self.model = MODEL
        self.models = [MODEL]
        self.replies = deque()
        self.choices = []
        self.prompts = []
        self.sessions = 0
        self.denials = []
        self.cancelled = []

    def send(self, **fields):
        self.stdout.feed_data(json.dumps({"jsonrpc": "2.0", **fields}).encode() + b"\n")

    def options(self):
        return [
            {
                "id": "model",
                "type": "select",
                "currentValue": self.model,
                "options": [{"value": model} for model in self.models],
            },
            {
                "id": "mode",
                "type": "select",
                "currentValue": self.mode,
                "options": [{"value": self.mode}],
            },
        ]

    def update(self, sid, **update):
        self.send(method="session/update", params={"sessionId": sid, "update": update})

    def write(self, frame):
        msg = json.loads(frame)
        method, params = msg.get("method"), msg.get("params", {})
        match method:
            case "initialize":
                result = {"protocolVersion": 1, "authMethods": [{"id": "opencode-login"}]}
            case "session/new":
                assert params == {"cwd": "/work", "mcpServers": []}
                self.sessions += 1
                result = {"sessionId": str(self.sessions), "configOptions": self.options()}
            case "session/set_config_option":
                self.choices.append(params["value"])
                result = {"configOptions": self.options()}
            case "session/prompt":
                self.prompts.append(params)
                sid = params["sessionId"]
                script = self.replies.popleft()
                if "permission" in script:
                    self.send(
                        method="session/request_permission",
                        id="permission",
                        params={
                            "options": [
                                {"kind": "allow_once", "optionId": "yes"},
                                {"kind": "reject_once", "optionId": "no"},
                            ],
                        },
                    )
                if "tool" in script:
                    self.update(sid, sessionUpdate="tool_call")
                if "error" in script:
                    self.send(id=msg["id"], error={"code": -32603, "message": script["error"]})
                    return
                for text in script.get("chunks", []):
                    self.update(
                        sid,
                        sessionUpdate="agent_message_chunk",
                        content={"type": "text", "text": text},
                    )
                result = {"stopReason": "end_turn"}
            case "session/cancel":
                self.cancelled.append(params["sessionId"])
                return
            case None:
                self.denials.append(msg.get("result"))
                return
            case _:
                raise AssertionError(method)
        self.send(id=msg["id"], result=result)


class ACPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = Peer()
        self.client = ACPClient(
            self.peer.process,
            frame_limit=65536,
            output_limit=4096,
            ready={"cwd": "/work"},
        )
        self.engine = Engine(settings_from({"model_id": MODEL}))
        self.engine.client = self.client
        self.addAsyncCleanup(self.engine.close)
        await self.client.start()

    async def submit(self, messages):
        return await asyncio.wait_for(self.engine.submit(messages, "test-scope"), 2)

    async def test_catalog_exact_model_and_two_turn_session_reuse(self):
        self.assertEqual(await self.engine.available_models(), [MODEL])
        self.peer.replies.extend([{"chunks": ["你", "好"]}, {"chunks": ["记得"]}])
        first = [{"role": "user", "content": "第一条"}]
        self.assertEqual(await self.submit(first), "你好")
        second = first + [
            {"role": "assistant", "content": "你好"},
            {"role": "user", "content": "第二条"},
        ]
        self.assertEqual(await self.submit(second), "记得")
        self.assertEqual(self.peer.choices, [MODEL])
        self.assertEqual([p["sessionId"] for p in self.peer.prompts], ["2", "2"])
        sent = self.peer.prompts[-1]["prompt"][0]["text"]
        self.assertIn("第二条", sent)
        self.assertNotIn("第一条", sent)

    async def test_rpc_error_retries_in_fresh_session(self):
        self.peer.replies.extend(
            [
                {"error": "RetriableError: [unavailable] PING timed out"},
                {"chunks": ["好"]},
            ]
        )
        messages = [{"role": "user", "content": "测试"}]
        with self.assertLogs("astrbot", level="WARNING") as logs:
            result = await retry_stream_errors(lambda: self.submit(messages))
        self.assertEqual(result, "好")
        self.assertEqual([p["sessionId"] for p in self.peer.prompts], ["1", "2"])
        self.assertIn("ping_timeout", "\n".join(logs.output))

    async def test_error_text_is_never_returned_as_reply(self):
        self.peer.replies.append({"chunks": ["Error: RetriableError: ", "Connection stalled"]})
        with self.assertRaises(OpenCodeError) as error:
            await self.submit([{"role": "user", "content": "测试"}])
        self.assertEqual(error.exception.code, "connection_stalled")
        self.assertEqual(self.engine.sessions, {})

    async def test_rejected_api_key_is_a_safe_failure_without_retry(self):
        self.peer.replies.append({"error": "Incorrect API key provided: fixture-secret"})
        messages = [{"role": "user", "content": "测试"}]
        with self.assertRaises(OpenCodeError) as error:
            await retry_stream_errors(lambda: self.submit(messages))
        self.assertEqual(error.exception.code, "not_logged_in")
        self.assertNotIn("fixture-secret", str(error.exception))
        self.assertEqual(len(self.peer.prompts), 1)
        self.assertFalse(self.engine.sessions)

    async def test_permissions_are_denied_and_tool_event_cancels_turn(self):
        self.peer.replies.append({"permission": True, "tool": True, "chunks": ["忽略"]})
        with self.assertRaises(OpenCodeError) as error:
            await self.submit([{"role": "user", "content": "测试"}])
        self.assertEqual(error.exception.code, "tool_blocked")
        self.assertEqual(
            self.peer.denials, [{"outcome": {"outcome": "selected", "optionId": "no"}}]
        )
        self.assertEqual(self.peer.cancelled, ["1"])

    async def test_unlisted_model_and_unsafe_mode_fail_before_prompt(self):
        messages = [{"role": "user", "content": "测试"}]
        self.peer.models = ["opencode/other"]
        with self.assertLogs("astrbot", level="WARNING"), self.assertRaises(OpenCodeError) as error:
            await self.submit(messages)
        self.assertEqual(error.exception.code, "model_unavailable")
        self.peer.models = [MODEL]
        self.peer.mode = "build"
        with self.assertRaises(OpenCodeError) as error:
            await self.submit(messages)
        self.assertEqual(error.exception.code, "chat_mode_required")
        self.assertEqual(self.peer.prompts, [])


class AuthTests(unittest.TestCase):
    def test_only_selected_provider_key_enters_sandbox(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "source.json", Path(temp) / "home/auth.json"
            source.write_text(
                json.dumps(
                    {
                        "opencode": {"type": "api", "key": "example", "extra": "discard"},
                        "other": {"type": "api", "key": "never-copy"},
                    }
                )
            )
            source.chmod(0o600)
            _copy_auth(source, target, "opencode")
            self.assertEqual(
                json.loads(target.read_text()), {"opencode": {"type": "api", "key": "example"}}
            )
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_unsafe_auth_permissions_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.json"
            source.write_text("{}")
            source.chmod(0o644)
            with self.assertRaises(OpenCodeError) as error:
                _copy_auth(source, Path(temp) / "result.json", "opencode")
            self.assertEqual(error.exception.code, "auth_permissions")
