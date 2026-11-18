"""Exercise OpenCode's step counters with a real private SQLite fixture."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from opencode_plugin.token_usage import TurnTokens, previous_user, turn_tokens


class TokenSourceTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "opencode.db"
        self.db = self.enterContext(closing(sqlite3.connect(self.path)))
        self.db.executescript("""
            CREATE TABLE message(id TEXT PRIMARY KEY, session_id TEXT, data TEXT);
            CREATE TABLE part(message_id TEXT, session_id TEXT, data TEXT);
        """)

    def add_user(self, message_id, *, session="chat"):
        self.db.execute(
            'INSERT INTO message VALUES (?, ?, \'{"role":"user"}\')', (message_id, session)
        )
        self.db.commit()

    def add_steps(self, message_id, parent, model, *steps, session="chat"):
        assistant = {
            "role": "assistant",
            "parentID": parent,
            "providerID": "mock",
            "modelID": model,
        }
        self.db.execute(
            "INSERT INTO message VALUES (?, ?, ?)", (message_id, session, json.dumps(assistant))
        )
        self.db.executemany(
            "INSERT INTO part VALUES (?, ?, ?)",
            [
                (message_id, session, json.dumps({"type": "step-finish", "tokens": step}))
                for step in steps
            ],
        )
        self.db.commit()

    def test_counts_only_current_turn_and_selected_model_including_cache_and_reasoning(self):
        step = {
            "input": 3,
            "output": 2,
            "reasoning": 1,
            "cache": {"read": 5, "write": 2},
            "total": 13,
        }
        self.add_user("01")
        self.add_steps("a1", "01", "plan", step)
        previous = previous_user(self.path, "chat")
        self.assertIsNone(turn_tokens(self.path, "chat", previous, "mock/plan"))
        self.add_user("02")
        self.add_steps("a2", "02", "plan", step, step)
        self.add_steps("a3", "02", "background", step)
        self.add_user("03", session="other")
        self.add_steps("a4", "03", "plan", step, session="other")
        self.assertEqual(
            turn_tokens(self.path, "chat", previous, "mock/plan"), TurnTokens(26, 20, 6, 10)
        )

    def test_missing_counts_are_unknown_and_a_reported_zero_is_known(self):
        step = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
        self.add_user("01")
        self.add_steps("a1", "01", "plan", step)
        self.assertIsNone(turn_tokens(self.path, "chat", "", "mock/plan"))
        self.add_user("02")
        self.add_steps("a2", "02", "plan", {**step, "total": 0})
        self.assertEqual(turn_tokens(self.path, "chat", "01", "mock/plan"), TurnTokens(0, 0, 0, 0))
