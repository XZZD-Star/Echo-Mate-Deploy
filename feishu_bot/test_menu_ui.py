import unittest
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

        self.assertIn("核心服务 5/5 正常", status)
        self.assertIn("SSH 通知 🟢", status)
        self.assertIn("Flow 查看 🟢", status)
        self.assertIn("数据库备份 🟢", status)
        self.assertIn("echomate-ssh-session-watch", detail)
        self.assertIn("echomate-flowview", detail)
        self.assertIn("echomate-db-backup", detail)

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


if __name__ == "__main__":
    unittest.main()
