"""Exercise the installation view and official bridge in Chromium."""

import os
import unittest
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).resolve().parents[1] / "views" / "environment"


@unittest.skipUnless(os.getenv("OPENCODE_TEST_CHROMIUM"), "requires Chromium and Playwright")
class EnvironmentViewTests(unittest.TestCase):
    def test_loading_failure_recheck_and_statuses(self):
        from playwright.sync_api import expect, sync_playwright

        host = """
        <iframe sandbox="allow-scripts" src="/index.html" style="width:100%;height:90vh;border:0"></iframe>
        <script>
        window.posts = [];
        window.addEventListener("message", ({data, source}) => {
          const send = msg => source.postMessage({channel: "astrbot-plugin-page", ...msg}, "*");
          if (data.kind === "ready") send({kind: "context", context: {}});
          if (data.kind !== "request") return;
          posts.push(data);
          window.respond = result => send({kind: "response", requestId: data.requestId, ...result});
        });
        </script>"""
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=os.environ["OPENCODE_TEST_CHROMIUM"])
            page = browser.new_page(viewport={"width": 360, "height": 800})

            def serve(route):
                path = urlsplit(route.request.url).path
                if path == "/":
                    route.fulfill(body=host, content_type="text/html")
                elif path.endswith("bridge-sdk.js"):
                    route.fulfill(
                        content_type="text/javascript",
                        body=files("astrbot.dashboard")
                        .joinpath("plugin_page_bridge.js")
                        .read_bytes(),
                    )
                else:
                    route.fulfill(path=ASSETS / path.lstrip("/"))

            page.route("http://env.test/**", serve)
            page.goto("http://env.test")
            frame = page.frame_locator("iframe")
            button = frame.get_by_role("button", name="重新检查环境")
            expect(button).to_be_disabled()
            expect(frame.get_by_text("正在检查", exact=True)).to_have_count(5)
            page.wait_for_function("posts.length === 1")
            self.assertEqual(page.evaluate("posts[0].endpoint"), "environment/check")
            self.assertFalse(page.evaluate("posts[0].body.refresh"))
            page.evaluate("respond({ok:false, error:'private-do-not-echo'})")
            expect(frame.get_by_text("未验证", exact=True)).to_have_count(5)
            self.assertNotIn("private-do-not-echo", frame.locator("body").inner_text())
            button.click()
            page.wait_for_function("posts.length === 2")
            self.assertTrue(page.evaluate("posts[1].body.refresh"))
            rows = [
                {"title": "运行环境", "status": "pass"},
                {"title": "OpenCode CLI", "status": "action", "detail": "请设置 cli_binary"},
                {
                    "title": "bubblewrap 沙盒",
                    "status": "disabled",
                    "detail": "工具权限不能替代进程隔离",
                },
                {"title": "服务商认证", "status": "unknown"},
                {"title": "模型与 ACP", "status": "pass", "facts": {"所选模型": "p/m"}},
            ]
            page.evaluate(
                "data => respond({ok:true,data})",
                {
                    "items": rows,
                    "checked_at": "2026-11-26T00:00:00Z",
                    "next_step": "请设置 cli_binary",
                },
            )
            expect(frame.locator("#next")).to_have_text("请设置 cli_binary")
            expect(frame.get_by_text("未启用", exact=True)).to_be_visible()
            expect(frame.get_by_text("p/m", exact=True)).to_be_visible()
            self.assertEqual(
                frame.locator("main").evaluate("e => e.scrollWidth <= e.clientWidth"), True
            )
            browser.close()
