"""AstrBot entry point for the OpenCode CLI provider."""

import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .opencode_plugin import __version__, runtime
from .opencode_plugin.errors import OpenCodeError
from .opencode_plugin.provider import OpenCodeCLIProvider, register_adapter, unregister_adapter
from .opencode_plugin.web import EnvironmentPage, UsagePage

__all__ = ["OpenCodeCLIPlugin", "OpenCodeCLIProvider"]


class OpenCodeCLIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._config_error = None
        try:
            runtime.configure(config)
        except OpenCodeError as exc:
            runtime.configure({})
            self._config_error = exc.code
        self._usage_page = UsagePage()
        self._environment_page = EnvironmentPage(config, self._config_error)
        self._adapter_type = None

    async def initialize(self):
        self._adapter_type = register_adapter()
        try:
            self._environment_page.register(self.context)
            await runtime.open_usage(self.context)
            try:
                await runtime.stop_providers(self.context, detach=bool(self._config_error))
                if self._config_error:
                    raise OpenCodeError(self._config_error)
                await runtime.initialize(self.context)
            except OpenCodeError as exc:
                runtime.record_startup_failure(exc.code)
            except asyncio.TimeoutError:
                runtime.record_startup_failure("startup_timeout")
            self._usage_page.register(self.context)
        except BaseException:
            await self.terminate()
            raise

    async def terminate(self):
        if self._adapter_type is None:
            return
        self._environment_page.close()
        self._usage_page.close()
        unregister_adapter(self._adapter_type)
        self._adapter_type = None
        try:
            try:
                await self._environment_page.checks.close()
            finally:
                await runtime.stop_validation()
        finally:
            try:
                await runtime.stop_providers(self.context, detach=True)
            finally:
                await runtime.close_usage(self.context)

    @filter.command("opencode_source")
    async def opencode_source(self, event: AstrMessageEvent):
        """Show the license and corresponding source for this bot plugin."""
        yield event.plain_result(
            f"astrbot_plugin_opencode v{__version__} · AGPL-3.0\n"
            f"源码：https://github.com/drshenmiao-hash/astrbot_plugin_opencode/tree/v{__version__}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("opencode_usage")
    async def opencode_usage(self, event: AstrMessageEvent, group: str = "total", key: str = ""):
        """Show plugin-local OpenCode usage statistics."""
        yield event.plain_result(await runtime.local_usage(group, key))
