#!/usr/bin/env python3
"""验证记忆注入：存的事实能被 Qwen 用上。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_memfix_test"

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


print("=" * 56)
print("1. 存入几条不同类型的事实")
print("-" * 56)
facts = [
    "记住：我的生日是 3 月 14 日",
    "记住：我在杭州工作，做嵌入式开发",
    "记住：我的猫叫豆豆，是只橘猫",
]
for fact in facts:
    print(" ", cmd(fact).split("\n")[0])

print("\n" + "=" * 56)
print("2. 读取记忆上下文")
print("-" * 56)
items = m.load_memory_context()
for item in items:
    print(f"  - {item}")

print("\n" + "=" * 56)
print("3. 生成 system prompt 片段")
print("-" * 56)
print(m.memory_system_block(items))

print("\n" + "=" * 56)
print("4. 实际问 Qwen")
print("-" * 56)
questions = [
    "我的生日是哪天？",
    "我在哪个城市工作？",
    "我的猫叫什么？",
]
for question in questions:
    print(f"\n用户：{question}")
    print(f"Echo：{m.generate_ai_reply(CHAT, question)}")

print("\n" + "=" * 56)
print("5. 去重验证")
print("-" * 56)
cmd("记住：我的生日是 3 月 14 日")
cmd("记住：我的生日是3月14日")
items = m.load_memory_context()
birthday = [i for i in items if "生日" in i]
print(f"  重复存入 2 次后，生日相关记忆条数：{len(birthday)}")

print("\n" + "=" * 56)
print("清理")
print("-" * 56)
conn = sqlite3.connect(DB, timeout=15)
conn.execute(
    "DELETE FROM memories WHERE content IN (?, ?, ?)", tuple(
        f.split("记住：", 1)[1] for f in facts
    )
)
conn.execute(
    "DELETE FROM memories WHERE content LIKE '我的生日是%'"
)
conn.execute(
    "DELETE FROM chat_messages WHERE chat_id = ?", (CHAT,)
)
conn.execute(
    "DELETE FROM app_settings WHERE key = ?", (m.page_key(CHAT),)
)
conn.commit()
print("  剩余记忆：", conn.execute(
    "SELECT COUNT(*) FROM memories").fetchone()[0])
conn.close()
