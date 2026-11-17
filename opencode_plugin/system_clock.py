"""Conservative clock-field comparison views; raw model input is never rewritten."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LABEL = re.compile(
    r"\s*(?:[-*]\s+)?(?:\*\*)?"
    r"(?P<label>(?:当前|系统|本地|现在|今日)?(?:时间|日期|时刻|时区)|"
    r"(?:current\s+|system\s+|local\s+)?(?:time(?:\s+zone)?|date|datetime|timestamp)|timezone)"
    r"(?:\*\*)?\s*[:：]\s*(?P<value>.+?)\s*",
    re.IGNORECASE,
)
_DATE = r"(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)"
_TIME = r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:\s*[AP]M)?"
_ZONE = r"(?:Z|(?:UTC|GMT)(?:[+-]\d{1,2}(?::?\d{2})?)?|[+-]\d{2}:?\d{2}|[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?)"
_VALUE = re.compile(
    rf"(?:(?P<date>{_DATE})(?:[ T]+(?P<time>{_TIME}))?|(?P<time_only>{_TIME}))"
    rf"(?:\s*\(?(?P<zone>{_ZONE})\)?)?",
    re.IGNORECASE,
)


def _zone(value):
    if value.upper() in {"Z", "UTC", "GMT"}:
        return True
    offset = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?", value, re.IGNORECASE)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3] or 0)
        return hours <= 14 and minutes < 60 and (hours < 14 or minutes == 0)
    try:
        ZoneInfo(value)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def _date(value):
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            datetime.strptime(value, pattern)
            return True
        except ValueError:
            pass
    return False


def _time(value):
    for pattern in ("%H:%M", "%H:%M:%S", "%H:%M:%S.%f", "%I:%M %p", "%I:%M:%S %p"):
        try:
            datetime.strptime(value, pattern)
            return True
        except ValueError:
            pass
    return False


def _field(value):
    match = _LABEL.fullmatch(value)
    if match is None:
        return False
    label, value = match["label"].casefold(), match["value"]
    if "zone" in label or "时区" in label:
        return _zone(value)
    stamp = _VALUE.fullmatch(value)
    if stamp is None:
        return False
    date, time, zone = stamp["date"], stamp["time"] or stamp["time_only"], stamp["zone"]
    if ("date" in label or "日期" in label) and not date:
        return False
    return (not date or _date(date)) and (not time or _time(time)) and (not zone or _zone(zone))


def _clock_line(line):
    value = line.strip()
    for left, right in (("[", "]"), ("【", "】")):
        if value.startswith(left) and value.endswith(right):
            value = value[len(left) : -len(right)].strip()
    # Mixed clock + substantive prose is deliberately left intact.
    fields = re.split(r"\s*[;；|]\s*", value)
    return bool(fields) and all(_field(field) for field in fields)


def system_view(content):
    if not isinstance(content, str):
        return {"text": content, "raw": content, "clocks": [], "spans": [], "leading": 0}
    kept, clocks, spans, position = [], [], [], 0
    for line in content.splitlines(keepends=True):
        if _clock_line(line.rstrip("\r\n")):
            clocks.append(line.strip())
        else:
            kept.append(line)
            spans.append((position, len(line)))
        position += len(line)
    joined = "".join(kept)
    leading = len(joined) - len(joined.lstrip("\r\n"))
    return {
        "text": joined.strip("\r\n"),
        "raw": content,
        "clocks": clocks,
        "spans": spans,
        "leading": leading,
    }


def raw_append_suffix(view, prefix_length):
    target = prefix_length + view["leading"]
    traversed = 0
    for start, length in view["spans"]:
        if target <= traversed + length:
            return view["raw"][start + target - traversed :]
        traversed += length
    return ""


def semantic_systems(systems):
    views = [system_view(item.get("content", "")) for item in systems]
    return [view for view in views if view["text"] or not view["clocks"]]


def current_clock_fields(systems):
    return [field for item in systems for field in system_view(item.get("content", ""))["clocks"]]
