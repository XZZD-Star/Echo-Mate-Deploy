import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_runtime


class FakeCompletions:
    def __init__(self, arguments):
        self.arguments = arguments
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(
                name=kwargs["tool_choice"]["function"]["name"],
                arguments=json.dumps(self.arguments, ensure_ascii=False),
            ),
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[call],
                    )
                )
            ]
        )


class FakeClient:
    def __init__(self, arguments):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(arguments)
        )


class AgentRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(
            2026, 10, 29, 11, 20, tzinfo=timezone(timedelta(hours=8))
        )

    def test_planner_extracts_todo_action(self):
        client = FakeClient({
            "actions": [{
                "name": "add_todo",
                "arguments": {
                    "content": "起床",
                    "due_at": "2026-10-29T11:24:00+08:00",
                },
            }],
        })
        plan = agent_runtime.plan_actions(
            client,
            "qwen-max",
            "11.24的时候提醒我起床",
            self.now,
        )
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(plan["actions"][0]["name"], "add_todo")
        self.assertEqual(
            plan["actions"][0]["arguments"]["title"], "起床"
        )
        request_messages = client.chat.completions.calls[0]["messages"]
        self.assertEqual(len(request_messages), 2)
        self.assertNotIn(
            "历史提醒",
            "\n".join(item["content"] for item in request_messages),
        )

    def test_explicit_clock_parser_handles_dot_and_empty_minute(self):
        first = agent_runtime.parse_explicit_reminder(
            "11.58提醒我去吃饭哦",
            self.now,
        )
        second = agent_runtime.parse_explicit_reminder(
            "12.提醒我去吃饭",
            self.now,
        )
        tomorrow = agent_runtime.parse_explicit_reminder(
            "明天11.24的时候提醒我起床",
            self.now,
        )
        self.assertEqual(
            first["arguments"]["due_at"],
            "2026-10-29T11:58:00+08:00",
        )
        self.assertEqual(
            second["arguments"]["due_at"],
            "2026-10-29T12:00:00+08:00",
        )
        self.assertEqual(
            tomorrow["arguments"]["due_at"],
            "2026-10-30T11:24:00+08:00",
        )

    def test_planner_deduplicates_identical_actions(self):
        action = {
            "name": "add_todo",
            "arguments": {
                "title": "吃饭",
                "due_at": "2026-10-29T11:58:00+08:00",
            },
        }
        client = FakeClient({"actions": [action, action, action]})
        plan = agent_runtime.plan_actions(
            client,
            "qwen-max",
            "11.58提醒我吃饭",
            self.now,
        )
        self.assertEqual(len(plan["actions"]), 1)

    def test_planner_asks_to_split_distinct_writes(self):
        client = FakeClient({
            "actions": [
                {
                    "name": "add_todo",
                    "arguments": {
                        "title": "吃饭",
                        "due_at": "2026-10-29T11:58:00+08:00",
                    },
                },
                {
                    "name": "add_todo",
                    "arguments": {
                        "title": "开会",
                        "due_at": "2026-10-29T12:30:00+08:00",
                    },
                },
            ],
        })
        plan = agent_runtime.plan_actions(
            client,
            "qwen-max",
            "11.58和12.30提醒我",
            self.now,
        )
        self.assertEqual(plan["actions"], [])
        self.assertIn("一次只处理一个", plan["clarification"])

    def test_plain_success_claim_is_detected(self):
        self.assertTrue(agent_runtime.looks_like_completion_claim(
            "好的，已经为你设置了提醒。"
        ))
        self.assertTrue(agent_runtime.looks_like_completion_claim(
            "我会在 11:24 提醒你起床。"
        ))

    def test_claim_validation_rejects_missing_tool_result(self):
        runs = [agent_runtime.normalize_tool_run(
            "add_todo",
            {
                "ok": False,
                "error": "past_due",
            },
        )]
        with self.assertRaises(agent_runtime.AgentProtocolError):
            agent_runtime.validate_reply(
                "已经设置好了，11:24 提醒你。",
                [{
                    "tool": "add_todo",
                    "ok": True,
                    "object_id": 62,
                }],
                runs,
            )

    def test_claim_validation_accepts_grounded_reply(self):
        runs = [agent_runtime.normalize_tool_run(
            "add_todo",
            {
                "ok": True,
                "todo_id": 62,
                "title": "起床",
                "due_text": "2026-10-29 11:24",
            },
        )]
        reply = agent_runtime.validate_reply(
            "好，11:24 的起床提醒已经创建，编号 #62。",
            [{
                "tool": "add_todo",
                "ok": True,
                "object_id": 62,
            }],
            runs,
        )
        self.assertIn("11:24", reply)

    def test_claim_validation_rejects_wrong_time(self):
        runs = [agent_runtime.normalize_tool_run(
            "add_todo",
            {
                "ok": True,
                "todo_id": 62,
                "title": "起床",
                "due_text": "2026-10-29 11:24",
            },
        )]
        with self.assertRaises(agent_runtime.AgentProtocolError):
            agent_runtime.validate_reply(
                "已经创建，11:42 提醒你 #62。",
                [{
                    "tool": "add_todo",
                    "ok": True,
                    "object_id": 62,
                }],
                runs,
            )

    def test_history_sanitizer_removes_unverified_completion(self):
        history = [
            {"role": "user", "content": "11.24的时候提醒我起床"},
            {"role": "assistant", "content": "好的，已经设置了提醒。"},
            {"role": "assistant", "content": "今天天气不错。"},
        ]
        sanitized = agent_runtime.sanitize_history(history)
        self.assertEqual(len(sanitized), 2)
        self.assertEqual(sanitized[-1]["content"], "今天天气不错。")

    def test_fallback_marks_failed_action_as_not_created(self):
        run = agent_runtime.normalize_tool_run(
            "add_todo",
            {
                "ok": False,
                "error": "past_due",
                "due_text": "2026-10-29 11:58",
                "current_time": "2026-10-29 12:00",
            },
        )
        reply = agent_runtime.fallback_action_reply([run, run])
        self.assertIn("没有创建", reply)
        self.assertNotIn("已经设置", reply)
        self.assertEqual(
            reply.count("这个提醒没有创建"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
