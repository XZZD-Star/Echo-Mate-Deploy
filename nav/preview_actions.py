#!/usr/bin/env python3
"""验证操作反馈与错误提示文案。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_preview_action_test"

src = open(PATH, encoding="utf-8").read()
cut = src.index("# ==================== 飞书消息发送 ====================")

m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def show(title, text):
    print("=" * 54)
    print(f"【{title}】")
    print("-" * 54)
    print(text)
    print()


def cmd(text):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        result = m.handle_command(conn, text, CHAT)
        conn.commit()
        return result
    finally:
        conn.close()


cmd("菜单")

show("格式错", cmd("添加待办 2026/10/26 09:30 测试"))
show("时间已过", cmd("添加待办 2020-01-01 10:00 测试"))
show("新建待办", cmd("添加待办 2026-10-26 09:30 交周报"))
show("待办页", cmd("2"))
show("完成不存在", cmd("完成待办 999"))

conn = sqlite3.connect(DB, timeout=15)
row = conn.execute(
    "SELECT id FROM todos WHERE title='交周报' ORDER BY id DESC LIMIT 1"
).fetchone()
conn.close()

if row:
    show("完成待办", cmd(f"完成待办 {row[0]}"))

show("记住", cmd("记住：偏好简洁回复"))
show("查记忆命中", cmd("查记忆 简洁"))
show("查记忆未命中", cmd("查记忆 不存在"))
show("删除记忆", cmd("删除记忆 999"))
show("静音成功", cmd("静音 23:00 08:00"))
show("静音格式错", cmd("静音 25:00 08:00"))
show("关闭静音", cmd("关闭静音"))
show("关闭待办播报", cmd("关闭待办播报"))
show("播报页（待办已关）", cmd("6"))
show("开启待办播报", cmd("2"))

conn = sqlite3.connect(DB, timeout=15)
conn.execute("DELETE FROM memories WHERE content='偏好简洁回复'")
if row:
    conn.execute("DELETE FROM todos WHERE id = ?", (row[0],))
conn.commit()
conn.close()

print("清理完成")
