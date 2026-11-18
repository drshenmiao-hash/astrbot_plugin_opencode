"""Pure history, queue and error rules. No IO, network, processes or framework imports."""

import hashlib
import json
import re

from .system_clock import current_clock_fields, raw_append_suffix, semantic_systems

_REASONING_BLOCK = re.compile(r"<(think|thinking)>(.*?)</\1>", re.DOTALL)
_TRAILING_REASONING_CLOSE = re.compile(r"</(?:think|thinking)>\s*$")

_ASTRNA_REPLY_MARKER = re.compile(
    r"<(astrna_(?:reply_target|quoted_sender|quoted_reply_target))>.*?</\1>", re.DOTALL
)
_MACHINE_CHUNK_TAIL = re.compile(
    r"(?:error[ \t]*:[ \t]*)*retriableerror[ \t]*:[ \t]*"
    r"(?:\[unavailable\][ \t]*)?(?:connection[ \t]+stalled|ping[ \t]+timed[ \t]+out)"
    r"[.!]?[ \t]*$",
    re.IGNORECASE,
)
_IMAGE_UNSUPPORTED = re.compile(
    r"\b(?:does not|doesn['’]t|cannot) (?:support|accept) "
    r"(?:images?|image inputs?|image attachments?|vision|multimodal inputs?)\b"
    r"|\b(?:images?|image inputs?|image attachments?|vision)(?: (?:is|are))? not supported\b"
    r"|\bunsupported (?:input|content)(?: block)? type\s*[:=]\s*[\"']?image\b"
    r"|\bunsupported image (?:inputs?|attachments?)\b"
    r"|\bonly supports? text(?: input| content)?\b"
    r"|\bonly (?:text input|text content) (?:is|are) supported\b"
)


def history_hashes(history):
    return [
        hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for item in history
    ]


def compare_history_hashes(old_hashes, new_hashes, *, tail_count=2):
    if tail_count < 1:
        raise ValueError("tail_count must be positive")
    old_size, new_size = len(old_hashes), len(new_hashes)
    prefix = list(new_hashes[:old_size])
    length_rule = "grown" if new_size > old_size else "shrunk" if new_size < old_size else "same"
    hash_rule = "match" if list(old_hashes) == prefix else "changed"
    tail_size = min(tail_count, old_size)
    if new_size < old_size:
        tail_rule = "insufficient"
    elif tail_size == 0:
        tail_rule = "empty"
    else:
        tail_rule = "match" if list(old_hashes[-tail_size:]) == prefix[-tail_size:] else "changed"
    # Full dialogue-prefix equality is authoritative. Length/tail are diagnostic evidence,
    # never substitutes for checking middle messages.
    reused = old_size > 0 and hash_rule == "match"
    reason = (
        "continuous_history"
        if reused
        else "new_conversation"
        if not old_size
        else "history_shrunk"
        if new_size < old_size
        else "history_changed"
    )
    return {
        "reused": reused,
        "delta_start": old_size if reused else 0,
        "reason": reason,
        "length_rule": length_rule,
        "hash_rule": hash_rule,
        "tail_rule": tail_rule,
    }


