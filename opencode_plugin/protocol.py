"""Bounded text serialization and explicit OpenCode model IDs."""

import json
import re

from .errors import OpenCodeError

MAX_CONTEXT = 256 * 1024
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*/[A-Za-z0-9][A-Za-z0-9._:/-]*")


def explicit_model(value):
    if (
        not isinstance(value, str)
        or len(value) > 240
        or not _MODEL.fullmatch(value)
        or value.rsplit("/", 1)[-1].casefold() in {"auto", "auto-smart", "default"}
    ):
        raise OpenCodeError("model")
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
