"""OpenCode lifecycle against AstrBot's Provider class and a small manager double."""

import asyncio
import importlib
import sys
import tempfile
import unittest
from copy import deepcopy
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_registration import MODEL, NATIVE_ID, existing_config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
main = importlib.import_module(ROOT.name + ".main")
runtime = main.runtime
metrics = importlib.import_module(runtime.__package__ + ".metrics")
tokens = importlib.import_module(runtime.__package__ + ".token_usage")


class Manager:
    _mcp_init_task = None

    def __init__(self):
        self.oauth = object()
        self.inst_map = {"oauth-model": self.oauth}
        self.curr_provider_inst = self.oauth

    async def reload(self, model):
        old = self.inst_map.pop(model["id"], None)
        if old:
            await old.terminate()
        if model["enable"]:
            self.inst_map[model["id"]] = main.OpenCodeCLIProvider(model, {})


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = Manager()
        self.config = existing_config()
        self.context = SimpleNamespace(
            provider_manager=self.manager,
            astrbot_config_mgr=SimpleNamespace(default_conf=self.config),
            registered_web_apis=[],
        )
        self.context.register_web_api = partial(main.Context.register_web_api, self.context)
        path = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(runtime, "_engine", None))
        self.enterContext(patch.object(runtime, "_usage_store", None))
        self.enterContext(patch.object(metrics, "_usage_store", None))
        self.store = runtime.configure_usage(self.context, directory=path)
        self.addCleanup(self.store.close)
        self.fetch = self.enterContext(
            patch.object(runtime, "_fetch_available_models", AsyncMock(return_value=[MODEL]))
        )
        self.plugin = main.OpenCodeCLIPlugin(self.context, {"model_id": MODEL})
        self.addAsyncCleanup(self.plugin.terminate)
        await self.plugin.initialize()
        self.provider = self.manager.inst_map[NATIVE_ID]

    async def test_failed_reload_keeps_dashboard_and_old_provider_is_closed(self):
        old_engine = self.provider.engine
        selected = self.manager.curr_provider_inst
        config = deepcopy(dict(self.config))
        self.store.record({"event": "request_finished", "request_id": "legacy", "code": "ok"})
        self.fetch.side_effect = main.OpenCodeError("not_logged_in")
        replacement = main.OpenCodeCLIPlugin(self.context, {"model_id": MODEL})
        self.addAsyncCleanup(replacement.terminate)
        await replacement.initialize()
        report = await runtime.usage_dashboard(7)
        self.assertEqual(report["startup_error"]["code"], "not_logged_in")
        self.assertFalse(report["validated"])
        self.assertEqual(report["totals"]["requests"], 1)
        self.assertTrue(replacement._usage_page.active)
        self.assertEqual(len(self.context.registered_web_apis), 1)
        self.assertTrue(old_engine.closed)
        self.assertEqual(old_engine.references, 0)
        with self.assertRaises(main.OpenCodeError) as error:
            await self.provider.text_chat(prompt="stopped")
        self.assertEqual(error.exception.code, "closed")
        self.assertEqual(self.store.summary()["rows"][0]["codes"], {"closed": 1, "ok": 1})
        self.assertEqual(dict(self.config), config)
        self.assertIs(self.manager.curr_provider_inst, selected)

    async def test_plugin_stop_lets_inflight_chat_fail_and_keeps_other_providers(self):
        engine = self.provider.engine
        chat_started = asyncio.Event()
        cancelled = asyncio.Event()

        async def prompt(*_args, **_kwargs):
            chat_started.set()
            await cancelled.wait()
            raise main.OpenCodeError("cancelled")

        async def notify(*_args):
            cancelled.set()

        engine.client = SimpleNamespace(
            closed=False,
            new_session=AsyncMock(return_value="session"),
            prompt=prompt,
            notify=notify,
            close=AsyncMock(),
        )
        chat = asyncio.create_task(self.provider.text_chat(prompt="pending"))
        try:
            await asyncio.wait_for(chat_started.wait(), 2)
            await self.plugin.terminate()
            with self.assertRaises(main.OpenCodeError) as error:
                await chat
            self.assertEqual(error.exception.code, "closed")
            self.assertIsNone(engine.client)
            self.assertEqual(engine.workers, [])
            self.assertIs(self.manager.inst_map["oauth-model"], self.manager.oauth)
        finally:
            if not chat.done():
                chat.cancel()
            await asyncio.gather(chat, return_exceptions=True)

    async def test_provider_only_returns_native_usage_for_reported_compatible_totals(self):
        cases = [
            (tokens.TurnTokens(13, 10, 3, 5), 13),
            (tokens.TurnTokens(10, 10, 3, 5), None),
            (None, None),
        ]
        for payload, expected in cases:

            async def reply(*_args, **kwargs):
                if payload is not None:
                    kwargs["result_stats"]["tokens"] = payload
                return "message"

            with patch.object(self.provider.engine, "submit", reply):
                response = await self.provider.text_chat(prompt="test")
            self.assertEqual(response.usage.total if response.usage else None, expected)
