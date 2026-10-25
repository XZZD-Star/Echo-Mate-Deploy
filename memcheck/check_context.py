#!/usr/bin/env python3
"""检查「今天天气怎么样」时实际拼出的 messages。"""

import sqlite3
import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_5b3d6c4b7569f760854044c0c5b7ba0d"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)

items = m.load_memory_context()
print("=== load_memory_context 返回 ===")
for item in items:
    print(" -", item)

print()
print("=== memory_system_block 输出 ===")
print(m.memory_system_block(items))

print()
print("=== 判断 ===")
has_city = any("南昌" in item for item in items)
print(f"记忆里有南昌：{has_city}")

if has_city:
    print()
    print("记忆已注入。天气回答没提南昌的原因要看模型：")
    print("prompt 说「与当前问题无关可忽略」，模型可能认为")
    print("「天气」和「居住地」无关，所以没用上这条记忆。")
