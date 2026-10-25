#!/usr/bin/env python3
"""用未来日期验证现场案例，并清理被 AI 污染的历史。"""

import sqlite3
import sys
import types
from datetime import datetime, timedelta

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_verify_case"
REAL_CHAT = "oc_5b3d6c4b7569f760854044c0c5b7ba0d"

src = open(PATH, encoding="utf-8").read()
cut = src.index("# ==================== 飞书消息发送 ====================")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def cmd(text, chat=CHAT):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        result = m.handle_command(conn, text, chat)
        conn.commit()
        return result
    finally:
        conn.close()


future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

print("=" * 54)
print("现场案例（改为未来日期）")
print("-" * 54)
for case in [
    f"{future} 14：54 呼叫林梦雨",
    f"添加待办 {future} 11.32 向林梦雨问好",
]:
    print(f"\n输入：{case}")
    print(cmd(case))

print("\n" + "=" * 54)
print("待办列表")
print("-" * 54)
print(cmd("2"))

print("\n" + "=" * 54)
print("清理测试待办")
conn = sqlite3.connect(DB, timeout=15)
rows = conn.execute(
    "SELECT id FROM todos WHERE chat_id = ?", (CHAT,)
).fetchall()
for (todo_id,) in rows:
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
conn.execute(
    "DELETE FROM app_settings WHERE key = ?", (m.page_key(CHAT),)
)

print("\n" + "=" * 54)
print("清理被 AI 幻觉污染的历史")
bad = conn.execute(
    """
    SELECT id, role, substr(content,1,50)
    FROM chat_messages
    WHERE chat_id = ? AND role = 'assistant'
      AND content LIKE '%已经帮你添加%'
    """,
    (REAL_CHAT,),
).fetchall()

for row in bad:
    print(f"  删除 #{row[0]} {row[1]} | {row[2]}")
    conn.execute("DELETE FROM chat_messages WHERE id = ?", (row[0],))

conn.commit()
print(f"\n删除 {len(rows)} 条测试待办、{len(bad)} 条幻觉回复")

left = conn.execute(
    "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ?",
    (REAL_CHAT,),
).fetchone()[0]
print(f"真实会话剩余历史 {left} 条")
conn.close()
