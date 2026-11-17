# SPDX-License-Identifier: AGPL-3.0-only
# Adapter registration adapted from astrbot_plugin_openai_oauth; see NOTICE.
"""A native AstrBot chat provider backed by the official OpenCode CLI."""

import asyncio
import hashlib
import secrets

from astrbot.api.event import MessageChain
from astrbot.api.provider import LLMResponse, Provider
from astrbot.core.provider.register import provider_cls_map, register_provider_adapter

from . import runtime
from .errors import OpenCodeError, UnsupportedFeatureError, retry_stream_errors
from .transcript import normalize_messages

_SESSION_SALT = secrets.token_bytes(32)

TEMPLATE = runtime._build_default_source_config()


def register_adapter(cls):
    metadata = provider_cls_map.get(runtime.PROVIDER_TYPE)
    if metadata is None:
        register_provider_adapter(
            runtime.PROVIDER_TYPE,
            "OpenCode 官方 CLI（纯文本）",
            default_config_tmpl=dict(TEMPLATE),
            provider_display_name="OpenCode CLI",
        )(cls)
    elif not getattr(metadata.cls_type, "_astrbot_opencode_plugin", False):
        raise OpenCodeError("config")
    # AstrBot keeps the registry across module reloads. Publish the new class
    # every time, as in the reference plugin, without registering twice.
    metadata = provider_cls_map[runtime.PROVIDER_TYPE]
    metadata.cls_type = cls
    metadata.default_config_tmpl = dict(TEMPLATE)
    return cls


@register_adapter
class OpenCodeCLIProvider(Provider):
    _astrbot_opencode_plugin = True

    def __init__(self, provider_config, provider_settings):
        # Dashboard-created models can default to image/audio/tool support.
        # Declare this adapter's actual capabilities on its own config copy.
        config = {**provider_config, "modalities": ["text"]}
        super().__init__(config, provider_settings)
        cfg = runtime.settings()
        self.set_model(provider_config.get("model") or cfg["model"])
        self.engine = runtime.acquire_engine()
        self.settings = cfg
        self._pending = set()
        self._closed = False

    def get_current_key(self):
        return ""

    def get_keys(self):
        return []

    def set_key(self, key):
        # The native source's Key field is deliberately unused. Never store it.
        if key:
            raise OpenCodeError("config")

    async def get_models(self):
        # ACP catalog only; no model prompt or automatic model selection.
        return await runtime.available_model_ids()

    async def text_chat(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice="auto",
        request_max_retries=None,
        **kwargs,
    ):
        try:
            return await self._complete_text(
                prompt=prompt,
                image_urls=image_urls,
                audio_urls=audio_urls,
                func_tool=func_tool,
                contexts=contexts,
                system_prompt=system_prompt,
                tool_calls_result=tool_calls_result,
                model=model,
                extra_user_content_parts=extra_user_content_parts,
                tool_choice=tool_choice,
                session_id=session_id,
                conversation_id=kwargs.get("conversation_id"),
                request_sequence=kwargs.get("request_sequence"),
                request_max_retries=request_max_retries,
            )
        except asyncio.CancelledError:
            raise
        except OpenCodeError:
            raise
        except Exception:
            # Also sanitize failures while serializing framework content objects.
            raise OpenCodeError("cli_failed") from None

    async def _complete_text(
        self,
        *,
        prompt,
        image_urls,
        audio_urls,
        func_tool,
        contexts,
        system_prompt,
        tool_calls_result,
        model,
        extra_user_content_parts,
        tool_choice,
        session_id,
        conversation_id,
        request_sequence,
        request_max_retries=None,
    ):
        if self._closed:
            raise OpenCodeError("closed")
        has_tools = func_tool is not None and (
            not func_tool.empty()
            if callable(getattr(func_tool, "empty", None))
            else bool(func_tool)
        )
        if image_urls:
            raise UnsupportedFeatureError("image")
        if audio_urls:
            raise UnsupportedFeatureError("audio")
        if has_tools or tool_calls_result or tool_choice == "required":
            raise UnsupportedFeatureError("tool_calls")
        chosen = runtime.resolve_model_id(model if model is not None else self.get_model())
        if chosen != self.settings["model"]:
            raise OpenCodeError("model")
        messages = normalize_messages(
            prompt,
            contexts,
            system_prompt,
            extra_user_content_parts,
            max_bytes=self.settings["context_max_bytes"],
        )
        event = None
        try:
            from astrbot.core.platform.astr_message_event import current_message_event

            event = current_message_event.get()
        except (ImportError, LookupError):
            pass
        origin = getattr(event, "unified_msg_origin", "") if event is not None else ""
        stable = conversation_id or session_id
        scope = None
        stats_session = None
        if isinstance(stable, str) and stable:
            raw = f"{self.provider_config.get('id', '')}\0{origin}\0{stable}"
            scope = hashlib.blake2s(raw.encode(), key=_SESSION_SALT, digest_size=16).hexdigest()
            stats_session = runtime.usage_session(raw)

        async def on_queued(depth):
            if event is not None:
                await event.send(MessageChain().message("呜呜等一下"))

        async def attempt():
            task = asyncio.create_task(
                self.engine.submit(
                    messages,
                    scope,
                    on_queued=on_queued,
                    sequence=request_sequence,
                    stats_session=stats_session,
                )
            )
            self._pending.add(task)
            try:
                return await task
            finally:
                self._pending.discard(task)

        text = await retry_stream_errors(attempt, request_max_retries=request_max_retries)
        return LLMResponse(role="assistant", completion_text=text)

    async def text_chat_stream(self, *args, **kwargs):
        # AstrBot may request streaming. Emit one final response, never fake deltas.
        yield await self.text_chat(*args, **kwargs)

    async def terminate(self):
        if self._closed:
            return
        self._closed = True
        tasks = list(self._pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.release_engine(self.engine)
