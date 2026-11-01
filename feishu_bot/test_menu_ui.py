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

        self.assertIn("核心服务 5/5", status)
        self.assertIn("语音服务  正常", status)
        self.assertIn("SSH 通知  正常", status)
        self.assertIn("Flow 查看  正常", status)
        self.assertIn("定时任务", status)
        self.assertIn("数据库备份  正常", status)
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
        with patch("bot.send_card", return_value=False), patch(
            "bot.send_text",
            return_value=True,
        ) as fallback:
            self.assertTrue(bot.send_page_card("chat", "status", "body"))
            fallback.assert_called_once_with("chat", "body")

    def test_main_menu_and_help_keep_primary_entries(self):
        menu = bot.menu_text()
        help_text = bot.render_help()

        for label in (
            "运行状态",
            "待办清单",
            "新建待办",
            "长期记忆",
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
        with patch("bot.notify_store.recent_records", return_value=rows):
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


if __name__ == "__main__":
    unittest.main()
