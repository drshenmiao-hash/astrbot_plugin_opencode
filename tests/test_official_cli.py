"""Optional integration test with the official CLI and a loopback model fixture."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_plugin.engine import Engine
from opencode_plugin.settings import settings_from

CLI = os.getenv("OPENCODE_TEST_BIN")
MODEL = "deepseek/deepseek-flash"
REPLY = "本地验证成功"


@unittest.skipUnless(CLI, "Set OPENCODE_TEST_BIN to an official OpenCode 1.19.6 binary")
class OfficialCLITests(unittest.IsolatedAsyncioTestCase):
    async def test_two_chat_turns_do_not_generate_a_title_or_send_tools(self):
        requests = []

        async def serve(reader, writer):
            try:
                header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                request_line, *lines = header.strip().split("\r\n")
                headers = {
                    key.lower(): value.strip()
                    for key, value in (line.split(":", 1) for line in lines)
                }
                request = json.loads(await reader.readexactly(int(headers["content-length"])))
                requests.append((request_line, headers.get("authorization"), request))
                events = [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": REPLY},
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
                    },
                ]
                chunks = (
                    f"data: {json.dumps({'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'deepseek-flash', **event})}\n\n"
                    for event in events
                )
                body = ("".join(chunks) + "data: [DONE]\n\n").encode("utf-8")
                writer.write(
                    (
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n"
                    ).encode("ascii")
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            create_process = asyncio.create_subprocess_exec

            async def launch_with_fixture(*args, **kwargs):
                # Change only the upstream address in this test. The rest of the
                # production configuration and the sandbox both run as shipped.
                env = kwargs["env"]
                config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                config["provider"] = {
                    "deepseek": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}}
                }
                kwargs["env"] = {**env, "OPENCODE_CONFIG_CONTENT": json.dumps(config)}
                return await create_process(*args, **kwargs)

            with tempfile.TemporaryDirectory() as path:
                auth = Path(path) / "auth.json"
                auth.write_text('{"deepseek":{"type":"api","key":"fixture-key-only"}}')
                auth.chmod(0o600)
                engine = Engine(
                    settings_from(
                        {
                            "model_id": MODEL,
                            "cli_binary": CLI,
                            "auth_path": str(auth),
                            "timeout_seconds": 15,
                        }
                    )
                )
                try:
                    with patch(
                        "opencode_plugin.launcher.asyncio.create_subprocess_exec",
                        launch_with_fixture,
                    ):
                        self.assertIn(MODEL, await asyncio.wait_for(engine.available_models(), 15))
                        messages = [{"role": "user", "content": "test first turn"}]
                        self.assertEqual(await engine.submit(messages, "test"), REPLY)
                        session = engine.sessions["test"]["session_id"]
                        followup = [
                            *messages,
                            {"role": "assistant", "content": REPLY},
                            {"role": "user", "content": "test second turn"},
                        ]
                        self.assertEqual(await engine.submit(followup, "test"), REPLY)
                        self.assertEqual(engine.sessions["test"]["session_id"], session)
                finally:
                    await engine.close()

        self.assertEqual(len(requests), 2, "Unexpected additional request from OpenCode")
        for line, authorization, request in requests:
            self.assertEqual(line, "POST /v1/chat/completions HTTP/1.1")
            self.assertEqual(authorization, "Bearer fixture-key-only")
            self.assertFalse(request.get("tools"))
        self.assertIn("test first turn", requests[0][2]["messages"][-1]["content"])
        last = requests[1][2]["messages"][-1]["content"]
        self.assertIn("test second turn", last)
        self.assertNotIn("test first turn", last)
