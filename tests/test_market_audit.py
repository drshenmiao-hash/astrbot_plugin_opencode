"""Market audit regressions for the host-owned registries and local SQLite writer."""

import asyncio
import importlib
import json
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.provider.register import provider_cls_map, provider_registry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
main = importlib.import_module(ROOT.name + ".main")
runtime = main.runtime
TYPE = runtime.PROVIDER_TYPE
USAGE_ATTRIBUTE = "_astrbot_opencode_usage_store"


class MarketAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.context = SimpleNamespace(registered_web_apis=[])
        self.context.register_web_api = partial(main.Context.register_web_api, self.context)
        self.plugin = main.OpenCodeCLIPlugin(self.context, {})
        self.addAsyncCleanup(self.plugin.terminate)
        self.enterContext(patch.object(runtime, "initialize", AsyncMock()))
        self.enterContext(patch.object(runtime, "_engine", None))
        self.configure = runtime.configure_usage
        self.enterContext(
            patch.object(
                runtime,
                "configure_usage",
                side_effect=lambda context: self.configure(context, directory=self.directory),
            )
        )

    async def test_disable_releases_owned_resources_and_reload_reads_old_rows(self):
        foreign = ("/elsewhere", object(), ["POST"], "another plugin")
        self.context.registered_web_apis.append(foreign)
        await self.plugin.initialize()
        store = getattr(self.context, USAGE_ATTRIBUTE)
        provider_metadata = provider_cls_map[TYPE]
        store.record({"event": "request_finished", "request_id": "pending", "code": "ok"})
        label = store.session_label("session")
        await self.plugin.terminate()
        self.assertTrue(store._closed)
        self.assertIsNone(runtime._usage_store)
        self.assertFalse(hasattr(self.context, USAGE_ATTRIBUTE))
        self.assertEqual(self.context.registered_web_apis, [foreign])
        self.assertNotIn(TYPE, provider_cls_map)
        self.assertNotIn(provider_metadata, provider_registry)

        replacement = main.OpenCodeCLIPlugin(self.context, {})
        self.addAsyncCleanup(replacement.terminate)
        await replacement.initialize()
        replacement_store = getattr(self.context, USAGE_ATTRIBUTE)
        self.assertIsNot(replacement_store, store)
        self.assertEqual(replacement_store.session_label("session"), label)
        self.assertEqual(replacement_store.summary()["rows"][0]["requests"], 1)

    async def test_cancelled_initialize_waits_for_storage_then_cleans_up(self):
        entered = threading.Event()
        proceed = threading.Event()

        def open_storage(context):
            entered.set()
            if not proceed.wait(3):
                raise TimeoutError("test storage did not resume")
            return self.configure(context, directory=self.directory)

        with patch.object(runtime, "configure_usage", side_effect=open_storage):
            opening = asyncio.create_task(self.plugin.initialize())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                opening.cancel()
                await asyncio.sleep(0)
                self.assertFalse(opening.done())
            finally:
                proceed.set()
                with self.assertRaises(asyncio.CancelledError):
                    await opening
        self.assertFalse(hasattr(self.context, USAGE_ATTRIBUTE))
        self.assertIsNone(runtime._usage_store)
        self.assertNotIn(TYPE, provider_cls_map)

    async def test_unexpected_start_failure_unwinds_route_registry_and_storage(self):
        def register_then_fail(*args):
            main.Context.register_web_api(self.context, *args)
            raise RuntimeError("fixture registration failure")

        self.context.register_web_api = Mock(side_effect=register_then_fail)
        with self.assertRaisesRegex(RuntimeError, "fixture registration failure"):
            await self.plugin.initialize()
        self.assertIsNone(runtime._usage_store)
        self.assertFalse(hasattr(self.context, USAGE_ATTRIBUTE))
        self.assertEqual(self.context.registered_web_apis, [])
        self.assertNotIn(TYPE, provider_cls_map)

    async def test_cancelled_config_commit_finishes_without_diverging_from_disk(self):
        entered = asyncio.Event()
        proceed = asyncio.Event()
        saved = []

        class Config(dict):
            async def save_config_async(self):
                snapshot = dict(self)
                entered.set()
                await proceed.wait()
                saved.append(snapshot)
                return True

        config = Config(provider_sources=["old"], provider_settings={"fallback": ["backup"]})
        saving = asyncio.create_task(
            runtime._commit_configuration(config, {"provider_sources": ["plugin"]})
        )
        await entered.wait()
        saving.cancel()
        await asyncio.sleep(0)
        self.assertFalse(saving.done())
        proceed.set()
        with self.assertRaises(asyncio.CancelledError):
            await saving
        self.assertEqual(saved, [dict(config)])
        self.assertEqual(config["provider_settings"], {"fallback": ["backup"]})

    async def test_invalid_settings_leave_dashboard_available_without_publishing_a_model(self):
        bad = main.OpenCodeCLIPlugin(self.context, {"timeout_seconds": 0})
        self.addAsyncCleanup(bad.terminate)
        await bad.initialize()
        runtime.initialize.assert_not_awaited()
        report = await runtime.usage_dashboard(7)
        self.assertFalse(report["validated"])
        self.assertEqual(report["startup_error"]["code"], "config")
        self.assertTrue(bad._usage_page.active)

    def test_schema_defaults_and_integer_types_match_runtime(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text())
        defaults = {field: spec["default"] for field, spec in schema.items()}
        self.assertEqual(runtime.settings_from(defaults), runtime.settings_from({}))
        for value in (True, 1.8):
            with self.assertRaises(main.OpenCodeError) as error:
                runtime.settings_from({"max_concurrency": value})
            self.assertEqual(error.exception.code, "config")
        self.assertEqual(runtime.settings_from({"max_concurrency": "2"})["max_concurrency"], 2)
