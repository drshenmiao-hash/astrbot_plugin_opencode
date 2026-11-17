"""Canonical text history and an incremental rendering; AstrBot remains authoritative."""

from .errors import OpenCodeError, UnsupportedFeatureError
from .protocol import MAX_CONTEXT, encode


def as_dict(value):
    if isinstance(value, dict):
        return value
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump()
    raise UnsupportedFeatureError("content")


def text_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise UnsupportedFeatureError("content")
    parts = []
    for item in content:
        part = as_dict(item)
        kind = part.get("type")
        if kind == "think":
            continue
        if kind in {"image", "image_url"}:
            raise UnsupportedFeatureError("image")
        if kind in {"audio", "audio_url"}:
            raise UnsupportedFeatureError("audio")
        if kind != "text" or not isinstance(part.get("text"), str):
            raise UnsupportedFeatureError("content")
        parts.append(part["text"])
    return "\n".join(parts)


def normalize_messages(
    prompt=None, contexts=None, system_prompt=None, extra_parts=None, *, max_bytes=MAX_CONTEXT
):
    messages = []
    if system_prompt:
        if not isinstance(system_prompt, str):
            raise UnsupportedFeatureError("content")
        messages.append({"role": "system", "content": system_prompt})
    for item in contexts or []:
        message = as_dict(item)
        role = message.get("role")
        if role == "tool" or message.get("tool_calls") or message.get("function_call"):
            raise UnsupportedFeatureError("tool_calls")
        if role not in {"system", "developer", "user", "assistant"}:
            raise UnsupportedFeatureError("content")
        messages.append({"role": role, "content": text_content(message.get("content"))})
    if prompt is not None and not isinstance(prompt, str):
        raise UnsupportedFeatureError("content")
    extra = text_content(extra_parts) if extra_parts else ""
    if prompt is not None or extra:
        content = prompt or ""
        if extra:
            content += ("\n" if content else "") + extra
        messages.append({"role": "user", "content": content})
    if not messages:
        raise OpenCodeError("empty_input")
    if len(encode(messages)) > max_bytes:
        raise OpenCodeError("context_limit")
    return messages


def render_messages(messages, *, incremental=False, clock_fields=None):
    if incremental:
        header = "Continue the conversation with these new transcript entries. Output only the assistant reply.\n\n"
    else:
        header = (
            "You are replying to a text chat managed by AstrBot. The JSON transcript "
            "below contains the configured persona, prior messages and current user "
            "message. Continue the conversation and output only the assistant reply. "
            "Do not inspect files, run commands, browse, use tools or manage a OpenCode "
            "session. System/developer entries describe the chat persona.\n\n"
        )
    if incremental and clock_fields:
        header = (
            header.rstrip("\n")
            + "\nCurrent clock fields for this request (supersede earlier clock values only): "
            + encode(clock_fields).decode("utf-8")
            + "\n\n"
        )
    return header + encode(messages).decode("utf-8")


def build_transcript(
    prompt=None, contexts=None, system_prompt=None, extra_parts=None, *, max_bytes=MAX_CONTEXT
):
    return render_messages(
        normalize_messages(prompt, contexts, system_prompt, extra_parts, max_bytes=max_bytes)
    )
