#!/usr/bin/env python3
"""复测天气各种问法。"""

import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("h")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)


def show(question):
    print("=" * 54)
    print(f"问：{question}")
    print("-" * 54)
    reply = m.weather_reply(question)
    print(reply if reply is not None else "<交给 AI>")
    print()


show("这段时间天气怎么样")
show("这几天天气怎么样")
show("最近天气怎么样")
show("今天天气怎么样")
show("南昌天气")
show("江西南昌天气")
show("北京天气")
show("瓦坎达天气")
show("你好呀")

print("=" * 54)
print("地名提取自测")
for case in [
    "这段时间天气怎么样",
    "这几天天气怎么样",
    "南昌天气",
    "江西南昌天气",
    "北京天气",
]:
    print(f"  {case!r:18} -> {m._extract_place(case)!r}")
