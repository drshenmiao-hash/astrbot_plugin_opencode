"""Optional Chromium check of the packaged page against a local SQLite fixture."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from opencode_plugin.usage import UsageStore
from tests.test_usage import event

ASSETS = Path(__file__).resolve().parents[1] / "views" / "usage"


@unittest.skipUnless(os.getenv("OPENCODE_TEST_CHROMIUM"), "requires Chromium and Playwright")
class UsageViewTests(unittest.TestCase):
    def test_ranges_metrics_keyboard_rankings_and_empty_data(self):
        from playwright.sync_api import expect, sync_playwright

        directory = self.enterContext(tempfile.TemporaryDirectory())
        store = UsageStore(directory)
        self.addCleanup(store.close)
        driver = self.enterContext(sync_playwright())
        browser = driver.chromium.launch(executable_path=os.environ["OPENCODE_TEST_CHROMIUM"])
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": 360, "height": 800}, locale="zh-CN")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlsplit(route.request.url)
            if parsed.path == "/":
                route.fulfill(
                    body='<iframe src="/index.html" style="border:0;width:100%"></iframe>',
                    content_type="text/html",
                )
            elif parsed.path == "/api/plugin/page/bridge-sdk.js":
                route.fulfill(
                    content_type="text/javascript",
                    body="""
                    window.AstrBotPluginView = { ready: async () => {},
                      apiPost: (_, body) => fetch('/report?days=' + body.days).then(r => r.json()) };
                """,
                )
            elif parsed.path == "/report":
                report = store.dashboard(int(parse_qs(parsed.query)["days"][0]))
                route.fulfill(
                    content_type="application/json",
                    body=json.dumps({**report, "runtime": {}, "concurrency_limit": 1}),
                )
            else:
                route.fulfill(path=ASSETS / parsed.path.lstrip("/"))

        page.route("http://usage.test/**", serve)
        page.goto("http://usage.test/")
        view = page.frame_locator("iframe")
        expect(view.locator("#chart-empty")).to_have_text("该时段暂无请求")
        today = datetime.now(timezone.utc)
        for i in range(24):
            store.record(
                event(str(i), model_id=f"fixture/{i:02}", queue_ms=11), at=today - timedelta(days=1)
            )
        view.locator("#refresh").click()
        expect(view.locator(".rank")).to_have_count(5)
        expect(view.locator(".rank-share").first).to_have_text("4.2%")
        view.locator("#metric").select_option("tokens")
        expect(view.locator("#chart-empty")).to_have_text("该时段的请求没有 Token 计数")
        store.record(
            event("zero", token_total=0, token_input=0, token_output=0, token_cached=0), at=today
        )
        view.locator("#refresh").click()
        expect(view.locator(".recorded-zero")).to_have_count(1)
        expect(view.locator(".bar.missing")).to_have_count(1)
        picker = view.locator("#day-picker")
        picker.press("End")
        expect(view.locator("#selected-values")).to_contain_text("0 Token")
        picker.press("ArrowLeft")
        expect(picker).to_have_attribute(
            "aria-valuetext", (today - timedelta(days=1)).date().isoformat() + " · 未记录 Token"
        )
        view.locator("#metric").select_option("duration")
        expect(view.locator("#selected-values")).to_have_text("平均耗时 100 ms · 平均排队 11 ms")
        view.locator("#dimension").select_option("session")
        expect(view.locator(".rank-share").first).to_have_text("100%")
        for days in (90, 7):
            view.locator("#range").select_option(str(days))
            expect(view.locator(".bar")).to_have_count(days)
            expect(view.locator("#selected-values")).to_contain_text("11 ms")
        picker.press("Home")
        expect(view.locator("#selected-values")).to_have_text("当日无请求")
        self.assertTrue(view.locator("html").evaluate("e => e.scrollWidth <= e.clientWidth"))
        self.assertEqual(errors, [])
