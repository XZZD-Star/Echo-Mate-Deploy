import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import bot


class MenuUITest(unittest.TestCase):
    def test_service_status_covers_runtime_units(self):
        units = {unit for unit, _, _ in bot.SERVICE_UNITS}
        self.assertEqual(
            units,
            {
                "echomate-server.service",
                "echomate-feishu-bot.service",
                "echomate-monitor.service",
                "echomate-ssh-session-watch.service",
                "echomate-flowview.service",
            },
        )
        self.assertEqual(
            bot.BACKUP_UNIT[0],
            "echomate-db-backup.timer",
        )

    def test_status_pages_show_all_units(self):
        proc = SimpleNamespace(stdout="active\n")
        with patch("bot.subprocess.run", return_value=proc):
            status = bot.render_status()
            detail = bot.render_status_detail()

        self.assertIn("5 个核心服务都在线", status)
        self.assertIn("🗃️ 备份任务", status)
        self.assertIn("🟢 正常", status)
        self.assertIn("CPU：", status)
        self.assertIn("🧰 Echo-Mate 系统详情", detail)
        self.assertIn("🟢 语音服务", detail)
        self.assertIn("🟢 飞书机器人", detail)
        self.assertIn("🟢 数据库备份", detail)
        self.assertNotIn("echomate-", detail)
        self.assertNotIn("active", detail)

    def test_cards_have_title_body_and_footer(self):
        card = bot.build_page_card("menu", bot.menu_text())
        self.assertEqual(card["header"]["title"]["content"], "Echo-Mate")
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["elements"][0]["tag"], "div")
        self.assertNotIn("🐣 Echo-Mate", card["elements"][0]["text"]["content"])
        self.assertIn("**1**", card["elements"][0]["text"]["content"])
        self.assertEqual(card["elements"][-1]["tag"], "note")
        self.assertIn(
            "回复数字进入",
            card["elements"][-1]["elements"][0]["content"],
        )

        card = bot.build_page_card(
            "status",
            "⚠️ 有服务未运行\n\n🔴 语音服务\n\n0 返回",
        )
        self.assertEqual(card["header"]["template"], "orange")

    def test_page_card_falls_back_to_text(self):
        with patch("apps.feishu.messaging.send_card", return_value=False), patch(
            "apps.feishu.messaging.send_text",
            return_value=True,
        ) as fallback:
            self.assertTrue(bot.send_page_card("chat", "status", "body"))
            fallback.assert_called_once_with("chat", "body")

    def test_pager_labels_are_consistent(self):
        lines = bot._pager_lines(2, 18, 8)
        self.assertEqual(
            lines,
            [
                "第 2/3 页 · 共 18 条",
                "8 上一页 · 9 下一页 · 0 返回",
            ],
        )

    def test_main_menu_and_help_keep_primary_entries(self):
        menu = bot.menu_text()
        help_text = bot.render_help()

        for label in (
            "运行状态",
            "待办清单",
            "新建待办",
            "记忆",
            "使用帮助",
            "语音播报",
            "本机 Codex",
        ):
            self.assertIn(label, menu)
        self.assertIn("运行状态", help_text)
        self.assertIn("本机 Codex", help_text)
        self.assertIn("随时回复“菜单”返回主菜单", help_text)

    def test_notify_page_and_records_use_clear_labels(self):
        with patch("bot.notify_store.get_setting", return_value="1"), patch(
            "bot.notify_store.pending_count",
            return_value=2,
        ):
            notify = bot.render_notify()

        rows = [
            {
                "id": 1,
                "kind": "todo",
                "status": "sent",
                "acked_at_utc": None,
                "last_error": None,
            },
            {
                "id": 2,
                "kind": "todo",
                "status": "sent",
                "acked_at_utc": "2026-10-31T12:00:00+00:00",
                "last_error": None,
            },
        ]
        with patch(
            "apps.feishu.views.content._record_total",
            return_value=2,
        ), patch(
            "bot.notify_store.recent_records",
            return_value=rows,
        ):
            records = bot.render_records()

        self.assertIn("待办提醒　已开启", notify)
        self.assertIn("已发送，待确认", records)
        self.assertIn("已播放", records)
        self.assertNotIn("#", records)

    def test_memory_page_hides_internal_ids(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    content TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO memories (owner_id, content) VALUES (?, ?)",
                (bot.OWNER_OPEN_ID, "喜欢简短回答"),
            )
            memory = bot.render_memory(conn)
        finally:
            conn.close()

        self.assertIn("1. 喜欢简短回答", memory)
        self.assertIn("删除第 1 条", memory)
        self.assertNotIn("#", memory)

    def test_memory_candidates_use_display_numbers(self):
        candidates = [{"id": 42, "content": "喜欢简短回答"}]
        conn = sqlite3.connect(":memory:")
        try:
            with patch(
                "bot.echomate_store.list_memory_candidates",
                return_value=candidates,
            ):
                listing = bot.handle_command(conn, "记忆候选", "chat")

            with patch(
                "bot.echomate_store.list_memory_candidates",
                return_value=candidates,
            ), patch(
                "bot.echomate_store.decide_memory_candidate",
                return_value=True,
            ):
                accepted = bot.handle_command(conn, "确认候选 1", "chat")
        finally:
            conn.close()

        self.assertIn("1. 喜欢简短回答", listing)
        self.assertIn("确认候选 1", listing)
        self.assertNotIn("#", listing)
        self.assertIn("候选记忆已写入", accepted)

    def test_chat_memory_page_uses_display_numbers(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                CREATE TABLE chat_memories (
                    id INTEGER PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    source_turn_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'chat',
                    kind TEXT NOT NULL DEFAULT 'chat',
                    content TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    created_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO chat_memories
                    (owner_id, source_turn_id, content, dedupe_key,
                     created_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    bot.OWNER_OPEN_ID,
                    "turn-1",
                    "用户最近在准备比赛",
                    "test",
                    bot.now_utc(),
                ),
            )
            page, state = bot._build_chat_memory_page(conn)
        finally:
            conn.close()

        self.assertIn("1. 用户最近在准备比赛", page)
        self.assertIn("删除聊天记忆 1", page)
        self.assertEqual(len(state["chat_memory_ids"]), 1)
        self.assertNotIn("#", page)

    def test_memory_hub_exposes_both_memory_types(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    content TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE chat_memories (
                    id INTEGER PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at_utc TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO memories (owner_id, content) VALUES (?, ?)",
                (bot.OWNER_OPEN_ID, "喜欢简短回答"),
            )
            text = bot.render_memory_hub(conn)
        finally:
            conn.close()

        self.assertIn("1 · 📚 长期偏好 · 1 条", text)
        self.assertIn("2 · 💬 聊天记忆 · 0 条", text)


if __name__ == "__main__":
    unittest.main()
