"""Read-only usage endpoint carried by AstrBot's authenticated page bridge."""

import logging

from .runtime import usage_dashboard
from .usage import DASHBOARD_RANGES

ROUTE = "/astrbot_plugin_opencode/usage/dashboard"
_NO_CACHE = {"Cache-Control": "no-store"}
logger = logging.getLogger("astrbot")


class UsagePage:
    def __init__(self):
        self.active = False

    def register(self, context):
        self.active = True
        context.register_web_api(ROUTE, self.handle, ["POST"], "OpenCode 用量与运行状态")

    def close(self):
        self.active = False

    async def handle(self):
        from astrbot.api.web import json_response, request
        from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

        def error(message, status_code):
            return json_response(
                {"status": "error", "message": message},
                status_code=status_code,
                headers=_NO_CACHE,
            )

        username = request.username
        if not username or username.startswith(API_KEY_USERNAME_PREFIX):
            return error("请登录 AstrBot 管理面板后查看。", 403)
        if not self.active:
            return error("OpenCode 插件当前已停止。", 503)
        body = await request.json(default=None)
        days = body.get("days") if isinstance(body, dict) else None
        if type(days) is not int or days not in DASHBOARD_RANGES:
            return error("请选择近 7、30 或 90 天。", 400)
        try:
            report = await usage_dashboard(days)
        except Exception:
            logger.warning("opencode_usage_web code=read_failed")
            return error("读取本地统计失败，请检查插件数据目录后重试。", 503)
        return json_response({"status": "success", "data": report}, headers=_NO_CACHE)
