"""Local executables, private auth fixtures, and lifetime of one admin diagnostic."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from opencode_plugin import environment as env
from opencode_plugin import runtime
from opencode_plugin.errors import OpenCodeError
from opencode_plugin.settings import settings_from


class EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.enterContext(patch.dict(os.environ, PATH=str(self.bin)))
        self.cli = self.bin / "opencode"
        self.auth = self.root / "auth.json"
        self.config = {
            "cli_binary": str(self.cli),
            "model_id": "fixture/test",
            "auth_path": str(self.auth),
            "sandbox_enabled": False,
        }
        self.script(self.cli, 'test "$#" = 1 && test "$1" = --version && echo 1.19.6')
        self.auth.write_text(
            json.dumps({"fixture": {"type": "api", "key": "credential-do-not-display"}})
        )
        self.auth.chmod(0o600)
        for key in ("_settings", "_available_models", "_startup_failure"):
            self.enterContext(patch.object(runtime, key, None))
        runtime.configure(self.config)

    def script(self, path, command):
        path.write_text("#!/bin/sh\n" + command + "\n")
        path.chmod(0o700)

    async def report(self):
        checks = env.EnvironmentChecks(self.config)
        self.addAsyncCleanup(checks.close)
        result = await checks.read()
        self.assertNotIn("credential-do-not-display", json.dumps(result))
        self.assertNotIn(str(self.auth), json.dumps(result))
        return {r["id"]: r for r in result["items"]}

    async def test_paths_config_precedence_fallback_and_no_mutation(self):
        alternate = self.root / "configured"
        self.script(alternate, "echo 1.20.0")
        self.config["cli_binary"] = str(alternate)
        cli = (await self.report())["cli"]
        self.assertEqual(cli["facts"]["版本"], "1.20.0")
        self.assertEqual(cli["facts"]["PATH 候选"], str(self.cli))
        self.assertEqual(cli["status"], "unknown")
        alternate.chmod(0o600)
        cli = (await self.report())["cli"]
        self.assertEqual(cli["status"], "action")
        self.assertEqual(cli["facts"]["配置检查"], "不可执行")
        self.assertEqual(cli["facts"]["本次检测路径"], str(self.cli))
        self.assertEqual(cli["facts"]["版本"], "1.19.6")
        self.assertEqual(self.config["cli_binary"], str(alternate))
        alternate.unlink()
        self.cli.unlink()
        result = await env.EnvironmentChecks(self.config).read()
        self.assertIn("安装 opencode", result["next_step"])
        self.assertEqual(result["items"][1]["status"], "action")

    async def test_version_limits_errors_and_unverified_build(self):
        for command, status in (
            ("echo 1.19.6", "pass"),
            ("echo 1.19.6-beta.1", "unknown"),
            ("echo credential-do-not-display", "unknown"),
            ("echo credential-do-not-display >&2; exit 1", "action"),
            ("head -c 10000 /dev/zero", "unknown"),
            ("exec sleep 20", "unknown"),
        ):
            with self.subTest(command=command), patch.object(env, "PROBE_TIMEOUT", 0.45):
                self.script(self.cli, command)
                self.assertEqual((await self.report())["cli"]["status"], status)

    async def test_sandbox_unavailable_and_disabled_never_launch_wrong_mode(self):
        bwrap = self.bin / "bwrap"
        self.config["bubblewrap_binary"] = str(bwrap)
        self.config["sandbox_enabled"] = True
        self.script(bwrap, 'test "$1" = --version')
        marker = self.root / "executed"
        self.script(self.cli, f'touch "{marker}"\necho 1.19.6')
        report = await self.report()
        self.assertEqual(report["sandbox"]["facts"]["版本命令"], "执行成功")
        self.assertEqual(report["sandbox"]["facts"]["隔离能力"], "不可用")
        self.assertEqual(report["cli"]["status"], "unknown")
        self.assertFalse(marker.exists())
        bwrap.unlink()
        self.assertEqual((await self.report())["sandbox"]["status"], "action")
        self.config["sandbox_enabled"] = False
        with patch.object(env, "_high_privilege", return_value=True):
            report = await self.report()
        self.assertEqual(report["sandbox"]["status"], "disabled")
        self.assertIn("低权限", report["sandbox"]["detail"])
        self.assertTrue(marker.exists())

    async def test_secure_auth_status(self):
        def check(status, snippet):
            row = env.auth_check(self.config, "fixture/test")
            self.assertEqual(row["status"], status)
            self.assertIn(snippet, row["detail"])
            self.assertNotIn("credential-do-not-display", json.dumps(row))

        check("pass", "尚未验证")
        self.auth.chmod(0o644)
        check("action", "所有者")
        self.auth.chmod(0o000)
        check("action", "所有者")
        self.auth.chmod(0o600)
        for data in ("bad-json", "[]", "[" * 3000, " " * 262145):
            self.auth.write_text(data)
            check("action", "JSON")
        for data in ({}, {"fixture": {"type": "oauth"}}, {"fixture": {"type": "api", "key": " "}}):
            self.auth.write_text(json.dumps(data))
            check("action", "API Key")
        with patch("os.geteuid", return_value=self.auth.stat().st_uid + 1):
            check("action", "所有者")
        self.auth.unlink()
        check("action", "未找到")
        self.auth.symlink_to(self.cli)
        check("action", "所有者")
        self.auth.unlink()
        os.mkfifo(self.auth)
        check("action", "所有者")

    async def test_model_uses_only_catalog_for_this_configuration(self):
        with patch.object(runtime, "_fetch_available_models", new_callable=AsyncMock) as fetch:
            for value in ("", "broken", "fixture/auto"):
                self.config["model_id"] = value
                self.assertEqual((await self.report())["model"]["status"], "action")
            self.config["model_id"] = "fixture/test"
            checks = env.EnvironmentChecks(self.config)
            self.addAsyncCleanup(checks.close)
            self.assertEqual((await checks.read())["items"][-1]["status"], "unknown")
            fetch.assert_not_awaited()
            fetch.return_value = ["fixture/test"]
            await runtime.validate_configured_model()
            self.assertEqual((await checks.read())["items"][-1]["status"], "pass")
            self.config["model_id"] = "other/model"
            self.assertEqual((await self.report())["model"]["status"], "unknown")
            fetch.assert_awaited_once()
        self.assertTrue(settings_from({})["sandbox_enabled"])
        for val in (None, 0, 1, "false", [], {}):
            with self.assertRaises(OpenCodeError):
                settings_from({"sandbox_enabled": val})

    async def test_coalescing_cancel_and_reload_reap_actual_child(self):
        marker = self.root / "pid"
        self.script(self.cli, f'echo $$ > "{marker}"\nexec /bin/sleep 30')
        checks = env.EnvironmentChecks(self.config)
        self.addAsyncCleanup(checks.close)
        with patch.object(checks, "_collect", wraps=checks._collect) as collect:
            calls = [asyncio.create_task(checks.read(True)) for _ in range(8)]
            async with asyncio.timeout(3):
                while not marker.is_file():
                    await asyncio.sleep(0.01)
            pid = int(marker.read_text())
            calls[0].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await calls[0]
            os.kill(pid, 0)
            await checks.close()
            results = await asyncio.gather(*calls[1:], return_exceptions=True)
            self.assertTrue(
                all(isinstance(r, OpenCodeError) and r.code == "closed" for r in results)
            )
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            collect.assert_awaited_once()
        self.script(self.cli, "echo 1.19.6")
        replacement = env.EnvironmentChecks(self.config)
        self.addAsyncCleanup(replacement.close)
        with patch.object(replacement, "_collect", wraps=replacement._collect) as collect:
            first = await replacement.read()
            self.assertEqual(await replacement.read(True), first)
            replacement.checked -= 3
            self.assertEqual(await replacement.read(), first)
            await replacement.read(True)
            self.assertEqual(collect.await_count, 2)

    async def test_independent_failure_preserves_other_results(self):
        with patch.object(env, "_probe", side_effect=OSError("credential-do-not-display")):
            rows = await self.report()
        self.assertEqual(rows["cli"]["status"], "unknown")
        self.assertEqual(rows["auth"]["status"], "pass")


class EnvironmentAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_host_admin_can_run_checks_and_failures_are_redacted(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from astrbot.api.web import bind_request_context
        from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

        from opencode_plugin.web import EnvironmentPage

        page = EnvironmentPage({}, None)
        page.register(
            SimpleNamespace(
                register_web_api=Mock(), get_config=lambda: {"dashboard": {"username": "operator"}}
            )
        )
        self.addAsyncCleanup(page.checks.close)
        request = SimpleNamespace(
            username="operator", json=AsyncMock(return_value={"refresh": False})
        )
        with (
            bind_request_context(request),
            patch.object(page.checks, "read", new_callable=AsyncMock) as read,
        ):
            for user in (None, "", "guest", "admin", API_KEY_USERNAME_PREFIX + "operator"):
                request.username = user
                self.assertEqual((await page.handle()).status_code, 403)
            request.username = "operator"
            with patch("astrbot.core.DEMO_MODE", True):
                self.assertEqual((await page.handle()).status_code, 403)
            read.assert_not_awaited()
            for body in (
                None,
                [],
                {"refresh": 1},
                {"cli_binary": "/usr/bin/true"},
                {"username": "operator"},
            ):
                request.json.return_value = body
                self.assertEqual((await page.handle()).status_code, 400)
            read.assert_not_awaited()
            request.json.return_value = {"refresh": True}
            read.return_value = {"items": []}
            response = await page.handle()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            read.assert_awaited_once_with(True)
            read.side_effect = RuntimeError("private-key-fixture")
            with self.assertLogs("astrbot", level="WARNING") as logs:
                response = await page.handle()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"private-key-fixture", response.body)
            self.assertNotIn("private-key-fixture", str(logs.output))
            page.close()
            self.assertEqual((await page.handle()).status_code, 503)
