#!/usr/bin/env python3
"""证明：已保存的记忆目前不会进入 Qwen 上下文。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_memcheck"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def cmd(text):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        result = m.handle_command(conn, text, CHAT)
        conn.commit()
        return result
    finally:
        conn.close()


print("=== 1. 存一条明确的个人事实 ===")
print(cmd("记住：我的生日是 3 月 14 日"))

print("\n=== 2. 数据库里确实有 ===")
conn = sqlite3.connect(DB, timeout=15)
rows = conn.execute(
    "SELECT id, content FROM memories WHERE owner_id = ? ORDER BY id DESC LIMIT 3",
    (m.OWNER_OPEN_ID,),
).fetchall()
for row in rows:
    print(f"  #{row[0]} {row[1]}")
conn.close()

print("\n=== 3. 问 Qwen 一个只有靠记忆才能答对的问题 ===")
reply = m.generate_ai_reply(CHAT, "我的生日是哪天？")
print(f"Echo：{reply}")

print("\n=== 4. 检查实际发给 Qwen 的 messages 里有没有 memories ===")
import inspect

source = inspect.getsource(m.generate_ai_reply)
has_memories = "memories" in source
print(f"  generate_ai_reply 源码中出现 'memories': {has_memories}")
print(f"  SQL 语句: {'FROM chat_messages' if 'FROM chat_messages' in source else '?'}")

# 清理
conn = sqlite3.connect(DB, timeout=15)
conn.execute(
    "DELETE FROM memories WHERE content = '我的生日是 3 月 14 日'"
)
conn.execute(
    "DELETE FROM chat_messages WHERE chat_id = ?", (CHAT,)
)
conn.execute(
    "DELETE FROM app_settings WHERE key = ?", (m.page_key(CHAT),)
)
conn.commit()
conn.close()
print("\n已清理测试数据")
