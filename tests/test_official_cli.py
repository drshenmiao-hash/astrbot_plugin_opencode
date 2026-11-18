"""Optional integration test with the official CLI and a loopback model fixture."""

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_plugin import runtime
from opencode_plugin.engine import Engine
from opencode_plugin.errors import OpenCodeError, retry_stream_errors
from opencode_plugin.images import ImageInput
from opencode_plugin.settings import settings_from
from opencode_plugin.token_usage import TurnTokens
from opencode_plugin.transcript import normalize_messages

CLI = os.getenv("OPENCODE_TEST_BIN")
MODEL = "deepseek/deepseek-flash"
VISION_MODEL = "openrouter/openai/gpt-4o"
TEXT_MODEL = "deepseek/deepseek-v4-pro"
REPLY = "本地验证成功"


@unittest.skipUnless(CLI, "Set OPENCODE_TEST_BIN to an official OpenCode 1.19.6 binary")
class OfficialCLITests(unittest.IsolatedAsyncioTestCase):
    async def test_two_chat_turns_and_rejected_api_key(self):
        await self.check_cli(MODEL)

    async def test_image_payload_reaches_the_selected_model_endpoint(self):
        await self.check_cli(VISION_MODEL, with_images=True)

    async def test_text_only_catalog_model_never_sends_an_image_request(self):
        await self.check_cli(TEXT_MODEL, with_images=True, rejects_images=True)

    async def check_cli(self, model, *, with_images=False, rejects_images=False):
        provider, slug = model.split("/", 1)
        requests = []
        started = []
        reject_auth = False

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
                if reject_auth:
                    body = json.dumps(
                        {
                            "error": {
                                "message": "Incorrect API key provided: fixture-private-error",
                                "type": "invalid_request_error",
                                "code": "invalid_api_key",
                            }
                        }
                    ).encode("utf-8")
                    writer.write(
                        (
                            "HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\n"
                            f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n"
                        ).encode("ascii")
                        + body
                    )
                    await writer.drain()
                    return
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
                        "usage": {
                            "prompt_tokens": 3 + len(requests),
                            "completion_tokens": 4,
                            "total_tokens": 7 + len(requests),
                        },
                    },
                ]
                chunks = (
                    f"data: {json.dumps({'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': slug, **event})}\n\n"
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
                    provider: {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}}
                }
                kwargs["env"] = {**env, "OPENCODE_CONFIG_CONTENT": json.dumps(config)}
                process = await create_process(*args, **kwargs)
                started.append((process, kwargs["cwd"]))
                return process

            with tempfile.TemporaryDirectory() as path:
                auth = Path(path) / "auth.json"
                auth.write_text(json.dumps({provider: {"type": "api", "key": "fixture-key-only"}}))
                auth.chmod(0o600)
                engine = Engine(
                    settings_from(
                        {
                            "model_id": model,
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
                        if not with_images:
                            with patch.object(runtime, "_settings", engine.cfg):
                                self.assertIn(model, await runtime._fetch_available_models())
                            probe, probe_dir = started[0]
                            self.assertIsNotNone(probe.returncode)
                            self.assertFalse(probe_dir.exists())
                            self.assertFalse(requests)
                        else:
                            self.assertIn(
                                model, await asyncio.wait_for(engine.available_models(), 15)
                            )
                        messages = [{"role": "user", "content": "test first turn"}]
                        if with_images:
                            from PIL import Image

                            buffer = io.BytesIO()
                            Image.new("RGB", (16, 16), "blue").save(buffer, format="PNG")
                            attachment = (
                                "data:image/png;base64,"
                                + base64.b64encode(buffer.getvalue()).decode()
                            )
                            image_input = ImageInput(path)
                            current = normalize_messages(
                                "test image", image_urls=[attachment], image_input=image_input
                            )
                            if rejects_images:
                                with self.assertRaises(OpenCodeError) as unsupported:
                                    await engine.submit(current, "image", images=image_input.blocks)
                                self.assertEqual(unsupported.exception.code, "image_unsupported")
                                self.assertFalse(requests, "Unsupported image reached upstream")
                                return
                            self.assertEqual(
                                await engine.submit(current, "image", images=image_input.blocks),
                                REPLY,
                            )
                            self.assertTrue(engine.owner._image_support)
                            self.assertEqual(len(requests), 1)
                            message = requests[0][2]["messages"][-1]["content"]
                            self.assertTrue(
                                any(
                                    part["type"] == "image_url"
                                    and part["image_url"]["url"].startswith(
                                        "data:image/png;base64,"
                                    )
                                    for part in message
                                )
                            )
                            self.assertFalse(requests[0][2].get("tools"))

                            self.assertTrue(
                                any("test image" in part.get("text", "") for part in message)
                            )
                            actual = next(
                                part["image_url"]["url"]
                                for part in message
                                if part["type"] == "image_url"
                            )
                            with Image.open(
                                io.BytesIO(base64.b64decode(actual.split(",", 1)[1]))
                            ) as received:
                                self.assertEqual(received.getpixel((8, 8)), (0, 0, 255))
                            return
                        first_stats = {}
                        self.assertEqual(
                            await engine.submit(messages, "test", result_stats=first_stats), REPLY
                        )
                        self.assertEqual(first_stats["tokens"], TurnTokens(8, 4, 4, 0))
                        session = engine.sessions["test"]["session_id"]
                        followup = [
                            *messages,
                            {"role": "assistant", "content": REPLY},
                            {"role": "user", "content": "test second turn"},
                        ]
                        second_stats = {}
                        self.assertEqual(
                            await engine.submit(followup, "test", result_stats=second_stats), REPLY
                        )
                        self.assertEqual(second_stats["tokens"], TurnTokens(9, 5, 4, 0))
                        self.assertEqual(engine.sessions["test"]["session_id"], session)
                        replay_stats = {}
                        self.assertEqual(
                            await engine.submit(followup, "test", result_stats=replay_stats), REPLY
                        )
                        self.assertEqual(replay_stats, {})
                        self.assertEqual(
                            len(requests), 2, "Unexpected extra generation from OpenCode"
                        )
                        reject_auth = True
                        with self.assertRaises(OpenCodeError) as error:
                            await retry_stream_errors(lambda: engine.submit(messages, "auth-test"))
                        self.assertEqual(error.exception.code, "not_logged_in")
                        self.assertNotIn("fixture-private-error", str(error.exception))
                        self.assertNotIn("auth-test", engine.sessions)
                finally:
                    await engine.close()

        self.assertEqual(len(requests), 3)
        for line, authorization, request in requests:
            self.assertEqual(line, "POST /v1/chat/completions HTTP/1.1")
            self.assertEqual(authorization, "Bearer fixture-key-only")
            self.assertFalse(request.get("tools"))
        self.assertIn("test first turn", requests[0][2]["messages"][-1]["content"])
        last = requests[1][2]["messages"][-1]["content"]
        self.assertIn("test second turn", last)
        self.assertNotIn("test first turn", last)
