"""Validate paths and limits before opening the isolated CLI."""

from pathlib import Path

from .errors import MESSAGES, OpenCodeError
from .protocol import explicit_model

PATH_DEFAULTS = {
    "cli_binary": "/usr/local/bin/opencode",
    "bubblewrap_binary": "/usr/bin/bwrap",
    "auth_path": "~/.local/share/astrbot-opencode/opencode/auth.json",
}


def configured_path(config, name):
    value = config.get(name, PATH_DEFAULTS[name])
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise ValueError
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError
    return str(path)


def settings_from(config):
    try:
        model = config.get("model_id", "").strip()
        if model:
            explicit_model(model)
        cfg = {"model": model, "requested_model": model}
        cfg["sandbox_enabled"] = config.get("sandbox_enabled", True)
        if type(cfg["sandbox_enabled"]) is not bool:
            raise ValueError
        notice = config.get("image_unsupported_message", "")
        if not isinstance(notice, str) or len(notice) > 240 or "\x00" in notice:
            raise ValueError
        cfg["image_unsupported_message"] = notice.strip() or MESSAGES["image_unsupported"]
        cfg["protected_paths"] = [str(Path(__file__).resolve().parents[1])]
        for name, default, minimum, maximum in (
            ("timeout_seconds", 120, 1, 1800),
            ("queue_timeout_seconds", 120, 1, 3600),
            ("max_concurrency", 2, 1, 8),
            ("queue_capacity", 64, 1, 1024),
            ("context_max_bytes", 262144, 1024, 4194304),
            ("output_max_bytes", 2097152, 1024, 4194304),
            ("max_sessions", 128, 1, 4096),
        ):
            value = config.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError
            value = int(value)
            if not minimum <= value <= maximum:
                raise ValueError
            cfg[name] = value
        for name in PATH_DEFAULTS:
            if name == "bubblewrap_binary" and not cfg["sandbox_enabled"]:
                continue
            cfg[name] = configured_path(config, name)
        return cfg
    except OpenCodeError:
        raise
    except (ValueError, TypeError, AttributeError, OverflowError, RuntimeError):
        raise OpenCodeError("config") from None
