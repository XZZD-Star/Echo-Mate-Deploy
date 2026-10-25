#!/usr/bin/env python3
"""验证待办修复：自然输入、全角冒号、意图拦截、严格格式。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_todofix_test"

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


def hint(text):
    """模拟 on_message 的兜底链。"""
    result = cmd(text)
    if result is None:
        result = m.todo_hint_reply(text)
    return result


cmd("菜单")
cmd("3")

show("全角冒号（问题现场）", hint("2026-10-25 14：54 呼叫林梦雨"))
show("点号（问题现场）", hint("添加待办 2026-10-25 11.32 向林梦雨问好"))
show("半角冒号无前缀", hint("2026-10-26 09:30 交周报"))
show("今天 格式", hint("明天 9:05 开会"))
show("仅时间（待办页）", hint("15:30 打电话"))
show("严格格式", hint("添加待办 2026-10-27 10:00 检查 VPS"))
show("意图词但格式不对", hint("提醒我一下交房租"))
show("待办页", cmd("2"))

print("=" * 54)
print("解析器自测")
for case in [
    "2026-10-25 14：54 呼叫林梦雨",
    "2026-10-25 11.32 向林梦雨问好",
    "今天 9:05 开会",
    "明天 9：05 开会",
    "15:30 打电话",
]:
    print(f"  {case!r:34} -> {m._parse_natural_todo(case)}")

print()
print("意图判定")
for case in ["提醒我一下交房租", "今天天气怎么样", "你好呀"]:
    print(f"  {case!r:20} -> {m._looks_like_todo(case)}")

# 清理本次测试数据
conn = sqlite3.connect(DB, timeout=15)
rows = conn.execute(
    "SELECT id FROM todos WHERE chat_id = ?", (CHAT,)
).fetchall()
for (todo_id,) in rows:
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
conn.execute(
    "DELETE FROM app_settings WHERE key = ?", (m.page_key(CHAT),)
)
conn.commit()
print(f"\n清理 {len(rows)} 条测试待办")
conn.close()
