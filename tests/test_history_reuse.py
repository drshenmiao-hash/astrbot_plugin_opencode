"""Regression tests through real normalization, queue, history cache and rendering.

The ACP peer is in memory: no CLI, child process, SSH, network or account is used.
Fixtures model the AstrBot message shapes reported by the user, not production traces.
"""

import json
import unittest
from collections import deque
from copy import deepcopy
from unittest.mock import patch

from opencode_plugin.engine import Engine
from opencode_plugin.logic import history_state
from opencode_plugin.settings import settings_from
from opencode_plugin.transcript import normalize_messages


def no_process_owner(_cfg):
    raise AssertionError("History regression tests must never launch a process")


class MemoryACP:
    def __init__(self):
        self.closed = False
        self.sessions = []
        self.prompts = []
        self.replies = deque()

    async def new_session(self, model):
        session = f"memory-{len(self.sessions) + 1}"
        self.sessions.append((session, model))
        return session

    async def prompt(self, session, text, *, stats=None):
        self.prompts.append(
            {"session": session, "text": text, "messages": json.loads(text.split("\n\n", 1)[1])}
        )
        reply = self.replies.popleft() if self.replies else "OK"
        # Synthetic counters only; no real ACP frame or billing is measured here.
        wire_bytes = len(text.encode()) + 100
        stats.update(wire_bytes=wire_bytes, output_bytes=len(reply.encode()))
        return {"text": reply, "wire_bytes": wire_bytes}

    async def close(self):
        self.closed = True


def dialogue_after(messages, reply):
    return deepcopy([m for m in messages if m["role"] != "system"]) + [
        {"role": "assistant", "content": reply}
    ]


class HistoryReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = MemoryACP()
        self.engine = Engine(
            settings_from({"model_id": "mock/plan-model"}), owner_factory=no_process_owner
        )
        self.engine.client = self.peer
        self.events = []
        self.record_patch = patch(
            "opencode_plugin.engine.record",
            side_effect=lambda event, **values: self.events.append({"event": event, **values}),
        )
        self.record_patch.start()
        self.process_patch = patch(
            "asyncio.create_subprocess_exec",
            side_effect=AssertionError("No child processes allowed"),
        )
        self.process_patch.start()

    async def asyncTearDown(self):
        try:
            await self.engine.close()
        finally:
            self.process_patch.stop()
            self.record_patch.stop()

    async def submit(self, messages):
        before = deepcopy(messages)
        result = await self.engine.submit(messages, "group")
        self.assertEqual(messages, before, "History matching mutated its input")
        return result

    async def turn(self, system, user, contexts=None):
        messages = normalize_messages(prompt=user, contexts=contexts, system_prompt=system)
        return messages, await self.submit(messages)

    def assert_delta(self, user, operations=None):
        self.assertEqual(len(self.peer.sessions), 1, "Unexpected session rebuild")
        actual = self.peer.prompts[-1]["messages"]
        if operations is not None:
            self.assertEqual([m["role"] for m in actual], ["system", "user"])
            self.assertEqual(json.loads(actual[0]["content"].split("\n", 1)[1]), operations)
            actual = actual[1:]
        self.assertEqual(actual, [{"role": "user", "content": user}])
        self.assertEqual(self.peer.prompts[-1]["session"], self.peer.sessions[0][0])

    def assert_cached(self):
        event = next(e for e in reversed(self.events) if e["event"] == "request_finished")
        self.assertEqual(event["code"], "ok")
        self.assertTrue(event["reused"])
        self.assertEqual(event["reset_reason"], "cached_reply")
        self.assertEqual(event["sent_input_bytes"], 0)
        self.assertEqual(event["wire_bytes"], 0)

    async def test_system_timestamp_changes_every_turn(self):
        history = []
        for index in range(3):
            system = f"你是 Lumi。\n时间：2026-11-15 12:00:0{index}"
            user = f"第 {index + 1} 问"
            messages, reply = await self.turn(system, user, history)
            if index:
                self.assert_delta(user)
                self.assertIn(f"2026-11-15 12:00:0{index}", self.peer.prompts[-1]["text"])
                self.assertNotIn("你是 Lumi。", self.peer.prompts[-1]["text"])
            history = dialogue_after(messages, reply)
        self.assertEqual(len(self.peer.prompts), 3)

    async def test_system_fixed_across_turns(self):
        history = []
        for user in ["第一问", "第二问", "第三问"]:
            messages, reply = await self.turn("固定人格", user, history)
            if history:
                self.assert_delta(user)
            history = dialogue_after(messages, reply)
        self.assertEqual(len(self.peer.prompts), 3)

    async def test_assistant_boundary_whitespace_stripped(self):
        raw_reply = " \n第一轮回复\t\n "
        self.peer.replies.append(raw_reply)
        messages, reply = await self.turn("固定人格", "第一问")
        self.assertEqual(reply, raw_reply)
        await self.turn("固定人格", "第二问", dialogue_after(messages, reply.strip()))
        self.assert_delta("第二问")

    async def test_identical_messages_replayed_with_and_without_reply(self):
        messages, reply = await self.turn("固定人格", "第一问")
        confirmed = messages + [{"role": "assistant", "content": reply}]
        for replay in [messages, confirmed, messages, confirmed]:
            with self.subTest(includes_reply=len(replay) == len(confirmed)):
                self.assertEqual(await self.submit(replay), reply)
                self.assertEqual(len(self.peer.prompts), 1)
                self.assertEqual(len(self.peer.sessions), 1)
                self.assert_cached()

    async def test_second_system_none_withdraws_once(self):
        messages, reply = await self.turn("上一轮人格", "第一问")
        second, reply2 = await self.turn(None, "第二问", dialogue_after(messages, reply))
        self.assertFalse(any(m["role"] == "system" for m in second))
        self.assert_delta("第二问", [{"op": "remove", "index": 0}])
        await self.turn(None, "第三问", dialogue_after(second, reply2))
        self.assert_delta("第三问")

    async def test_system_memory_changes_every_turn(self):
        history = []
        for index, memory in enumerate(["偏好茶", "偏好咖啡", "偏好果汁"]):
            system = f"固定人格\n记忆：{memory}"
            user = f"第 {index + 1} 问"
            messages, reply = await self.turn(system, user, history)
            if index:
                self.assert_delta(user, [{"op": "replace", "index": 0, "content": system}])
            history = dialogue_after(messages, reply)
        self.assertEqual(len(self.peer.prompts), 3)

    async def test_memory_attached_to_current_user_preserves_prior_reminders(self):
        history = []
        for index, memory in enumerate(["茶", "咖啡", "果汁"]):
            user = f"第 {index + 1} 问\n[时间：12:00:0{index}；记忆：{memory}；群昵称：Lumi友]"
            previous = deepcopy(history)
            messages, reply = await self.turn("固定人格", user, history)
            if history:
                self.assert_delta(user)
                self.assertEqual(messages[1:-1], previous)
            history = dialogue_after(messages, reply)
        self.assertEqual(len(self.peer.prompts), 3)

    async def test_system_append_sends_only_suffix(self):
        system = "固定人格\n已有知识卡片"
        suffix = "\n开发说明：AstrBot 插件\nKB：新增卡片"
        messages, reply = await self.turn(system, "第一问")
        await self.turn(system + suffix, "聊一下 AstrBot", dialogue_after(messages, reply))
        self.assert_delta("聊一下 AstrBot", [{"op": "append", "index": 0, "content": suffix}])
        self.assertNotIn(system, self.peer.prompts[-1]["text"])

    async def test_system_only_change_deferred_until_new_user(self):
        messages, reply = await self.turn("固定人格", "第一问")
        history = dialogue_after(messages, reply)
        applied = deepcopy(self.engine.sessions["group"])
        changed = "固定人格\n新 KB"
        system_only = normalize_messages(contexts=history, system_prompt=changed)
        self.assertEqual(await self.submit(system_only), reply)
        self.assert_cached()
        self.assertEqual(self.engine.sessions["group"], applied)
        self.assertEqual(len(self.peer.prompts), 1)
        second, reply2 = await self.turn(changed, "第二问", history)
        self.assert_delta("第二问", [{"op": "append", "index": 0, "content": "\n新 KB"}])
        await self.turn(changed, "第三问", dialogue_after(second, reply2))
        self.assert_delta("第三问")

    async def test_replayed_input_with_system_change_defers_update(self):
        messages, reply = await self.turn("固定人格", "第一问")
        replay = normalize_messages(prompt="第一问", system_prompt="固定人格\n新 KB")
        self.assertEqual(await self.submit(replay), reply)
        self.assert_cached()
        self.assertEqual(len(self.peer.prompts), 1)
        await self.turn("固定人格\n新 KB", "第二问", dialogue_after(messages, reply))
        self.assert_delta("第二问", [{"op": "append", "index": 0, "content": "\n新 KB"}])

    async def assert_cleaned_assistant_reuses(self, raw, cleaned):
        self.peer.replies.append(raw)
        messages, reply = await self.turn("固定人格", "第一问")
        self.assertEqual(reply, raw, "Actual output was normalized")
        await self.turn("固定人格", "第二问", dialogue_after(messages, cleaned))
        self.assert_delta("第二问")
        self.assertEqual(len(self.peer.prompts), 2)

    async def test_assistant_think_blocks_removed(self):
        await self.assert_cleaned_assistant_reuses(
            "\n<think>多行\n思考</think>\n<think>继续思考</think>\n实际回答 \n", "实际回答"
        )

    async def test_assistant_thinking_blocks_removed(self):
        await self.assert_cleaned_assistant_reuses(
            "<thinking>推理\n过程</thinking>\n实际回答", "实际回答"
        )

    async def test_assistant_trailing_think_close_removed(self):
        await self.assert_cleaned_assistant_reuses("实际回答</think> \n", "实际回答")

    async def test_actual_sent_history_keeps_assistant_think_and_whitespace(self):
        raw = " \n<think>原始思考</think>\n原始回答\t "
        contexts = [{"role": "assistant", "content": raw}]
        messages, _ = await self.turn("固定人格", "下一问", contexts)
        self.assertEqual(self.peer.prompts[0]["messages"], messages)
        self.assertEqual(self.peer.prompts[0]["messages"][1]["content"], raw)
        history_state(messages)
        self.assertEqual(contexts[0]["content"], raw)

    async def test_visible_assistant_edit_still_rebuilds(self):
        messages, _ = await self.turn("固定人格", "第一问")
        history = dialogue_after(messages, "回答被实质修改")
        await self.turn("固定人格", "第二问", history)
        self.assertEqual(len(self.peer.sessions), 2)

    async def test_user_think_tags_and_whitespace_are_not_normalized(self):
        messages, reply = await self.turn("固定人格", " <think>这是用户原文</think> 问题 ")
        history = dialogue_after(messages, reply)
        history[0]["content"] = "问题"
        await self.turn("固定人格", "第二问", history)
        self.assertEqual(len(self.peer.sessions), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
