"""Local statistics regressions; only temporary plugin data and in-memory ACP."""

import ast
import asyncio
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opencode_plugin import metrics, runtime
from opencode_plugin.acp import ACPClient
from opencode_plugin.engine import Engine
from opencode_plugin.errors import OpenCodeError
from opencode_plugin.settings import settings_from
from opencode_plugin.usage import DISCLAIMER, UsageStore, format_summary


def event(request_id="one", **changes):
    return {
        "event": "request_finished",
        "request_id": request_id,
        "session": "anonymous",
        "model_id": "mock/grok-4.6",
        "code": "ok",
        "duration_ms": 100,
        "queue_ms": 20,
        "input_bytes": 200,
        "sent_input_bytes": 30,
        "wire_bytes": 80,
        "output_bytes": 10,
        "process_reused": True,
        "reused": True,
        **changes,
    }


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "plugin_data" / "astrbot_plugin_opencode"
        self.store = UsageStore(self.directory)
        self.addCleanup(self.store.close)

    def test_success_and_failure_and_all_metadata_are_recorded(self):
        self.store.record(event("ok"))
        self.store.record(event("bad", code="timeout", process_reused=False))
        row = self.store.summary()["rows"][0]
        self.assertEqual((row["requests"], row["success"], row["failed"]), (2, 1, 1))
        self.assertEqual(row["codes"], {"ok": 1, "timeout": 1})
        for key, expected in {
            "duration_ms": 200,
            "queue_ms": 40,
            "input_bytes": 400,
            "sent_input_bytes": 60,
            "wire_bytes": 160,
            "output_bytes": 20,
            "process_reused": 1,
            "reused": 2,
        }.items():
            self.assertEqual(row[key], expected)

    def test_model_day_and_session_aggregates(self):
        day1 = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        day2 = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        self.store.record(event("one"), at=day1)
        self.store.record(event("two", code="quota_exhausted", model_id="mock/sonnet-4.6"), at=day2)
        self.store.record(event("three", model_id=None, session="other", wire_bytes=0), at=day2)
        models = {r["label"]: r["requests"] for r in self.store.summary("model")["rows"]}
        self.assertEqual(models, {"mock/grok-4.6": 1, "mock/sonnet-4.6": 1, "": 1})
        days = {r["label"]: r["requests"] for r in self.store.summary("day")["rows"]}
        self.assertEqual(days, {"2026-11-15": 1, "2026-11-16": 2})
        self.assertEqual(self.store.summary("session", "anonymous")["rows"][0]["requests"], 2)
        self.assertEqual(self.store.summary("day", "2026-11-16")["rows"][0]["failed"], 1)

    def test_restart_preserves_rows_and_anonymous_session_identity(self):
        label = self.store.session_label("QQ-origin/conversation")
        self.store.record(event(session=label))
        self.store.close()
        reopened = UsageStore(self.directory)
        try:
            self.assertEqual(reopened.summary()["rows"][0]["requests"], 1)
            self.assertEqual(reopened.session_label("QQ-origin/conversation"), label)
        finally:
            reopened.close()

    def test_duplicate_request_id_is_not_double_counted(self):
        self.store.record(event())
        self.store.record(event())
        self.assertEqual(self.store.summary()["rows"][0]["requests"], 1)

    def test_non_request_events_are_not_counted(self):
        self.store.record({"event": "process_started"})
        self.assertEqual(self.store.summary()["rows"], [])

    def test_only_plugin_directory_and_metadata_are_written(self):
        config = Path(self.temp.name) / "cmd_config.json"
        config.write_text('{"default":"oauth-model"}')
        before = config.read_bytes()
        self.store.record(event(prompt="PRIVATE_CHAT_SENTINEL", credential="PRIVATE_KEY_SENTINEL"))
        self.store.flush()
        with sqlite3.connect(self.store.path) as db:
            rows = repr(db.execute("SELECT * FROM requests").fetchall())
        self.assertNotIn("PRIVATE_CHAT_SENTINEL", rows)
        self.assertNotIn("PRIVATE_KEY_SENTINEL", rows)
        self.assertEqual(config.read_bytes(), before)
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file() and path != config:
                self.assertIn(self.directory, path.parents)

    def test_report_disclaims_billing_and_labels_unsent_requests(self):
        self.store.record(event(model_id=None, wire_bytes=0))
        text = format_summary(self.store.summary("model"))
        self.assertIn(DISCLAIMER, text)
        self.assertIn("未记录 ACP 发送", text)
        self.assertIn("不是 token 或费用", text)

    def test_invalid_group_cannot_be_used_as_sql(self):
        with self.assertRaises(ValueError):
            self.store.summary("model; DROP TABLE requests")
        self.store.record(event())
        self.assertEqual(self.store.summary()["rows"][0]["requests"], 1)

    def test_admin_command_is_permission_filtered(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())
        plugin = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        command = next(
            n
            for n in plugin.body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "opencode_usage"
        )
        decorators = [ast.unparse(d) for d in command.decorator_list]
        self.assertIn("filter.permission_type(filter.PermissionType.ADMIN)", decorators)
        self.assertIn("filter.command('opencode_usage')", decorators)


