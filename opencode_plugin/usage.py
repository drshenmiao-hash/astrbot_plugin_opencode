"""Plugin-local metadata statistics. No OpenCode billing/usage API is involved."""

import hashlib
import json
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, day TEXT NOT NULL,
                session TEXT NOT NULL, model_id TEXT NOT NULL, code TEXT NOT NULL,
                duration_ms INTEGER NOT NULL, queue_ms INTEGER NOT NULL,
                input_bytes INTEGER NOT NULL, sent_input_bytes INTEGER NOT NULL,
                wire_bytes INTEGER NOT NULL, output_bytes INTEGER NOT NULL,
                process_reused INTEGER NOT NULL, reused INTEGER NOT NULL,
                reset_reason TEXT NOT NULL
            )""")
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
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("Local statistics store is closed")
            self._last = self._writer.submit(self._write, row)
            self._last.add_done_callback(self._write_done)
            return self._last

    def _write(self, row):
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    def _write_done(self, future):
        if future.exception() is not None:
            # Never log raw SQLite errors, event contents, credentials or paths.
            self.write_failures += 1
            logger.warning("opencode_local_usage code=write_failed")

    def flush(self):
        with self._lock:
            future = self._last
        if future is not None:
            future.result()

    def summary(self, group="total", key="", *, limit=20):
        if group not in _GROUPS:
            raise ValueError("Use total, session, day or model")
        self.flush()
        column = _GROUPS[group]
        where, params = (f" WHERE {column} = ?", [key]) if key else ("", [])
        sums = ",".join(f"COALESCE(SUM({name}),0) AS {name}" for name in _NUMBERS)
        sql = f"""SELECT {column} AS label, COUNT(*) AS requests,
            COALESCE(SUM(code='ok'),0) AS success,
            COALESCE(SUM(code!='ok'),0) AS failed,
            {sums}, COALESCE(SUM(process_reused),0) AS process_reused,
            COALESCE(SUM(reused),0) AS reused
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
        lines.extend(
            [
                f"[{label}] 请求 {row['requests']}，成功 {row['success']}，失败 {row['failed']}；进程复用 {row['process_reused']}，会话复用 {row['reused']}",
                f"耗时总/均(ms) {row['duration_ms']}/{row['duration_avg_ms']}；排队总/均(ms) {row['queue_ms']}/{row['queue_avg_ms']}",
                f"字节 input={row['input_bytes']} sent={row['sent_input_bytes']} wire={row['wire_bytes']} output={row['output_bytes']}",
                "结果码：" + json.dumps(row["codes"], ensure_ascii=False, sort_keys=True),
            ]
        )
    if not report["rows"]:
        lines.append("暂无本地记录。")
    lines.append("请求数包含失败和缓存命中；字节不是 token 或费用，不查询官方账单。")
    return "\n".join(lines)
