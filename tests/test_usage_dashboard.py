"""Coverage for bounded statistics and the authenticated dashboard route."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from opencode_plugin import web
from opencode_plugin.usage import UsageStore
from tests.test_usage import event


class DashboardQueries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UsageStore(self.tmp.name)
        self.addCleanup(self.store.close)

    def test_range_counts_and_populates_each_utc_day(self):
        now = datetime.now(timezone.utc)
        self.store.record(event("today", code="ok", model_id="mock/alpha"), at=now)
        self.store.record(
            event("yesterday", code="timeout", model_id="mock/alpha"), at=now - timedelta(days=1)
        )
        self.store.record(
            event("prior", code="ok", model_id="mock/beta"), at=now - timedelta(days=8)
        )
        self.store.record(event("future", code="ok"), at=now + timedelta(days=1))
        short = self.store.dashboard(7)
        long = self.store.dashboard(30)
        self.assertEqual(short["totals"]["requests"], 2)
        self.assertEqual(short["totals"]["failed"], 1)
        self.assertEqual(short["trend"][-2]["failed"], 1)
        self.assertEqual(short["trend"][-1]["label"], now.date().isoformat())
        self.assertEqual(len(short["trend"]), 7)
        self.assertEqual(long["totals"]["requests"], 3)
        self.assertEqual(
            [row["label"] for row in long["groups"]["model"]], ["mock/alpha", "mock/beta"]
        )
        self.assertEqual(sum(row["requests"] for row in long["trend"]), 3)

    def test_daily_metrics_distinguish_missing_zero_and_partial_coverage(self):
        today = datetime.now(timezone.utc)
        self.store.record(event("missing"), at=today - timedelta(days=2))
        self.store.record(
            event("zero", token_total=0, token_input=0, token_output=0, token_cached=0),
            at=today - timedelta(days=1),
        )
        self.store.record(event("legacy", duration_ms=20, queue_ms=0), at=today)
        self.store.record(
            event(
                "measured",
                duration_ms=81,
                queue_ms=11,
                token_total=12,
                token_input=10,
                token_output=2,
                token_cached=0,
            ),
            at=today,
        )
        empty, missing, zero, partial = self.store.dashboard(7)["trend"][-4:]
        self.assertIsNone(empty["duration_avg_ms"])
        self.assertIsNone(empty["queue_avg_ms"])
        self.assertEqual((missing["token_total"], missing["token_attempts"]), (None, 0))
        self.assertEqual((zero["token_total"], zero["token_attempts"]), (0, 1))
        self.assertEqual((partial["token_total"], partial["token_attempts"]), (12, 1))
        self.assertEqual((partial["duration_avg_ms"], partial["queue_avg_ms"]), (50.5, 5.5))

    def test_empty_database_and_failed_write_still_readable(self):
        self.assertEqual(self.store.dashboard(90)["totals"]["requests"], 0)
        self.store.record(event("stored"))
        self.store.flush()
        with patch.object(self.store, "_write", side_effect=OSError("fake disk failure")):
            pending = self.store.record(event("dropped"))
            with self.assertRaises(OSError):
                pending.result(timeout=2)
        self.assertEqual(self.store.dashboard(7)["totals"]["requests"], 1)
        summary = self.store.summary()
        self.assertEqual(summary["rows"][0]["requests"], 1)
        self.assertEqual(summary["write_failures"], 1)

    def test_existing_store_keeps_legacy_rows_and_aggregates_recorded_counters(self):
        self.store.record(event("legacy"))
        self.store.close()
        connection = sqlite3.connect(self.store.path)
        try:
            for key in ("token_total", "token_input", "token_output", "token_cached"):
                connection.execute(f"ALTER TABLE requests DROP COLUMN {key}")
            connection.commit()
        finally:
            connection.close()
        migrated = UsageStore(self.tmp.name)
        self.addCleanup(migrated.close)
        no_tokens_yet = migrated.dashboard(7)
        self.assertEqual(no_tokens_yet["totals"]["requests"], 1)
        self.assertIsNone(no_tokens_yet["totals"]["token_total"])
        migrated.record(
            event("success", token_total=13, token_input=10, token_output=3, token_cached=5)
        )
        migrated.record(
            event(
                "zero",
                model_id="mock/other",
                code="cli_failed",
                token_total=0,
                token_input=0,
                token_output=0,
                token_cached=0,
            )
        )
        report = migrated.dashboard(7)
        self.assertEqual(report["totals"]["token_attempts"], 2)
        self.assertEqual(report["totals"]["token_total"], 13)
        self.assertEqual(report["totals"]["token_cached"], 5)
        self.assertEqual(report["trend"][-1]["token_total"], 13)
        self.assertEqual([row["token_total"] for row in report["groups"]["model"]], [13, 0])

    def test_group_output_is_bounded_and_excludes_sensitive_fields(self):
        for i in range(25):
            self.store.record(event(str(i), session=f"anonymous-{i}"))
        report = self.store.dashboard(7)
        self.assertEqual(report["totals"]["requests"], 25)
        self.assertEqual(len(report["groups"]["session"]), 20)
        self.assertNotIn("request_id", json.dumps(report))
        with self.assertRaises(ValueError):
            self.store.dashboard(999)


class DashboardAccess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = web.UsagePage()
        self.context = SimpleNamespace(register_web_api=Mock())
        self.handler.register(self.context)
        self.request = SimpleNamespace(username="admin", json=AsyncMock(return_value={"days": 30}))
        shim = ModuleType("astrbot.api.web")
        shim.request = self.request
        shim.json_response = lambda data, status_code=200, headers=None: (
            data,
            status_code,
            headers,
        )
        workspace = ModuleType("astrbot.core.workspace")
        workspace.API_KEY_USERNAME_PREFIX = "api_key:"
        self.enterContext(
            patch.dict(
                "sys.modules", {"astrbot.api.web": shim, "astrbot.core.workspace": workspace}
            )
        )
        self.read = self.enterContext(patch.object(web, "usage_dashboard", new_callable=AsyncMock))

    async def test_guest_and_api_key_cannot_read_usage(self):
        for user in (None, "", "api_key:test"):
            self.request.username = user
            _, status, _ = await self.handler.handle()
            self.assertEqual(status, 403)
        self.read.assert_not_awaited()

    async def test_managed_route_uses_dashboard_session_and_never_caches(self):
        self.read.return_value = {"totals": {"requests": 1}}
        data, status, headers = await self.handler.handle()
        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["totals"]["requests"], 1)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.read.assert_awaited_once_with(30)
        self.assertEqual(self.context.register_web_api.call_args.args[0], web.ROUTE)
        self.assertEqual(self.context.register_web_api.call_args.args[2], ["POST"])

    async def test_invalid_body_and_stopped_plugin_do_not_query_data(self):
        for body in (None, [], {"days": True}, {"days": "30"}, {"days": 365}):
            self.request.json.return_value = body
            _, status, _ = await self.handler.handle()
            self.assertEqual(status, 400)
        self.handler.close()
        self.request.json.return_value = {"days": 30}
        _, status, _ = await self.handler.handle()
        self.assertEqual(status, 503)
        self.read.assert_not_awaited()

    async def test_storage_failure_is_sanitized(self):
        self.read.side_effect = OSError("/private/opencode/storage")
        with self.assertLogs("astrbot", level="WARNING") as logs:
            data, status, _ = await self.handler.handle()
        self.assertEqual(status, 503)
        self.assertNotIn("/private", data["message"])
        self.assertNotIn("/private", " ".join(logs.output))
