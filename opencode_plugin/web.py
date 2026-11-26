"""Read-only usage endpoint carried by AstrBot's authenticated page bridge."""

from astrbot.api import logger

from .runtime import usage_dashboard
from .usage import DASHBOARD_RANGES

ROUTE = "/astrbot_plugin_opencode/usage/dashboard"
_NO_CACHE = {"Cache-Control": "no-store"}


class _Page:
    def __init__(self):
        self.active = False
        self._context = None

    def register(self, context):
        self._context = context
        context.register_web_api(self.route, self.handle, ["POST"], self.title)
        self.active = True

    def close(self):
        self.active = False
        context, self._context = self._context, None
        apis = getattr(context, "registered_web_apis", None)
        if isinstance(apis, list):
            # AstrBot 4.29/4.30 replace routes on registration and expose no
            # unregister method. Remove only this instance's handler.
            apis[:] = [api for api in apis if not (api[0] == self.route and api[1] == self.handle)]


class UsagePage(_Page):
    route = ROUTE
    title = "OpenCode 用量与运行状态"

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


class EnvironmentPage(_Page):
    route = "/astrbot_plugin_opencode/environment/check"
    title = "OpenCode 环境与配置"

    def __init__(self, config, config_error):
        super().__init__()
        from .environment import EnvironmentChecks

        self.checks = EnvironmentChecks(config, config_error)

    async def handle(self):
        from astrbot.api.web import json_response, request
        from astrbot.core import DEMO_MODE
        from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

        def error(message, status):
            return json_response(
                {"status": "error", "message": message}, status_code=status, headers=_NO_CACHE
            )

        if not self.active:
            return error("插件已停止，请刷新插件详情页。", 503)
        username = request.username
        dashboard = self._context.get_config().get("dashboard", {})
        if (
            DEMO_MODE
            or not isinstance(username, str)
            or not username
            or username.startswith(API_KEY_USERNAME_PREFIX)
            or not isinstance(dashboard, dict)
            or username != dashboard.get("username")
        ):
            return error("仅 AstrBot 管理面板管理员可查看环境诊断。", 403)
        body = await request.json(default=None)
        if (
            not isinstance(body, dict)
            or body.keys() - {"refresh"}
            or type(body.get("refresh", False)) is not bool
        ):
            return error("无效的环境检查请求。", 400)
        try:
            report = await self.checks.read(body.get("refresh", False))
        except Exception:
            logger.warning("opencode_environment code=check_unavailable")
            return error("本次检查未完成，请稍后重新检查。", 503)
        return json_response({"status": "success", "data": report}, headers=_NO_CACHE)
