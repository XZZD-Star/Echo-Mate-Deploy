#!/usr/bin/env python3
"""渲染全部页面，检查排版。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_preview_nav_test"

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


show("主菜单", cmd("菜单"))
show("状态页", cmd("1"))
show("状态详情", cmd("2"))
show("0 返回状态页", cmd("0"))
show("0 回主菜单", cmd("0"))
show("待办页", cmd("2"))
show("新待办页", cmd("3"))
show("0 回主菜单", cmd("0"))
show("记忆页", cmd("4"))
show("记忆全部", cmd("1"))
show("0 回主菜单", cmd("0"))
show("帮助页", cmd("5"))
show("0 回主菜单", cmd("0"))
show("播报页", cmd("6"))
show("静音子页", cmd("1"))
show("0 回播报", cmd("0"))
show("播报记录", cmd("4"))
show("0 回播报", cmd("0"))
show("0 回主菜单", cmd("0"))

print("=" * 54)
print("当前页面状态:", m.get_page(CHAT))
