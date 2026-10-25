#!/usr/bin/env python3
"""复测：明天天气、待办承诺。"""

import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
CHAT = "oc_tools_test2"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("h")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)

for question in [
    "明天下雨吗，要带伞吗",
    "后天天气怎么样",
    "我有哪些待办",
]:
    print("=" * 58)
    print(f"问：{question}")
    print("-" * 58)
    print(m.generate_ai_reply(CHAT, question))
    print()

print("=" * 58)
print("直接测工具 offset")
print(m._run_tool("get_weather", '{"day_offset": 1, "days": 1}'))
