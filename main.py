"""AstrBot entry point for the OpenCode CLI provider."""

import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .opencode_plugin import runtime
from .opencode_plugin.errors import OpenCodeError
from .opencode_plugin.provider import OpenCodeCLIProvider
from .opencode_plugin.web import UsagePage

__all__ = ["OpenCodeCLIPlugin", "OpenCodeCLIProvider"]


@register("astrbot_plugin_opencode", "drshenmiao-hash", "OpenCode CLI 纯文本 Provider", "0.2.0")
class OpenCodeCLIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        runtime.configure(config)
        runtime.configure_usage(context)
        self._usage_page = UsagePage()

    async def initialize(self):
        try:
            await runtime.stop_providers(self.context)
            await runtime.initialize(self.context)
        except OpenCodeError as exc:
            runtime.record_startup_failure(exc.code)
        except asyncio.TimeoutError:
            runtime.record_startup_failure("startup_timeout")
        self._usage_page.register(self.context)

    async def terminate(self):
        self._usage_page.close()
        await runtime.stop_providers(self.context)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("opencode_usage")
    async def opencode_usage(self, event: AstrMessageEvent, group: str = "total", key: str = ""):
        yield event.plain_result(await runtime.local_usage(group, key))
