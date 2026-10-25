#!/usr/bin/env python3
"""验证 Qwen 工具调用：天气/待办/记忆。"""

import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
CHAT = "oc_tools_test"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("h")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)

QUESTIONS = [
    "今天天气怎么样",
    "后面几天天气会不会变冷",
    "南昌和北京哪里更热",
    "明天下雨吗，要带伞吗",
    "我有哪些待办",
    "你还记得我住在哪吗",
    "你好呀，随便聊聊",
]

for question in QUESTIONS:
    print("=" * 58)
    print(f"问：{question}")
    print("-" * 58)
    try:
        print(m.generate_ai_reply(CHAT, question))
    except Exception as exc:
        print(f"<异常 {type(exc).__name__}: {exc}>")
    print()