class MemoryPeer:
    closed = False

    def __init__(self):
        self.prompts = 0
        self.sessions = 0
        self.fail = False

    async def new_session(self, model):
        self.sessions += 1
        return "session"

    async def prompt(self, session, text, *, stats):
        self.prompts += 1
        stats.update(model_id="mock/grok-4.6", wire_bytes=100, output_bytes=0 if self.fail else 2)
        if self.fail:
            raise OpenCodeError("timeout")
        return {"text": "OK", "wire_bytes": 100}

    async def close(self):
        self.closed = True


class UsageIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = runtime._usage_store, metrics._usage_store
        self.context = SimpleNamespace(
            astrbot_config_mgr=SimpleNamespace(
                default_conf={
                    "agent": {"model": "oauth-model"},
                    "provider_settings": {"fallback": ["deepseek"]},
                    "provider_sources": [{"id": "oauth", "key": "DUMMY"}],
                }
            )
        )
        self.before = deepcopy(self.context.astrbot_config_mgr.default_conf)
        self.store = runtime.configure_usage(
            self.context, directory=Path(self.temp.name) / "cursor-data"
        )
        self.engine = Engine(settings_from({"model_id": "mock/grok-4.6"}))
        self.peer = MemoryPeer()
        self.engine.client = self.peer
        process = patch(
            "asyncio.create_subprocess_exec", side_effect=AssertionError("No CLI requests")
        )
        process.start()
        self.addCleanup(process.stop)

    async def asyncTearDown(self):
        await self.engine.close()
        self.store.close()
        runtime._usage_store, metrics._usage_store = self.old

    async def test_real_engine_success_and_failure_persist_with_actual_model_id(self):
        first = [{"role": "user", "content": "First"}]
        await self.engine.submit(
            first, "engine-key", stats_session=self.store.session_label("QQ/session")
        )
        self.peer.fail = True
        with self.assertRaises(OpenCodeError):
            await self.engine.submit(
                first
                + [{"role": "assistant", "content": "OK"}, {"role": "user", "content": "Next"}],
                "engine-key",
            )
        report = await asyncio.to_thread(self.store.summary, "model")
        self.assertEqual(report["rows"][0]["label"], "mock/grok-4.6")
        self.assertEqual(report["rows"][0]["codes"], {"ok": 1, "timeout": 1})
        self.assertEqual(self.peer.prompts, 2)
        self.assertEqual(self.peer.sessions, 1)
        self.assertEqual(self.engine.cfg["max_concurrency"], 2)
        self.assertEqual(self.context.astrbot_config_mgr.default_conf, self.before)

    async def test_cached_reply_counts_locally_without_extra_model_request(self):
        first = [{"role": "user", "content": "First"}]
        await self.engine.submit(first, "same")
        await self.engine.submit(first, "same")
        report = await asyncio.to_thread(self.store.summary, "model")
        counts = {r["label"]: r["requests"] for r in report["rows"]}
        self.assertEqual(counts, {"mock/grok-4.6": 1, "": 1})
        self.assertEqual(self.peer.prompts, 1)
        self.assertEqual(self.peer.sessions, 1)

    async def test_admin_query_does_not_change_defaults_or_call_models(self):
        metrics.record("request_finished", **{k: v for k, v in event().items() if k != "event"})
        text = await runtime.local_usage("model")
        self.assertIn("mock/grok-4.6", text)
        self.assertIn(DISCLAIMER, text)
        self.assertEqual(self.context.astrbot_config_mgr.default_conf, self.before)
        self.assertEqual(self.peer.prompts, 0)
        self.assertIs(
            runtime.configure_usage(self.context, directory=self.store.directory), self.store
        )

    async def test_statistics_write_failure_does_not_fail_model_response(self):
        with (
            patch.object(self.store, "_write", side_effect=OSError("PRIVATE_PATH_SENTINEL")),
            self.assertLogs("astrbot", level="WARNING") as logs,
        ):
            reply = await self.engine.submit([{"role": "user", "content": "First"}], "session")
            with self.assertRaises(OSError):
                await asyncio.to_thread(self.store.flush)
        self.assertEqual(reply, "OK")
        self.assertEqual(self.peer.prompts, 1)
        self.assertIn("write_failed", "\n".join(logs.output))
        self.assertNotIn("PRIVATE_PATH_SENTINEL", "\n".join(logs.output))

    async def test_acp_prompt_reports_confirmed_model_id_even_on_failure(self):
        client = ACPClient.__new__(ACPClient)
        client.turns = {}
        client.session_models = {"s": "mock/grok-4.6"}

        async def call(method, params):
            client.turns["s"]["wire_bytes"] = 70
            raise OpenCodeError("quota_exhausted")

        client.call = call
        stats = {}
        with self.assertRaises(OpenCodeError):
            await client.prompt("s", "Test", stats=stats)
        self.assertEqual(stats["model_id"], "mock/grok-4.6")
        self.assertEqual(stats["wire_bytes"], 70)


if __name__ == "__main__":
    unittest.main()
