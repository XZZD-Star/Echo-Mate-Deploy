#!/usr/bin/env python3
"""验证 AI 不再编造待办。"""

import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"

src = open(PATH, encoding="utf-8").read()

# 只执行到 on_message 之前，避免启动飞书长连接。
cut = src.index("def on_message(")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)

print("=== 实际询问 AI ===")
for text in ["帮我加个待办", "我有几个待办？"]:
    print(f"\n用户：{text}")
    reply = m.generate_ai_reply("oc_prompt_test", text)
    print(f"Echo：{reply}")
