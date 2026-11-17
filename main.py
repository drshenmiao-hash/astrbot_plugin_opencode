"""AstrBot entry point for the OpenCode CLI provider."""

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .opencode_plugin import runtime
from .opencode_plugin.provider import OpenCodeCLIProvider

__all__ = ["OpenCodeCLIPlugin", "OpenCodeCLIProvider"]


@register("astrbot_plugin_opencode", "drshenmiao-hash", "OpenCode CLI 纯文本 Provider", "0.1.1")
class OpenCodeCLIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        runtime.configure(config)
        runtime.configure_usage(context)

    async def initialize(self):
        await runtime.initialize(self.context)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("opencode_usage")
    async def opencode_usage(self, event: AstrMessageEvent, group: str = "total", key: str = ""):
        yield event.plain_result(await runtime.local_usage(group, key))
