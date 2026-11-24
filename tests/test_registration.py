"""Exercise real native config/save/reload code with explicit host API doubles.

No AstrBot install, CLI, subprocess, network, account or live config is accessed.
ProviderManager and the resulting Provider instance are doubles; tests do not
claim actual AstrBot/WebUI integration coverage.
"""

import sys
import unittest
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from opencode_plugin import runtime
from opencode_plugin.errors import OpenCodeError

MODEL = "mock/explicit-plan-model"
NATIVE_ID = f"opencode_cli:{MODEL}"


class Config(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.saved = []
        self.results = []

    async def save_config_async(self):
        self.saved.append(deepcopy(dict(self)))
        result = self.results.pop(0) if self.results else True
        if isinstance(result, Exception):
            raise result
        return result


class NativeModel:
    def __init__(self, config):
        self.config = deepcopy(config)
        self.closed = False


class Manager:
    def __init__(self, config, *, running=True):
        self.config = config
        self.providers_config = config["provider"]
        self.provider_sources_config = config["provider_sources"]
        self.inst_map = {model["id"]: object() for model in config["provider"]} if running else {}
        self.curr_provider_inst = self.inst_map.get("oauth-model")
        self._mcp_init_task = object() if running else None
        self.reloads = []

    async def reload(self, model):
        self.reloads.append(model["id"])
        source = next(
            (s for s in self.provider_sources_config if s["id"] == model.get("provider_source_id")),
            {},
        )
        merged = {**source, **model}
        if merged.get("type") != "opencode_cli":
            raise AssertionError("Plugin tried to reload an unrelated provider")
        old = self.inst_map.pop(model["id"], None)
        if isinstance(old, NativeModel):
            old.closed = True
        if model.get("enable", True):
            self.inst_map[model["id"]] = NativeModel(merged)
            # Deliberately emulate a host that selects while loading. The plugin
            # must restore the previous selection, including None, afterwards.
            self.curr_provider_inst = self.inst_map[model["id"]]


def existing_config():
    return Config(
        provider_sources=[
            {
                "id": "oauth-source",
                "type": "OpenAI Subscribe",
                "key": "DUMMY_OAUTH",
                "api_base": "https://example.invalid/oauth",
                "enable": True,
                "oauth_accounts": [{"id": "local", "key": "DUMMY_ACCOUNT"}],
                "oauth_routing": {"models": {"oauth-model": "local"}},
            },
            {
                "id": "deepseek-source",
                "type": "openai_chat_completion",
                "key": ["DUMMY_DS"],
                "api_base": "https://example.invalid/deepseek",
                "enable": True,
            },
        ],
        provider=[
            {
                "id": "oauth-model",
                "provider_source_id": "oauth-source",
                "enable": True,
                "model": "oauth-pinned",
                "modalities": ["text", "image", "tool_use"],
            },
            {
                "id": "deepseek-model",
                "provider_source_id": "deepseek-source",
                "enable": True,
                "model": "deepseek-chat",
                "modalities": ["text", "tool_use"],
            },
        ],
        provider_settings={"fallback_chat_models": ["deepseek-model", "oauth-model"]},
        agent={"runtime": "astrbot", "config": {"model": {"provider_id": "oauth-model"}}},
        session_preferences={"group": {"model": "deepseek-model"}},
    )


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_settings = runtime._settings
        self.original_engine = runtime._engine
        self.original_catalog = runtime._available_models
        runtime._engine = None
        runtime.configure({"model_id": MODEL})
        self.catalog_patch = patch.object(
            runtime,
            "_fetch_available_models",
            new=AsyncMock(return_value=[MODEL, "mock/other-model"]),
        )
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.conf = existing_config()
        self.manager = Manager(self.conf)
        self.context = SimpleNamespace(
            astrbot_config_mgr=SimpleNamespace(default_conf=self.conf),
            provider_manager=self.manager,
        )
        provider = ModuleType("opencode_plugin.provider")
        provider.OpenCodeCLIProvider = NativeModel
        self.provider_patch = patch.dict(sys.modules, {"opencode_plugin.provider": provider})
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        self.process_patch = patch(
            "asyncio.create_subprocess_exec",
            side_effect=AssertionError("No processes in registration tests"),
        )
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def tearDown(self):
        runtime._settings = self.original_settings
        runtime._engine = self.original_engine
        runtime._available_models = self.original_catalog

    def opencode_sources(self):
        return [s for s in self.conf["provider_sources"] if s.get("type") == "opencode_cli"]

    def opencode_models(self):
        ids = {s["id"] for s in self.opencode_sources()}
        return [m for m in self.conf["provider"] if m.get("provider_source_id") in ids]

    async def test_first_load_creates_exact_source_and_native_model(self):
        await runtime.initialize(self.context)
        self.assertEqual(await runtime.available_model_ids(), [MODEL])
        self.assertEqual(len(self.opencode_sources()), 1)
        source = self.opencode_sources()[0]
        self.assertEqual(source["type"], "opencode_cli")
        self.assertEqual(source["key"], "")
        self.assertEqual(source["api_base"], "")
        model = self.opencode_models()[0]
        self.assertEqual(model["id"], NATIVE_ID)
        self.assertEqual(model["model"], MODEL)
        self.assertEqual(model["provider_source_id"], source["id"])
        self.assertEqual(self.manager.reloads, [NATIVE_ID])
        self.assertIn(NATIVE_ID, self.manager.inst_map)
        self.assertEqual(len(self.conf.saved), 1)
        self.assertEqual(self.conf.saved[-1], dict(self.conf))

    async def test_repeated_load_does_not_duplicate_or_rewrite_config(self):
        await runtime.initialize(self.context)
        first = self.manager.inst_map[NATIVE_ID]
        before = deepcopy(dict(self.conf))
        await runtime.initialize(self.context)
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(len(self.opencode_sources()), 1)
        self.assertEqual(len(self.opencode_models()), 1)
        self.assertEqual(len(self.conf.saved), 1)
        self.assertTrue(first.closed)
        self.assertIsNot(self.manager.inst_map[NATIVE_ID], first)
        self.assertEqual(len(self.manager.inst_map), 3)

    async def test_empty_or_auto_does_not_create_model_and_warns(self):
        runtime.configure({"model_id": ""})
        with self.assertLogs("astrbot", level="WARNING"):
            await runtime.initialize(self.context)
        self.assertEqual(self.opencode_models(), [])
        self.assertEqual(self.manager.reloads, [])
        with self.assertRaises(OpenCodeError):
            runtime.configure({"model_id": "auto"})

    async def test_default_model_and_fallbacks_remain_unchanged(self):
        selected = self.manager.curr_provider_inst
        fields = {
            key: deepcopy(value)
            for key, value in self.conf.items()
            if key not in {"provider", "provider_sources"}
        }
        await runtime.initialize(self.context)
        self.assertIs(self.manager.curr_provider_inst, selected)
        self.assertEqual({key: self.conf[key] for key in fields}, fields)

    async def test_existing_oauth_and_deepseek_are_untouched(self):
        sources = deepcopy(self.conf["provider_sources"])
        models = deepcopy(self.conf["provider"])
        instances = dict(self.manager.inst_map)
        await runtime.initialize(self.context)
        await runtime.initialize(self.context)
        self.assertEqual(self.conf["provider_sources"][:2], sources)
        self.assertEqual(self.conf["provider"][:2], models)
        for key, instance in instances.items():
            self.assertIs(self.manager.inst_map[key], instance)
        self.assertEqual(self.manager.reloads, [NATIVE_ID, NATIVE_ID])

    async def test_switching_pin_disables_old_generated_entry_and_restores_it_on_switch_back(self):
        await runtime.initialize(self.context)
        old = self.manager.inst_map[NATIVE_ID]
        self.manager.curr_provider_inst = old
        self.conf["agent"]["config"]["model"]["provider_id"] = NATIVE_ID
        stable_settings = deepcopy(self.conf["agent"])

        other = "mock/other-model"
        other_id = f"opencode_cli:{other}"
        runtime.configure({"model_id": other})
        await runtime.initialize(self.context)
        self.assertEqual(await runtime.available_model_ids(), [other])
        self.assertTrue(old.closed)
        self.assertNotIn(NATIVE_ID, self.manager.inst_map)
        self.assertIn(other_id, self.manager.inst_map)
        self.assertIsNone(self.manager.curr_provider_inst)
        self.assertEqual(self.conf["agent"], stable_settings)
        self.assertEqual([m["id"] for m in self.opencode_models() if m["enable"]], [other_id])

        runtime.configure({"model_id": MODEL})
        await runtime.initialize(self.context)
        self.assertEqual([m["id"] for m in self.opencode_models() if m["enable"]], [NATIVE_ID])
        self.assertIn(NATIVE_ID, self.manager.inst_map)
        self.assertNotIn(other_id, self.manager.inst_map)

    async def test_manually_disabled_model_stays_disabled(self):
        await runtime.initialize(self.context)
        self.opencode_models()[0]["enable"] = False
        runtime.configure({"model_id": "mock/other-model"})
        await runtime.initialize(self.context)
        runtime.configure({"model_id": MODEL})
        await runtime.initialize(self.context)
        self.assertFalse(self.opencode_models()[0]["enable"])
        self.assertNotIn(NATIVE_ID, self.manager.inst_map)

    async def test_image_capability_for_pinned_model_without_audio_or_tools(self):
        await runtime.initialize(self.context)
        source = self.opencode_sources()[0]
        model = self.opencode_models()[0]
        self.assertEqual(source["modalities"], ["text"])
        for config in [model, self.manager.inst_map[NATIVE_ID].config]:
            self.assertEqual(config["modalities"], ["text", "image"])
            self.assertEqual(config["key"], "")
            self.assertEqual(config["api_base"], "")

    async def test_existing_source_and_model_are_updated_in_place(self):
        self.conf["provider_sources"].append(
            {
                "id": "custom-opencode",
                "type": "opencode_cli",
                "enable": True,
                "key": ["OLD"],
                "api_base": "old",
            }
        )
        self.conf["provider"].append(
            {
                "id": NATIVE_ID,
                "provider_source_id": "custom-opencode",
                "model": "old-model",
                "enable": True,
                "modalities": ["text", "image", "tool_use"],
                "temperature": 0.4,
                "key": ["OLD"],
            }
        )
        await runtime.initialize(self.context)
        self.assertEqual(len(self.opencode_sources()), 1)
        self.assertEqual(self.opencode_sources()[0]["id"], "custom-opencode")
        self.assertEqual(len(self.opencode_models()), 1)
        self.assertEqual(self.opencode_models()[0]["model"], MODEL)
        self.assertEqual(self.opencode_models()[0]["temperature"], 0.4)
        self.assertEqual(self.opencode_models()[0]["modalities"], ["text", "image"])

    async def test_same_model_alias_is_not_duplicated_or_renamed(self):
        self.conf["provider_sources"].append({"id": "custom-opencode", "type": "opencode_cli"})
        model = {
            "id": "old-opencode-alias",
            "model": MODEL,
            "enable": True,
            "provider_source_id": "custom-opencode",
        }
        self.conf["provider"].append(model)
        old = NativeModel(model)
        self.manager.inst_map[model["id"]] = old
        self.manager.curr_provider_inst = old
        self.conf["agent"]["config"]["model"]["provider_id"] = model["id"]
        await runtime.initialize(self.context)
        self.assertEqual([m["id"] for m in self.opencode_models()], [model["id"]])
        self.assertIs(self.manager.curr_provider_inst, self.manager.inst_map[model["id"]])
        self.assertEqual(self.conf["agent"]["config"]["model"]["provider_id"], model["id"])
        self.assertTrue(old.closed)

    async def test_disabled_source_stays_disabled_and_new_model_is_disabled(self):
        self.conf["provider_sources"].append(
            {"id": "custom-opencode", "type": "opencode_cli", "enable": False}
        )
        await runtime.initialize(self.context)
        self.assertFalse(self.opencode_sources()[0]["enable"])
        self.assertFalse(self.opencode_models()[0]["enable"])
        self.assertEqual(self.manager.reloads, [])

    async def test_foreign_model_id_collision_never_overwrites(self):
        self.conf["provider"][0]["id"] = NATIVE_ID
        before = deepcopy(self.conf["provider"])
        with self.assertRaises(OpenCodeError) as error:
            await runtime.initialize(self.context)
        self.assertEqual(error.exception.code, "model_id_conflict")
        self.assertFalse(self.conf.saved)
        self.assertEqual(self.conf["provider"], before)
        self.assertEqual(self.opencode_models(), [])
        self.assertEqual(self.manager.reloads, [])

    async def test_foreign_source_id_collision_never_overwrites(self):
        self.conf["provider_sources"][0]["id"] = "opencode_cli"
        before = deepcopy(dict(self.conf))
        with self.assertRaises(OpenCodeError) as error:
            await runtime.initialize(self.context)
        self.assertEqual(error.exception.code, "source_id_conflict")
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(self.conf.saved, [])

    async def test_cold_start_syncs_config_and_defers_instances_to_host(self):
        self.manager = Manager(self.conf, running=False)
        self.context.provider_manager = self.manager
        await runtime.initialize(self.context)
        self.assertEqual(len(self.opencode_models()), 1)
        self.assertIs(self.manager.providers_config, self.conf["provider"])
        self.assertIs(self.manager.provider_sources_config, self.conf["provider_sources"])
        self.assertEqual(self.manager.inst_map, {})
        self.assertEqual(self.manager.reloads, [])
        self.assertIsNone(self.manager.curr_provider_inst)

    async def test_running_empty_manager_loads_model_without_selecting_it(self):
        self.conf["provider"] = []
        self.manager = Manager(self.conf)
        self.context.provider_manager = self.manager
        self.assertIsNone(self.manager.curr_provider_inst)
        await runtime.initialize(self.context)
        self.assertIn(NATIVE_ID, self.manager.inst_map)
        self.assertIsNone(self.manager.curr_provider_inst)

    async def test_save_failure_rolls_back_only_registration(self):
        before = deepcopy(dict(self.conf))
        selected = self.manager.curr_provider_inst
        self.conf.results = [OSError("simulated write failure")]
        with self.assertRaises(OpenCodeError) as error:
            await runtime.initialize(self.context)
        self.assertEqual(error.exception.code, "registration_save_failed")
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(self.manager.reloads, [])
        self.assertIs(self.manager.curr_provider_inst, selected)

    async def test_superseded_save_is_retried_once(self):
        self.conf.results = [False, True]
        await runtime.initialize(self.context)
        self.assertEqual(len(self.conf.saved), 2)
        self.assertEqual(len(self.opencode_models()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
