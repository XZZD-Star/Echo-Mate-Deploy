#!/usr/bin/env python3
"""调试页面状态读写。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_debug_page"

src = open(PATH, encoding="utf-8").read()
cut = src.index("# ==================== 飞书消息发送 ====================")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def cmd(text):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        before = m.get_page(CHAT, conn=conn)
        result = m.handle_command(conn, text, CHAT)
        conn.commit()
        after = m.get_page(CHAT, conn=conn)
        print(f"cmd={text!r:28} page {before} -> {after}  "
              f"result={'None' if result is None else 'text'}")
        return result
    finally:
        conn.close()


cmd("菜单")
cmd("6")
cmd("2")
cmd("6")
cmd("4")

conn = sqlite3.connect(DB, timeout=15)
row = conn.execute(
    "SELECT value FROM app_settings WHERE key = ?",
    (m.page_key(CHAT),),
).fetchone()
conn.close()
print("raw stored:", row)
