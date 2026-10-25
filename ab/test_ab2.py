#!/usr/bin/env python3
"""复测：地名提取、清空确认。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_ab_test2"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def show(title, text):
    print("=" * 56)
    print(f"【{title}】")
    print("-" * 56)
    print(text if text is not None else "<None>")
    print()


def cmd(text):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        result = m.handle_command(conn, text, CHAT)
        conn.commit()
        return result
    finally:
        conn.close()


print("### 地名候选 ###")
for case in ["江西南昌", "南昌市", "南昌", "南昌市西湖区", "广东深圳"]:
    print(f"  {case!r:16} -> {m._place_candidates(case)}")

print()
print("### 天气 ###")
show("不带地点（用记忆）", m.weather_reply("今天天气怎么样"))
show("带省市", m.weather_reply("江西南昌天气"))
show("带市后缀", m.weather_reply("南昌市天气"))

print("### 清空记忆两步确认 ###")
cmd("记住：临时测试记忆 C")
show("第一步", cmd("清空记忆"))
show("此时记忆数", str(len(m.load_memory_context())))
show("取消", cmd("0"))
show("记忆仍在", str(len(m.load_memory_context())))

cmd("清空记忆")
show("确认清空", cmd("确认清空记忆"))
show("清空后", str(len(m.load_memory_context())))

# 恢复真实记忆
import json

rows = json.load(
    open("/root/echomate-deploy/ab/backup/memories.json")
)
conn = sqlite3.connect(DB, timeout=15)
conn.execute("DELETE FROM memories")
for row in rows:
    conn.execute(
        "INSERT INTO memories(id,owner_id,content,created_at) "
        "VALUES(?,?,?,?)",
        row,
    )
conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (CHAT,))
conn.execute(
    "DELETE FROM app_settings WHERE key = ?", (m.page_key(CHAT),)
)
conn.commit()
print("\n恢复真实记忆：")
for r in conn.execute("SELECT id, content FROM memories ORDER BY id"):
    print(" ", r)
conn.close()
