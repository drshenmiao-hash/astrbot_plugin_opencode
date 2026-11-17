"""Metadata-only records. No request/response content, credentials, paths or raw errors."""

import json
import logging

logger = logging.getLogger("astrbot")
_usage_store = None
_FIELDS = {
    "request_id",
    "session",
    "event",
    "transport",
    "code",
    "input_bytes",
    "sent_input_bytes",
    "wire_bytes",
    "output_bytes",
    "queue_ms",
    "duration_ms",
    "reused",
    "queue_depth",
    "active",
    "reset_reason",
    "process_reused",
    "version",
    "sequence",
    "model_id",
}


def set_usage_store(store):
    global _usage_store
    _usage_store = store


def record(event, **values):
    data = {"event": event}
    data.update({key: value for key, value in values.items() if key in _FIELDS})
    if event == "request_finished" and _usage_store is not None:
        try:
            _usage_store.record(data)
        except Exception:
            # Statistics must never prevent a request's result/error delivery.
            logger.warning("opencode_local_usage code=enqueue_failed")
    logger.info("opencode_provider %s", json.dumps(data, ensure_ascii=True, separators=(",", ":")))
