"""Canonical text history and an incremental rendering; AstrBot remains authoritative."""

from .errors import OpenCodeError, UnsupportedFeatureError
from .images import historical_image_key, prompt_images
from .protocol import MAX_CONTEXT, encode


def as_dict(value):
    if isinstance(value, dict):
        return value
    if callable(getattr(value, "model_dump_for_context", None)):
        return value.model_dump_for_context()
    if callable(getattr(value, "model_dump", None)):
        data = value.model_dump()
        if isinstance(getattr(value, "content", None), list):
            data["content"] = value.content
        return data
    raise UnsupportedFeatureError("content")


def temporary_text(part):
    text = part["text"]
    # AstrBot's modality serialization strips AstrNa's _no_save flag from these parts.
    return part.get("_no_save") is True or (
        text.startswith("<system_reminder>\nAstrNa 触发后新增群聊消息：\n")
        and text.endswith("\n--- END NEW GROUP MESSAGES ---\n</system_reminder>")
        and "\n--- BEGIN NEW GROUP MESSAGES ---\n" in text
    )


def text_content(content, *, read_image=None, history_parts=None):
    if content is None:
        return ""
    if isinstance(content, str):
        if history_parts is not None:
            history_parts.append(content)
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
            if read_image is None:
                raise UnsupportedFeatureError("image")
            source = part.get("image_url")
            if isinstance(source, dict):
                source = source.get("url")
            read_image(source)
            continue
        if kind in {"audio", "audio_url"}:
            raise UnsupportedFeatureError("audio")
        if kind != "text" or not isinstance(part.get("text"), str):
            raise UnsupportedFeatureError("content")
        parts.append(part["text"])
        if history_parts is not None and not temporary_text(part):
            history_parts.append(part["text"])
    return "\n".join(parts)


def merge_extra_parts(content, extra_parts):
    """The AstrBot runner passes its assembled final user message and its extra parts."""
    if not extra_parts:
        return content
    if isinstance(content, list):
        parts = [as_dict(part) for part in content]
    else:
        parts = [{"type": "text", "text": content}] if content else []
    unmatched = [*range(1, len(parts)), *([0] if parts else [])]
    for part in map(as_dict, extra_parts):
        matching = next(
            (
                i
                for i in unmatched
                if {key: value for key, value in parts[i].items() if key != "_no_save"}
                == {key: value for key, value in part.items() if key != "_no_save"}
            ),
            None,
        )
        if matching is None:
            parts.append(part)
        else:
            unmatched.remove(matching)
            if part.get("_no_save") is True:
                parts[matching] = {**parts[matching], "_no_save": True}
    return parts


def normalize_messages(
    prompt=None,
    contexts=None,
    system_prompt=None,
    extra_parts=None,
    *,
    max_bytes=MAX_CONTEXT,
    image_input=None,
    image_urls=None,
):
    def user_entry(content, *, current, refs=()):
        images = []
        history_parts = []

        def read(source):
            if not current:
                images.append(historical_image_key(source))
            elif image_input is not None:
                images.append(image_input.read(source))
            else:
                raise UnsupportedFeatureError("image")

        if (
            current
            and isinstance(content, list)
            and any(
                part.get("type") == "text" and part.get("text") == "[Image unavailable]"
                for part in map(as_dict, content)
            )
        ):
            raise OpenCodeError("image_source")
        text = text_content(content, read_image=read, history_parts=history_parts)
        for ref in refs:
            read(ref)
        entry = {"role": "user", "content": text}
        if images:
            entry["images"] = images
            if current:
                entry["_current"] = True
        history_text = "\n".join(history_parts)
        if history_text != text:
            entry["_history_content"] = history_text
        return entry

    messages = []
    if system_prompt:
        if not isinstance(system_prompt, str):
            raise UnsupportedFeatureError("content")
        messages.append({"role": "system", "content": system_prompt})
    contexts = [as_dict(item) for item in contexts or []]
    explicit_user = prompt is not None or bool(image_urls)
    current = (
        len(contexts) - 1
        if not explicit_user and contexts and contexts[-1].get("role") == "user"
        else None
    )
    for index, message in enumerate(contexts):
        role = message.get("role")
        if role == "tool" or message.get("tool_calls") or message.get("function_call"):
            raise UnsupportedFeatureError("tool_calls")
        if role not in {"system", "developer", "user", "assistant"}:
            raise UnsupportedFeatureError("content")
        if role == "user":
            content = message.get("content")
            if index == current:
                content = merge_extra_parts(content, extra_parts)
            messages.append(user_entry(content, current=index == current))
        else:
            messages.append({"role": role, "content": text_content(message.get("content"))})
    if prompt is not None and not isinstance(prompt, str):
        raise UnsupportedFeatureError("content")
    if current is None and (explicit_user or extra_parts):
        content = prompt
        if extra_parts:
            content = ([{"type": "text", "text": prompt}] if prompt else []) + list(extra_parts)
        messages.append(user_entry(content, current=True, refs=image_urls or ()))
    if not messages:
        raise OpenCodeError("empty_input")
    if len(encode(messages)) > max_bytes:
        raise OpenCodeError("context_limit")
    return messages


def render_messages(messages, *, incremental=False, clock_fields=None, image_count=0):
    if incremental:
        header = "Continue the conversation with these new transcript entries. Output only the assistant reply.\n\n"
    else:
        header = (
            "You are replying to a chat managed by AstrBot. The JSON transcript "
            "below contains the configured persona, prior messages and current user "
            "message. Continue the conversation and output only the assistant reply. "
            "Do not inspect files, run commands, browse, use tools or manage an OpenCode "
            "session. System/developer entries describe the chat persona.\n\n"
        )
    if incremental and clock_fields:
        header = (
            header.rstrip("\n")
            + "\nCurrent clock fields for this request (supersede earlier clock values only): "
            + encode(clock_fields).decode("utf-8")
            + "\n\n"
        )
    if any("historical_images_not_attached" in entry for entry in messages):
        header += "The historical_images_not_attached field reports earlier images whose pixels are not included. Use the recorded text and do not invent missing visual details.\n\n"
    if image_count:
        header += "In the transcript, each images index refers to the corresponding image attachment (1-based).\n\n"
    return header + encode(messages).decode("utf-8")


def build_transcript(
    prompt=None, contexts=None, system_prompt=None, extra_parts=None, *, max_bytes=MAX_CONTEXT
):
    messages = normalize_messages(prompt, contexts, system_prompt, extra_parts, max_bytes=max_bytes)
    transcript, _ = prompt_images(messages, {})
    return render_messages(transcript)
