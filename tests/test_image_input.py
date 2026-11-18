"""Send AstrBot's native multimodal payloads to an in-memory ACP peer."""

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.message import ImageURLPart, Message, TextPart
from astrbot.core.platform.astr_message_event import current_message_event
from PIL import Image

from opencode_plugin import runtime
from opencode_plugin.acp import ACPClient
from opencode_plugin.engine import Engine
from opencode_plugin.errors import OpenCodeError
from opencode_plugin.provider import OpenCodeCLIProvider
from tests.test_acp import MODEL, Peer


def png(width=16, height=16, color="blue"):
    data = io.BytesIO()
    Image.new("RGB", (width, height), color).save(data, format="PNG")
    return data.getvalue()


def attachment(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def image(ref):
    return ImageURLPart(image_url=ImageURLPart.ImageURL(url=ref))


class ImageInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name in (
            "_engine",
            "_settings",
            "_startup_failure",
            "_available_models",
            "_validation_stopped",
            "_retry_validation_after",
        ):
            self.enterContext(patch.object(runtime, name, None))
        runtime.configure(
            {
                "model_id": MODEL,
                "timeout_seconds": 1,
                "image_unsupported_message": "这只模型看不了图片",
            }
        )
        self.enterContext(
            patch.object(runtime, "_fetch_available_models", AsyncMock(return_value=[MODEL]))
        )
        await runtime.ensure_model_validated()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch(
                "astrbot.core.utils.astrbot_path.get_astrbot_temp_path", return_value=str(self.root)
            )
        )
        self.peer = Peer()
        client = ACPClient(
            self.peer.process, frame_limit=65536, output_limit=4096, ready={"cwd": "/work"}
        )
        await client.start()
        self.engine = Engine(runtime.settings())
        self.engine.client = client
        self.capability = AsyncMock(return_value=True)
        self.engine.owner = SimpleNamespace(
            model_supports_images=self.capability, close=AsyncMock(), usage_db=None
        )
        runtime._engine = self.engine
        self.provider = OpenCodeCLIProvider({"id": "native", "model": MODEL}, {})
        self.addAsyncCleanup(self.provider.terminate)

    async def test_image_text_text_text_image_uses_one_acp_session(self):
        self.peer.replies.extend(
            {"chunks": [reply]} for reply in ("first", "text1", "text2", "text3", "last")
        )
        first, second = attachment(png()), attachment(png(color="red"))
        durable_history = []
        for index, (body, images) in enumerate(
            (("看图", [first]), ("a", []), ("b", []), ("c", []), ("再看", [second, first]))
        ):
            extra = TextPart(text=f"private metadata {index}").mark_as_temp()
            content = [TextPart(text=body), *map(image, images)]
            response = await self.provider.text_chat(
                contexts=[*durable_history, Message(role="user", content=[*content, extra])],
                conversation_id="chat",
                extra_user_content_parts=[extra],
            )
            frame = self.peer.prompts[-1]
            self.assertEqual(len(frame["prompt"]) - 1, len(images))
            self.assertEqual(frame["prompt"][0]["text"].count(f"private metadata {index}"), 1)
            durable_history.extend(
                [
                    Message(role="user", content=content),
                    Message(role="assistant", content=response.completion_text),
                ]
            )
        self.assertEqual({frame["sessionId"] for frame in self.peer.prompts}, {"1"})
        self.assertEqual([len(frame["prompt"]) - 1 for frame in self.peer.prompts], [1, 0, 0, 0, 2])

    async def test_history_images_are_not_opened_or_counted_on_rebuild(self):
        self.peer.replies.append({"chunks": ["OK"]})
        history = [{"role": "user", "content": [image("/outside/private.png")] * 20}]
        await self.provider.text_chat(prompt="text", contexts=history, conversation_id="chat")
        blocks = self.peer.prompts[0]["prompt"]
        self.assertEqual(len(blocks), 1)
        self.assertIn('"historical_images_not_attached":20', blocks[0]["text"])
        self.assertNotIn("/outside/private.png", blocks[0]["text"])

    async def test_current_local_and_image_only_upload(self):
        picture = self.root / "local.png"
        picture.write_bytes(png())
        self.peer.replies.append({"chunks": ["OK"]})
        await self.provider.text_chat(image_urls=[picture.as_uri()])
        self.assertEqual(self.peer.prompts[0]["prompt"][1]["type"], "image")

    async def test_preflight_errors_are_typed_and_logged_by_limit(self):
        for images, code, limit in (
            (["/outside/private.png"], "image_source", None),
            ([attachment(b"not a PNG")], "image_invalid", None),
            ([attachment(png(2001, 2000))], "image_limit", "pixels"),
            ([attachment(png())] * 5, "image_limit", "count"),
            ([attachment(bytes(2 * 1024 * 1024 + 1))], "image_limit", "file_bytes"),
        ):
            with self.subTest(code=code, limit=limit), self.assertLogs("astrbot") as logs:
                with self.assertRaises(OpenCodeError) as error:
                    await self.provider.text_chat(prompt="test", image_urls=images)
                self.assertEqual(error.exception.code, code)
                records = [
                    json.loads(line.split("opencode_provider ", 1)[1])
                    for line in logs.output
                    if '"event":"request_finished"' in line
                ]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].get("image_limit_kind"), limit)
        self.assertFalse(self.peer.prompts)

    async def test_unsupported_model_sends_custom_notice_once_and_no_prompt(self):
        attachments = [attachment(png())]
        self.capability.return_value = False
        extras = {}
        event = SimpleNamespace(
            get_extra=extras.get, set_extra=extras.__setitem__, send=AsyncMock()
        )
        event_token = current_message_event.set(event)
        try:
            for _ in range(2):
                with self.assertRaises(OpenCodeError) as error:
                    await self.provider.text_chat(image_urls=attachments, conversation_id="chat")
                self.assertEqual(error.exception.code, "image_unsupported")
                self.assertIn("这只模型看不了图片", str(error.exception))
            event.send.assert_awaited_once()
        finally:
            current_message_event.reset(event_token)
        self.assertFalse(self.peer.prompts)

    async def test_image_timeout_cancels_session_then_text_can_continue(self):
        self.peer.replies.extend([{"wait_for_cancel": True}, {"chunks": ["OK"]}])
        with self.assertRaises(OpenCodeError) as error:
            await self.provider.text_chat(image_urls=[attachment(png())], conversation_id="chat")
        self.assertEqual(error.exception.code, "timeout")
        self.assertEqual(self.peer.cancelled, ["1"])
        second = await self.provider.text_chat(prompt="after timeout", conversation_id="chat")
        self.assertEqual(second.completion_text, "OK")
        self.assertEqual(self.peer.prompts[-1]["sessionId"], "2")
