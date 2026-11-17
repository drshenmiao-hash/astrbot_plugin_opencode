"""Clock noise regressions through real normalization, Engine and sent text."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from opencode_plugin.engine import Engine
from opencode_plugin.logic import system_delta
from opencode_plugin.settings import settings_from
from opencode_plugin.system_clock import current_clock_fields, system_view
from opencode_plugin.transcript import normalize_messages
from tests.test_history_reuse import MemoryACP, dialogue_after


def system(content):
    return [{"role": "system", "content": content}]


class ClockComparisonTests(unittest.TestCase):
    def test_only_clock_changes_produce_no_operations(self):
        self.assertIsNone(
            system_delta(
                system("固定人格\n时间：2026-11-16 04:51"),
                system("固定人格\n时间：2026-11-16 04:52"),
            )
        )

    def test_non_clock_numbers_and_business_dates_are_preserved(self):
        for text in [
            "规则编号：202611160451",
            "预算：2026.11",
            "版本：2026.11.16",
            "会议时间：2026-11-16 04:51",
            "截止时间：2026-11-16 04:51",
            "记忆：订单 2026-11-16 04:51 已成交",
            "时间：123456",
        ]:
            with self.subTest(text=text):
                self.assertEqual(system_view(text)["text"], text)
                self.assertEqual(current_clock_fields(system(text)), [])

    def test_uncertain_mixed_or_invalid_time_is_not_stripped(self):
        for text in [
            "时间：2026-11-16 04:51，规则：不能付款",
            "Current time: 2026-11-16 04:51; preserve this rule",
            "时间：2026-02-30 04:51",
            "时间：2026-11-16 29:99",
            "时区：CST",
            "时区：UTC+25:00",
            "正文提到时间：2026-11-16 04:51",
        ]:
            with self.subTest(text=text):
                self.assertEqual(system_view(text)["text"], text)

    def test_clock_only_system_entry_can_change_position_without_substantive_update(self):
        old = system("固定人格") + system("时间：2026-11-16 04:51")
        new = system("时间：2026-11-16 04:52") + system("固定人格")
        self.assertIsNone(system_delta(old, new))
        self.assertEqual(current_clock_fields(new), ["时间：2026-11-16 04:52"])


class ClockEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = MemoryACP()
        self.engine = Engine(settings_from({"model_id": "mock/plan-model"}))
        self.engine.client = self.peer
        process = patch("asyncio.create_subprocess_exec", side_effect=AssertionError("No real CLI"))
        process.start()
        self.addCleanup(process.stop)

    async def asyncTearDown(self):
        await self.engine.close()

    async def turn(self, content, user, history=None, scope="group"):
        messages = normalize_messages(prompt=user, contexts=history, system_prompt=content)
        before = deepcopy(messages)
        reply = await self.engine.submit(messages, scope)
        self.assertEqual(messages, before)
        return messages, reply

    def operations(self):
        entry = self.peer.prompts[-1]["messages"][0]
        self.assertEqual(entry["role"], "system")
        return json.loads(entry["content"].split("\n", 1)[1])

    async def test_common_clock_formats_reuse_and_send_latest_clock_minimally(self):
        pairs = [
            ("时间：2026-11-16 04:51", "时间：2026-11-16 04:52"),
            ("Current time: 2026-11-16T04:51:00Z", "Current time: 2026-11-16T04:52:01Z"),
            ("系统时间：2026/11/16 04:51:00 UTC+08:00", "系统时间：2026/11/16 04:52:00 UTC+08:00"),
            ("当前日期：2026年11月16日", "当前日期：2026年11月17日"),
            ("时刻：04:51", "时刻：04:52:30"),
            ("Time zone: UTC+08:00", "Time zone: Europe/London"),
            (
                "[时间：2026-11-16 04:51；时区：Asia/Shanghai]",
                "[时间：2026-11-16 04:52；时区：UTC]",
            ),
            ("**Current time**: 2026.11.16 04:51 GMT", "**Current time**: 2026.11.16 04:52 GMT"),
        ]
        for index, (old_clock, new_clock) in enumerate(pairs):
            with self.subTest(format=old_clock):
                scope = f"format-{index}"
                old = "不应重复发送的固定人设\n" + old_clock
                new = "不应重复发送的固定人设\n" + new_clock
                messages, reply = await self.turn(old, "第一问", scope=scope)
                session = self.peer.prompts[-1]["session"]
                await self.turn(new, "第二问", dialogue_after(messages, reply), scope=scope)
                prompt = self.peer.prompts[-1]
                self.assertEqual(prompt["session"], session)
                self.assertEqual(prompt["messages"], [{"role": "user", "content": "第二问"}])
                self.assertIn(new_clock, prompt["text"])
                self.assertNotIn("不应重复发送的固定人设", prompt["text"])
                self.assertEqual(len(self.peer.sessions), index + 1)

    async def test_persona_change_still_replaces_with_raw_latest_system(self):
        old = "你是 Lumi\n时间：2026-11-16 04:51"
        new = "你是新的人设\n时间：2026-11-16 04:52"
        messages, reply = await self.turn(old, "第一问")
        await self.turn(new, "第二问", dialogue_after(messages, reply))
        self.assertEqual(self.operations(), [{"op": "replace", "index": 0, "content": new}])
        self.assertIn("2026-11-16 04:52", self.peer.prompts[-1]["text"])
        self.assertEqual(len(self.peer.sessions), 1)

    async def test_rule_append_despite_clock_change_sends_only_real_suffix(self):
        old = "固定人格\n时间：2026-11-16 04:51\n规则：A"
        new = "固定人格\n时间：2026-11-16 04:52\n规则：A\n规则：B"
        messages, reply = await self.turn(old, "第一问")
        await self.turn(new, "第二问", dialogue_after(messages, reply))
        self.assertEqual(self.operations(), [{"op": "append", "index": 0, "content": "\n规则：B"}])
        self.assertIn("时间：2026-11-16 04:52", self.peer.prompts[-1]["text"])
        self.assertNotIn("固定人格", self.peer.prompts[-1]["text"])

    async def test_memory_and_rule_number_changes_are_not_hidden_as_clock_noise(self):
        old = "固定人格\n时间：2026-11-16 04:51\n记忆：预算 2026，规则编号 451"
        new = "固定人格\n时间：2026-11-16 04:52\n记忆：预算 2027，规则编号 452"
        messages, reply = await self.turn(old, "第一问")
        await self.turn(new, "第二问", dialogue_after(messages, reply))
        self.assertEqual(self.operations()[0], {"op": "replace", "index": 0, "content": new})

    async def test_original_full_system_is_sent_intact_and_input_is_not_mutated(self):
        raw = "固定人格\n  时间：2026-11-16 04:51 UTC+08:00\n规则：不要修改正文"
        messages, reply = await self.turn(raw, "第一问")
        self.assertEqual(self.peer.prompts[0]["messages"], messages)
        self.assertEqual(self.peer.prompts[0]["messages"][0]["content"], raw)
        new = raw.replace("04:51", "04:52")
        after, _ = await self.turn(new, "第二问", dialogue_after(messages, reply))
        self.assertEqual(after[0]["content"], new)
        self.assertEqual(self.peer.prompts[-1]["messages"], [{"role": "user", "content": "第二问"}])
        self.assertIn("04:52", self.peer.prompts[-1]["text"])

    async def test_time_only_without_new_user_sends_nothing_then_uses_latest_clock(self):
        messages, reply = await self.turn("固定人格\n时间：2026-11-16 04:51", "第一问")
        history = dialogue_after(messages, reply)
        only_clock = normalize_messages(
            contexts=history, system_prompt="固定人格\n时间：2026-11-16 04:52"
        )
        self.assertEqual(await self.engine.submit(only_clock, "group"), reply)
        self.assertEqual(len(self.peer.prompts), 1)
        await self.turn("固定人格\n时间：2026-11-16 04:53", "下一问", history)
        self.assertIn("04:53", self.peer.prompts[-1]["text"])
        self.assertEqual(self.peer.prompts[-1]["messages"], [{"role": "user", "content": "下一问"}])


if __name__ == "__main__":
    unittest.main()
