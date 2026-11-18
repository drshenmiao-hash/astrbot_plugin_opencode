"""Plugin-local metadata statistics. No OpenCode billing/usage API is involved."""

import hashlib
import json
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .token_usage import TOKEN_FIELDS

logger = logging.getLogger("astrbot")
DISCLAIMER = "OpenCode 插件本地统计，不是 OpenCode 账单、套餐余额或官方消耗。"
_NUMBERS = (
    "duration_ms",
    "queue_ms",
    "input_bytes",
    "sent_input_bytes",
    "wire_bytes",
    "output_bytes",
)
_GROUPS = {"total": "'total'", "session": "session", "day": "day", "model": "model_id"}
DASHBOARD_RANGES = (7, 30, 90)
_TOTALS = f"""
    COUNT(*) AS requests,
    COALESCE(SUM(code = 'ok'), 0) AS success,
    COALESCE(SUM(code != 'ok'), 0) AS failed,
    {", ".join(f"COALESCE(SUM({name}), 0) AS {name}" for name in _NUMBERS)},
    {", ".join(f"SUM({name}) AS {name}" for name in TOKEN_FIELDS)},
    COUNT(token_total) AS token_attempts,
    COALESCE(SUM(process_reused), 0) AS process_reused,
    COALESCE(SUM(reused), 0) AS reused
"""


class UsageStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "usage.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        salt = self.directory / "usage-session.key"
        try:
            fd = os.open(salt, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            self._salt = salt.read_bytes()
        else:
            self._salt = os.urandom(32)
            with os.fdopen(fd, "wb") as out:
                out.write(self._salt)
        if len(self._salt) != 32:
            raise ValueError("Invalid local statistics session key")
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, day TEXT NOT NULL,
                session TEXT NOT NULL, model_id TEXT NOT NULL, code TEXT NOT NULL,
                duration_ms INTEGER NOT NULL, queue_ms INTEGER NOT NULL,
                input_bytes INTEGER NOT NULL, sent_input_bytes INTEGER NOT NULL,
                wire_bytes INTEGER NOT NULL, output_bytes INTEGER NOT NULL,
                process_reused INTEGER NOT NULL, reused INTEGER NOT NULL,
                reset_reason TEXT NOT NULL
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            for name in TOKEN_FIELDS:
                if name not in columns:
                    db.execute(f"ALTER TABLE requests ADD COLUMN {name} INTEGER")
            db.execute("CREATE INDEX IF NOT EXISTS requests_day ON requests(day)")
        # One storage worker only; no model workers, queue limits or sessions change.
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opencode-local-usage")
        self._lock = threading.Lock()
        self._last = None
        self._closed = False
        self.write_failures = 0

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def session_label(self, raw):
        return hashlib.blake2s(raw.encode(), key=self._salt, digest_size=16).hexdigest()

    def record(self, data, *, at=None):
        if data.get("event") != "request_finished":
            return None
        when = at or datetime.now(timezone.utc)
        if when.tzinfo is None:
            raise ValueError("Statistics timestamps must include a timezone")
        day = when.astimezone(timezone.utc).date().isoformat()
        # Only metadata columns are accepted. Never serialize the original event.
        numbers = [max(0, int(data.get(key) or 0)) for key in _NUMBERS]
        tokens = [data.get(key) for key in TOKEN_FIELDS]
        if not all(type(value) is int and 0 <= value <= 2**63 - 1 for value in tokens):
            tokens = [None] * len(TOKEN_FIELDS)
        row = (
            str(data["request_id"]),
            day,
            data.get("session") or "",
            data.get("model_id") or "",
            data.get("code") or "unknown",
            *numbers,
            int(bool(data.get("process_reused"))),
            int(bool(data.get("reused"))),
            data.get("reset_reason") or "",
            *tokens,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("Local statistics store is closed")
            self._last = self._writer.submit(self._write_record, row)
            return self._last

    def _write(self, row):
        placeholders = ",".join("?" for _ in row)
        with self._connect() as db:
            db.execute(
                f"""INSERT OR IGNORE INTO requests (
                    request_id, day, session, model_id, code, {", ".join(_NUMBERS)},
                    process_reused, reused, reset_reason, {", ".join(TOKEN_FIELDS)}
                ) VALUES ({placeholders})""",
                row,
            )

    def _write_record(self, row):
        try:
            self._write(row)
        except Exception:
            # Never log raw SQLite errors, event contents, credentials or paths.
            self.write_failures += 1
            logger.warning("opencode_local_usage code=write_failed")
            raise

    def flush(self):
        with self._lock:
            future = self._last
        if future is not None:
            future.result()

    def _wait_for_writes(self):
        with self._lock:
            pending = self._last
        if pending is not None:
            wait([pending])

    def summary(self, group="total", key="", *, limit=20):
        if group not in _GROUPS:
            raise ValueError("Use total, session, day or model")
        self._wait_for_writes()
        column = _GROUPS[group]
        where, params = (f" WHERE {column} = ?", [key]) if key else ("", [])
        sql = f"""SELECT {column} AS label, {_TOTALS}
            FROM requests{where} GROUP BY {column}
            ORDER BY MAX(day) DESC, label LIMIT ?"""
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(sql, [*params, min(100, max(1, limit))])]
            for row in rows:
                codes = db.execute(
                    f"SELECT code,COUNT(*) FROM requests WHERE {column} = ? GROUP BY code ORDER BY code",
                    [row["label"]],
                )
                row["codes"] = dict(codes)
                count = row["requests"]
                row["duration_avg_ms"] = round(row["duration_ms"] / count, 2) if count else 0
                row["queue_avg_ms"] = round(row["queue_ms"] / count, 2) if count else 0
        return {
            "notice": DISCLAIMER,
            "group": group,
            "timezone": "UTC",
            "rows": rows,
            "write_failures": self.write_failures,
        }

    def dashboard(self, days):
        """Read a consistent, bounded UTC view even after an individual write fails."""
        if type(days) is not int or days not in DASHBOARD_RANGES:
            raise ValueError("Unsupported dashboard range")
        self._wait_for_writes()

        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=days - 1)
        date_filter = (start.isoformat(), end.isoformat())
        where = "FROM requests WHERE day BETWEEN ? AND ?"

        with self._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            total = dict(db.execute(f"SELECT {_TOTALS} {where}", date_filter).fetchone())
            day_rows = db.execute(
                f"""SELECT day AS label, COUNT(*) AS requests, SUM(code != 'ok') AS failed,
                    SUM(token_total) AS token_total {where} GROUP BY day""",
                date_filter,
            )
            trend = {row["label"]: dict(row) for row in day_rows}
            groups = {}
            for key, column in (("model", "model_id"), ("session", "session"), ("code", "code")):
                groups[key] = [
                    dict(row)
                    for row in db.execute(
                        f"SELECT {column} AS label, {_TOTALS} {where} "
                        f"GROUP BY {column} ORDER BY requests DESC, label LIMIT 20",
                        date_filter,
                    )
                ]
        labels = [(start + timedelta(days=offset)).isoformat() for offset in range(days)]
        return {
            "notice": DISCLAIMER,
            "timezone": "UTC",
            "start": date_filter[0],
            "end": date_filter[1],
            "totals": total,
            "trend": [
                trend.get(label, {"label": label, "requests": 0, "failed": 0, "token_total": None})
                for label in labels
            ],
            "groups": groups,
            "write_failures": self.write_failures,
        }

    def close(self):
        with self._lock:
            self._closed = True
        self._writer.shutdown(wait=True)


