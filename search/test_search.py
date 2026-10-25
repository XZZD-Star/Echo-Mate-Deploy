#!/usr/bin/env python3
"""验证联网搜索与工具调用。"""

import sys
import types

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
CHAT = "oc_search_test"

src = open(PATH, encoding="utf-8").read()
cut = src.index("def on_message(")
m = types.ModuleType("h")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)

print("=" * 58)
print("工具直连测试")
print("-" * 58)
result = m._run_tool("web_search", '{"query": "2026年10月 AI 新闻", "max_results": 3}')
print("ok =", result.get("ok"))
print("query =", result.get("query"))
if result.get("answer"):
    print("answer:", result["answer"][:150])
for item in result.get("results", [])[:3]:
    print(f"  - {item['title']}")
    print(f"    {item['url']}")

print()
print("=" * 58)
print("缓存测试（第二次应命中缓存）")
print("-" * 58)
import time

start = time.time()
m._run_tool("web_search", '{"query": "2026年10月 AI 新闻", "max_results": 3}')
print(f"第二次耗时 {time.time() - start:.3f}s")

print()
print("=" * 58)
print("自然对话测试")
print("-" * 58)
for question in [
    "最近有什么 AI 大新闻",
    "Python 3.14 有什么新特性",
    "今天天气怎么样",
]:
    print(f"\n问：{question}")
    print("-" * 58)
    print(m.generate_ai_reply(CHAT, question))
