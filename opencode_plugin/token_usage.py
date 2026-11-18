"""Read counters from the pinned OpenCode CLI's private session database."""

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass

logger = logging.getLogger("astrbot")
TOKEN_FIELDS = ("token_total", "token_input", "token_output", "token_cached")


@dataclass(frozen=True)
class TurnTokens:
    token_total: int
    token_input: int
    token_output: int
    token_cached: int


def _connect(path):
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.15))


def _latest_user(db, session_id):
    row = db.execute(
        """SELECT id FROM message WHERE session_id = ? AND json_extract(data, '$.role') = 'user'
        ORDER BY id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()
    return row[0] if row else ""


def previous_user(path, session_id):
    try:
        with _connect(path) as db:
            return _latest_user(db, session_id)
    except (OSError, sqlite3.Error):
        logger.warning("opencode_token_usage code=read_failed")
        return None


def turn_tokens(path, session_id, previous, model):
    try:
        with _connect(path) as db:
            db.execute("BEGIN")
            user = _latest_user(db, session_id)
            if not user or user == previous:
                return None
            provider, model_id = model.split("/", 1)
            rows = db.execute(
                """SELECT json_extract(p.data, '$.tokens.total'),
                    json_extract(p.data, '$.tokens.input'), json_extract(p.data, '$.tokens.output'),
                    json_extract(p.data, '$.tokens.reasoning'), json_extract(p.data, '$.tokens.cache.read'),
                    json_extract(p.data, '$.tokens.cache.write')
                FROM message AS m JOIN part AS p ON p.message_id = m.id AND p.session_id = m.session_id
                WHERE m.session_id = ? AND json_extract(m.data, '$.role') = 'assistant'
                    AND json_extract(m.data, '$.parentID') = ?
                    AND json_extract(m.data, '$.providerID') = ? AND json_extract(m.data, '$.modelID') = ?
                    AND json_extract(p.data, '$.type') = 'step-finish'""",
                (session_id, user, provider, model_id),
            ).fetchall()
    except (OSError, sqlite3.Error):
        logger.warning("opencode_token_usage code=read_failed")
        return None
    if not rows:
        return None
    total = input_total = output_total = cached_total = 0
    for reported, inputs, output, reasoning, cached, written in rows:
        if any(
            type(value) is not int or not 0 <= value <= 2**53 - 1
            for value in (inputs, output, reasoning, cached, written)
        ):
            return None
        step_input = inputs + cached + written
        step_output = output + reasoning
        if reported is None:
            # OpenCode normalizes missing provider counts to zero as well.
            if not (step_input or step_output):
                return None
            reported = step_input + step_output
        elif type(reported) is not int or not 0 <= reported <= 2**53 - 1:
            return None
        total += reported
        input_total += step_input
        output_total += step_output
        cached_total += cached
    return TurnTokens(total, input_total, output_total, cached_total)