def format_summary(report):
    lines = [report["notice"], f"维度：{report['group']}；日期按 UTC；每次最多显示 20 组。"]
    if report["write_failures"]:
        lines.append(f"统计写入失败 {report['write_failures']} 次，数据可能不完整。")
    for row in report["rows"]:
        label = row["label"] or ("未记录 ACP 发送" if report["group"] == "model" else "未识别会话")
        tokens = (
            f"总计 {row['token_total']}，输入 {row['token_input']}，输出 {row['token_output']}，缓存命中 {row['token_cached']}，已记录 {row['token_attempts']} 次"
            if row["token_attempts"]
            else "暂无记录"
        )
        lines.extend(
            [
                f"[{label}] 请求 {row['requests']}，成功 {row['success']}，失败 {row['failed']}；进程复用 {row['process_reused']}，会话复用 {row['reused']}",
                f"耗时总/均(ms) {row['duration_ms']}/{row['duration_avg_ms']}；排队总/均(ms) {row['queue_ms']}/{row['queue_avg_ms']}",
                f"字节 input={row['input_bytes']} sent={row['sent_input_bytes']} wire={row['wire_bytes']} output={row['output_bytes']}",
                f"Token {tokens}",
                "结果码：" + json.dumps(row["codes"], ensure_ascii=False, sort_keys=True),
            ]
        )
    if not report["rows"]:
        lines.append("暂无本地记录。")
    lines.append("请求数包含失败和缓存命中；字节不是 token 或费用，不查询官方账单。")
    return "\n".join(lines)
