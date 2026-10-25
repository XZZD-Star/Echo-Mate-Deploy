#!/usr/bin/env python3
"""渲染所有飞书文案的实际效果，不发送消息。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_5b3d6c4b7569f760854044c0c5b7ba0d"

src = open(PATH, encoding="utf-8").read()
cut = src.index("# ==================== 飞书消息发送 ====================")

m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def show(title, text):
    print("=" * 52)
    print(f"【{title}】")
    print("-" * 52)
    print(text)
    print()


conn = sqlite3.connect(DB, timeout=10)

show("菜单", m.menu_text())
show("帮助", m.help_text())
show("VPS 状态", m.vps_status_text())
show("播报设置", m.notify_settings_text())
show("播报记录（空）", m.notify_records_text())

show("待办列表（空）", m.handle_command(conn, "我的待办", CHAT))
show("新待办提示", m.handle_command(conn, "添加待办", CHAT))
show("错误：时间已过", m.handle_command(
    conn, "添加待办 2020-01-01 10:00 测试", CHAT))
show("错误：格式错", m.handle_command(
    conn, "添加待办 2026/10/25 10:00 测试", CHAT))
show("错误：无此待办", m.handle_command(conn, "完成待办 999", CHAT))

show("记忆列表（空）", m.handle_command(conn, "列出记忆", CHAT))
show("错误：查无记忆", m.handle_command(conn, "查记忆 不存在的词", CHAT))
show("错误：删除无此记忆", m.handle_command(conn, "删除记忆 999", CHAT))

show("静音格式错", m.handle_command(conn, "静音 25:00 08:00", CHAT))
show("静音起止相同", m.handle_command(conn, "静音 10:00 10:00", CHAT))
show("静音成功", m.handle_command(conn, "静音 23:00 08:00", CHAT))
show("关闭静音", m.handle_command(conn, "关闭静音", CHAT))
show("关闭待办播报", m.handle_command(conn, "关闭待办播报", CHAT))
show("开启待办播报", m.handle_command(conn, "开启待办播报", CHAT))

# 临时建一条待办，展示创建/列表/完成三段效果。
show("创建待办", m.handle_command(
    conn, "添加待办 2026-10-26 09:30 交周报", CHAT))
show("待办列表", m.handle_command(conn, "我的待办", CHAT))

row = conn.execute(
    "SELECT id FROM todos WHERE title='交周报' ORDER BY id DESC LIMIT 1"
).fetchone()
if row:
    show("完成待办", m.handle_command(conn, f"完成待办 {row[0]}", CHAT))
    conn.execute("DELETE FROM todos WHERE id = ?", (row[0],))
    conn.commit()

conn.close()