def assistant_match_text(text):
    """Mirror AstrBot's reasoning-tag cleanup, exclusively for history hashes.

    Match complete reasoning blocks, a trailing reasoning close, and the
    internal reply markers removed by AstrNa when it saves conversation history.
    """
    text = _TRAILING_REASONING_CLOSE.sub("", _REASONING_BLOCK.sub("", text)).strip()
    text, removed = _ASTRNA_REPLY_MARKER.subn("", text)
    if removed:
        text = re.sub(r"[ \t]+\n|\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def history_state(history):
    """Keep system, durable dialogue, and the exact replay of a turn separately."""
    systems = [dict(item) for item in history if item.get("role") == "system"]
    dialogue = [item for item in history if item.get("role") != "system"]
    submitted = []
    canonical = []
    for item in dialogue:
        entry = {key: value for key, value in item.items() if not key.startswith("_")}
        if entry.get("role") == "assistant" and isinstance(entry.get("content"), str):
            entry["content"] = assistant_match_text(entry["content"])
        submitted.append(entry)
        canonical.append(
            {**entry, "content": item["_history_content"]} if "_history_content" in item else entry
        )
    hashes = history_hashes(canonical)
    return {
        "systems": systems,
        "dialogue_hashes": hashes,
        "request_hashes": hashes if submitted == canonical else history_hashes(submitted),
        "dialogue_fields": [
            (
                item.get("role"),
                hashlib.sha256(
                    json.dumps(item.get("content"), ensure_ascii=False).encode()
                ).digest(),
                tuple(item.get("images", ())),
            )
            for item in canonical
        ],
    }


def history_difference(previous, current):
    """Return positions and field names only. Never return a comparison value."""
    old, new = previous["dialogue_hashes"], current["dialogue_hashes"]
    first = next((i for i, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
    if first == len(old):
        difference = "none"
    elif first == len(new):
        difference = "window_shortened"
    else:
        difference = "entry_changed"
        if first == 0 and len(new) >= 2:
            for index in range(1, len(old) - 1):
                overlap = len(old) - index
                if overlap <= len(new) and new[:overlap] == old[index:]:
                    difference = "window_shifted"
                    break
    fields = []
    if difference == "entry_changed":
        fields = [
            name
            for name, a, b in zip(
                ("role", "content", "images"),
                previous["dialogue_fields"][first],
                current["dialogue_fields"][first],
            )
            if a != b
        ]
    return {
        "history_change": difference,
        "history_previous_count": len(old),
        "history_incoming_count": len(new),
        "history_first_difference": first if difference == "entry_changed" else None,
        "history_changed_fields": fields,
    }


def system_delta(old_systems, new_systems):
    """One transcript entry containing changes to the last applied system state."""
    old_views, new_views = semantic_systems(old_systems), semantic_systems(new_systems)
    operations = [
        {"op": "remove", "index": index}
        for index in range(len(old_views) - 1, len(new_views) - 1, -1)
    ]
    for index, new in enumerate(new_views):
        content = new["raw"]
        if index >= len(old_views):
            operations.append({"op": "add", "index": index, "content": content})
            continue
        old = old_views[index]["text"]
        if new["text"] == old:
            continue
        if (
            isinstance(old, str)
            and isinstance(new["text"], str)
            and old
            and new["text"].startswith(old)
        ):
            operations.append(
                {"op": "append", "index": index, "content": raw_append_suffix(new, len(old))}
            )
        else:
            operations.append({"op": "replace", "index": index, "content": content})
    if not operations:
        return None
    return {
        "role": "system",
        "content": (
            "Update the previous system entries of this transcript before answering. "
            "Indices are zero-based among system entries with non-clock instructions; "
            "clock-only entries are updated through the current clock fields. Apply these operations in order: "
            "append extends an entry; replace supersedes its previous content; "
            "add introduces an entry; remove withdraws it. Unchanged entries remain in force.\n"
            + json.dumps(operations, ensure_ascii=False, separators=(",", ":"))
        ),
    }


def reconcile_history(previous, new_history, *, last_request_hashes=None, tail_count=2):
    """Match only dialogue; return pending system updates alongside new dialogue."""
    state = history_state(new_history)
    dialogue = [item for item in new_history if item.get("role") != "system"]
    result = compare_history_hashes(
        previous["dialogue_hashes"] if previous else [],
        state["dialogue_hashes"],
        tail_count=tail_count,
    )
    # A retry may repeat the input *without* our already-generated assistant reply.
    # Match that request too, rather than reopening a session or generating twice.
    if previous and last_request_hashes and state["request_hashes"] == last_request_hashes:
        result.update(reused=True, delta_start=len(dialogue), reason="request_replay")
    dialogue_delta = dialogue[result["delta_start"] :]
    update = system_delta(previous["systems"], state["systems"]) if previous else None
    delta = ([update] if update else []) + dialogue_delta if result["reused"] else list(new_history)
    return {
        **result,
        "delta": delta,
        "system_changed": update is not None,
        "has_new_user_input": any(item.get("role") == "user" for item in dialogue_delta),
        "clock_fields": current_clock_fields(state["systems"]),
        "history_difference": history_difference(previous, state) if previous else {},
    }


def history_plan(old_history, new_history, *, tail_count=2):
    return reconcile_history(history_state(old_history), new_history, tail_count=tail_count)


def queue_execution_order(requests, *, active_sessions=()):
    """Order a pending batch; explicit sequence applies only within a session.

    Without a sequence, arrival order is used. This cannot reorder requests that
    already started or infer a sequence absent from the input.
    """
    active = set(active_sessions)
    groups = {}
    for index, item in enumerate(requests):
        session = item["session"]
        groups.setdefault(session, []).append((index, item))
    for items in groups.values():
        sequences = [item.get("sequence") for _, item in items]
        if all(type(seq) is int for seq in sequences):
            # Equal sequence values retain arrival order; never kill a worker
            # because a caller supplied duplicate timestamps/sequence values.
            items.sort(key=lambda entry: entry[1]["sequence"])
    ordered = []
    while groups:
        candidates = [
            (min(index for index, _ in items), session)
            for session, items in groups.items()
            if session not in active
        ]
        if not candidates:
            break
        _, session = min(candidates, key=lambda entry: entry[0])
        _, item = groups[session].pop(0)
        ordered.append(item["id"])
        if not groups[session]:
            groups.pop(session)
    return ordered


def classify_image_error(raw, *, reply=False):
    text = raw.strip().casefold()
    prefixed = text.startswith(("error:", "image error:"))
    text = text.removeprefix("error:").strip().removeprefix("image error:").strip()
    text = text.removeprefix("[invalid_argument]").strip()
    if (
        text.startswith("the model provider could not read an image")
        or text.rstrip(".!") == "invalid image"
    ):
        return "image_unavailable"
    if not reply or prefixed:
        if _IMAGE_UNSUPPORTED.search(text):
            return "image_unsupported"
        if text.startswith("invalid image:"):
            return "image_unavailable"
    concise = re.sub(r"^(?:(?:this|the|current|selected) )?model ", "", text.rstrip(".!"))
    if _IMAGE_UNSUPPORTED.fullmatch(concise):
        return "image_unsupported"
    return None


def classify_error(raw, *, stage="request"):
    text = raw.casefold()
    if image_error := classify_image_error(raw):
        return image_error
    if "queue" in text and any(term in text for term in ("timeout", "timed out", "expired")):
        return "queue_timeout"
    if any(
        term in text
        for term in (
            "authentication required",
            "authentication failed",
            "not authenticated",
            "not logged in",
            "unauthenticated",
            "token expired",
            "token has expired",
            "invalid access token",
            "invalid api key",
            "incorrect api key",
            "invalid_api_key",
            "unauthorized",
        )
    ):
        return "not_logged_in"
    if any(term in text for term in ("quota exceeded", "usage limit", "insufficient balance")):
        return "quota_exhausted"
    if "429" in text or "rate limit" in text:
        return "rate_limited"
    if "model" in text and any(
        term in text for term in ("not available", "not found", "unsupported", "unavailable")
    ):
        return "model_unavailable"
    if "context" in text and any(
        term in text for term in ("too long", "too large", "limit", "exceed")
    ):
        return "context_limit"
    if any(
        term in text for term in ("invalid json", "malformed json", "json parse", "protocol error")
    ):
        return "protocol"
    if "timeout" in text or "timed out" in text:
        return "startup_timeout" if stage == "startup" else "timeout"
    return "cli_failed"


def classify_reply_error(text):
    """Recognize machine error replies, not prose that mentions an error phrase."""
    lines = text.strip().splitlines()
    if not lines:
        return None
    for line in dict.fromkeys((lines[0].strip(), lines[-1].strip())):
        stripped = re.sub(r"^(?:error\s*:\s*)+", "", line, flags=re.IGNORECASE)
        retryable = re.match(r"^RetriableError(?:\s*:\s*|$)", stripped, re.IGNORECASE)
        marked = stripped != line
        if retryable:
            reason = stripped[retryable.end() :].casefold()
        else:
            if len(lines) > 1 and not marked:
                continue
            match = re.fullmatch(
                r"(?:\[unavailable\]\s*)?(connection\s+stalled|ping\s+timed\s+out)[.!]?",
                stripped,
                re.IGNORECASE,
            )
            if not match:
                continue
            reason = match[1].casefold()
        if re.search(r"\bconnection\s+stalled\b", reason):
            return "connection_stalled"
        if re.search(r"\bping\s+timed\s+out\b", reason):
            return "ping_timeout"
        return "upstream_retryable"
    return None


def classify_chunk_tail_error(text, chunks):
    """Check a terminal machine diagnostic starting in a separate ACP text chunk."""
    trimmed = text.rstrip()
    match = _MACHINE_CHUNK_TAIL.search(trimmed, max(0, len(trimmed) - 256))
    if not match:
        return None
    offset = 0
    for chunk in chunks:
        if offset + len(chunk) - len(chunk.lstrip()) == match.start():
            return classify_reply_error(match[0])
        offset += len(chunk)
        if offset > match.start():
            break
    return None
