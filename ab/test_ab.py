#!/usr/bin/env python3
"""验证天气、记忆使用规则、记忆删除。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_ab_test"
REAL = "oc_5b3d6c4b7569f760854044c0c5b7ba0d"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def show(title, text):
    print("=" * 56)
    print(f"【{title}】")
    print("-" * 56)
    print(text if text is not None else "<None，交给下一层>")
    print()


def cmd(text, chat=CHAT):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        result = m.handle_command(conn, text, chat)
        conn.commit()
        return result
    finally:
        conn.close()


print("### A. 记忆使用规则 ###")
print("当前记忆：")
for item in m.load_memory_context():
    print("  -", item)
print()
print(m.memory_system_block(m.load_memory_context())[:400])
print()

print("### B. 天气 ###")
show("记忆里的居住地（问句不带地点）", m.weather_reply("今天天气怎么样"))
show("问句带地点", m.weather_reply("南昌天气怎么样"))
show("带省市", m.weather_reply("江西南昌天气"))
show("带市后缀", m.weather_reply("南昌市天气"))
show("不存在的城市", m.weather_reply("瓦坎达天气"))
show("不是天气问题", m.weather_reply("你好呀"))

print("### C. 记忆删除 ###")
print("存入测试记忆：")
print(" ", cmd("记住：测试删除用的记忆 A").split("\n")[0])
print(" ", cmd("记住：测试删除用的记忆 B").split("\n")[0])

conn = sqlite3.connect(DB, timeout=15)
rows = conn.execute(
    "SELECT id, content FROM memories WHERE content LIKE '测试删除用的记忆%'"
).fetchall()
conn.close()
print("  新记录：", rows)

if rows:
    show("删除一条", cmd(f"删除记忆 {rows[0][0]}"))
    show("删除不存在的", cmd("删除记忆 99999"))
    show("记忆页", cmd("4"))
    show("清空记忆", cmd("清空记忆"))
    show("清空后再看", cmd("4"))

print("### D. 恢复真实记忆 ###")
print("  当前记忆：")
for item in m.load_memory_context():
    print("  -", item)

# 清理测试会话
conn = sqlite3.connect(DB, timeout=15)
conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (CHAT,))
conn.execute(
    "DELETE FROM app_settings WHERE key = ?", (m.page_key(CHAT),)
)
conn.commit()
conn.close()
print("\n测试会话已清理")
