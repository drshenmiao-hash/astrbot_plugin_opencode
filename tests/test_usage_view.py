"""Optional Chromium check of the packaged page against a local SQLite fixture."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from opencode_plugin.usage import UsageStore
from tests.test_usage import event

ASSETS = Path(__file__).resolve().parents[1] / "views" / "usage"
# Use the installed AstrBot bridge inside a sandboxed iframe. Only the parent
# transport is a fixture, so individual replies can be held or delivered late.
HOST = """
<iframe sandbox="allow-scripts" src="/index.html" style="border:0;width:100%"></iframe>
<script>
  function send(message) {
    document.querySelector("iframe").contentWindow.postMessage(
      {channel: "astrbot-plugin-page", ...message}, "*");
  }
  let posts = 0;
  window.addEventListener("message", async ({source, data}) => {
    if (source !== document.querySelector("iframe").contentWindow) return;
    if (data.kind === "ready" && !location.search) send({kind: "context", context: {}});
    if (data.kind !== "request") return;
    posts++;
    const report = await fetch("/report?days=" + data.body.days).then(r => r.json());
    const reply = {kind: "response", requestId: data.requestId, ok: true, data: report};
    if (window.hold) window.delayed = reply;
    else send(reply);
  });
</script>
"""


@unittest.skipUnless(os.getenv("OPENCODE_TEST_CHROMIUM"), "requires Chromium and Playwright")
class UsageViewTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright

        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = UsageStore(directory)
        self.addCleanup(self.store.close)
        driver = self.enterContext(sync_playwright())
        browser = driver.chromium.launch(executable_path=os.environ["OPENCODE_TEST_CHROMIUM"])
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": 360, "height": 800}, locale="zh-CN")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            parsed = urlsplit(route.request.url)
            if parsed.path == "/":
                route.fulfill(body=HOST, content_type="text/html")
            elif parsed.path == "/api/plugin/page/bridge-sdk.js":
                route.fulfill(
                    content_type="text/javascript",
                    body=files("astrbot.dashboard").joinpath("plugin_page_bridge.js").read_bytes(),
                )
            elif parsed.path == "/report":
                report = self.store.dashboard(int(parse_qs(parsed.query)["days"][0]))
                route.fulfill(json={**report, "runtime": {}, "concurrency_limit": 1})
            else:
                route.fulfill(path=ASSETS / parsed.path.lstrip("/"))

        page.route("http://usage.test/**", serve)
        self.page = page
        self.view = page.frame_locator("iframe")
        self.addCleanup(self.assertEqual, errors, [])

    def test_ranges_metrics_keyboard_rankings_and_empty_data(self):
        from playwright.sync_api import expect

        self.page.goto("http://usage.test/")
        view = self.view
        store = self.store
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

    def test_token_ranking_handles_coverage_denominators_and_dimension_switches(self):
        from playwright.sync_api import expect

        today = datetime.now(timezone.utc)
        page, view = self.page, self.view
        page.goto("http://usage.test/")
        view.get_by_role("radio", name="请求次数").focus()
        view.get_by_role("radio", name="请求次数").press("ArrowRight")
        expect(view.get_by_role("radio", name="Token 消耗")).to_be_checked()
        expect(view.locator("#groups-empty")).to_have_text("该时段暂无请求")
        self.store.record(event("missing", model_id="unmeasured"), at=today)
        view.locator("#refresh").click()
        expect(view.locator("#groups-empty")).to_have_text("该时段的请求没有 Token 计数")
        self.store.record(
            event(
                "zero",
                model_id="zero",
                token_total=0,
                token_input=0,
                token_output=0,
                token_cached=0,
            ),
            at=today,
        )
        view.locator("#refresh").click()
        expect(view.locator(".rank-count")).to_have_text("0 Token")
        expect(view.locator(".rank-share")).to_have_text("—")
        self.assertEqual(view.locator(".rank-fill").evaluate("e => e.style.width"), "0%")
        for i in range(6):
            self.store.record(
                event(
                    str(i),
                    model_id=f"small/{i}",
                    token_total=10,
                    token_input=7,
                    token_output=3,
                    token_cached=2,
                ),
                at=today,
            )
        self.store.record(
            event(
                "large",
                model_id="rare/large",
                session="large-session",
                code="timeout",
                token_total=60,
                token_input=45,
                token_output=15,
                token_cached=5,
            ),
            at=today,
        )
        self.store.record(
            event(
                "older",
                model_id="old/large",
                token_total=120,
                token_input=80,
                token_output=40,
                token_cached=10,
            ),
            at=today - timedelta(days=9),
        )
        view.locator("#refresh").click()
        expect(view.locator(".rank-name").first).to_have_text("old/large")
        view.locator("#range").select_option("7")
        expect(view.locator(".rank-name").first).to_have_text("rare/large")
        expect(view.locator(".rank")).to_have_count(5)
        expect(view.locator(".rank-count").first).to_have_text("60 Token")
        expect(view.locator(".rank-share").first).to_have_text("50%")
        expect(view.locator("#rank-basis")).to_contain_text("8 / 9")
        view.get_by_text("查看分组明细（最多 20 项）").click()
        expect(view.locator("#groups tr").first.locator("td")).to_have_text(
            ["rare/large", "60", "45", "15", "5", "1 / 1"]
        )
        expect(view.locator("#groups tr")).to_have_count(8)
        view.locator("#dimension").select_option("code")
        expect(view.locator(".rank-count")).to_have_text(["60 Token", "60 Token"])
        view.locator("#dimension").select_option("session")
        expect(view.locator("#groups")).to_contain_text("large-session")
        self.assertTrue(view.locator("html").evaluate("e => e.scrollWidth <= e.clientWidth"))
        view.get_by_role("radio", name="请求次数").check()
        expect(view.locator(".rank-count").first).to_have_text("8 次")
        expect(view.locator("#group-columns")).to_contain_text("会话复用")

    def test_rare_outcomes_do_not_round_to_zero_or_unanimous_success(self):
        from playwright.sync_api import expect

        older = datetime.now(timezone.utc) - timedelta(days=14)
        for i in range(2499):
            self.store.record(event(str(i)), at=older)
        self.store.record(event("timeout", code="timeout"))
        self.page.goto("http://usage.test/")
        expect(self.view.locator("#success")).to_have_text(">99.9%")
        self.view.locator("#dimension").select_option("code")
        expect(self.view.locator(".rank-share")).to_have_text([">99.9%", "<0.1%"])
        expect(self.view.locator("#failed")).to_have_text("失败 1 次")
        self.view.locator("#range").select_option("7")
        expect(self.view.locator("#success")).to_have_text("0%")
        expect(self.view.locator(".rank-share")).to_have_text("100%")

    def test_bridge_timeouts_recover_and_late_replies_cannot_replace_new_data(self):
        from playwright.sync_api import expect

        self.store.record(event("recent"))
        self.store.record(event("older"), at=datetime.now(timezone.utc) - timedelta(days=14))
        page, view = self.page, self.view
        page.clock.install()
        page.goto("http://usage.test/?hold-ready")
        refresh = view.locator("#refresh")
        expect(refresh).to_be_disabled()
        page.clock.fast_forward(20_000)
        expect(refresh).to_be_enabled()
        expect(view.locator("#error")).to_contain_text("超时")
        page.evaluate("send({kind: 'context', context: {}})")
        refresh.click()
        expect(view.locator("#requests")).to_have_text("2")
        self.assertEqual(page.evaluate("posts"), 1)

        page.evaluate("window.hold = true")
        view.locator("#range").select_option("7")
        page.wait_for_function("window.delayed !== undefined")
        expect(view.locator("#dashboard")).to_be_hidden()
        page.clock.fast_forward(20_000)
        expect(refresh).to_be_enabled()
        expect(view.locator("#range")).to_be_enabled()
        expect(view.locator("#error")).to_contain_text("超时")

        page.evaluate("window.hold = false")
        self.store.record(event("second recent"))
        refresh.click()
        expect(view.locator(".bar")).to_have_count(7)
        expect(view.locator("#requests")).to_have_text("2")
        expect(view.locator("#error")).to_be_hidden()
        # postMessage is asynchronous. Wait until the iframe has handled the
        # delayed 1-request response before checking the newer 2-request total.
        page.frames[1].evaluate("""
          () => { window.lateReceived = new Promise(resolve =>
            window.addEventListener("message", () => resolve(true), {once: true})); }
        """)
        page.evaluate("send(window.delayed)")
        page.frames[1].evaluate("window.lateReceived")
        expect(view.locator("#requests")).to_have_text("2")
