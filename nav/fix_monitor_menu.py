#!/usr/bin/env python3
"""修正 monitor.py 中误替换成模块级的 MENU 常量。"""

from pathlib import Path

MONITOR = Path("/root/echomate-deploy/monitor/monitor.py")

OLD = '''MENU = (
    "🐣 Echo\\n\\n"
    "1 · 📊 状态\\n"
    "2 · 📋 待办\\n"
    "3 · ➕ 新待办\\n"
    "4 · 📚 记忆\\n"
    "5 · 💡 帮助\\n"
    "6 · 🔊 播报"
)

    if not send_feishu(menu):
        return
'''

NEW = '''    menu = (
        "🐣 Echo\\n\\n"
        "1 · 📊 状态\\n"
        "2 · 📋 待办\\n"
        "3 · ➕ 新待办\\n"
        "4 · 📚 记忆\\n"
        "5 · 💡 帮助\\n"
        "6 · 🔊 播报"
    )

    if not send_feishu(menu):
        return
'''

text = MONITOR.read_text(encoding="utf-8")

if NEW in text:
    print("already fixed")
else:
    if text.count(OLD) != 1:
        raise SystemExit(f"匹配失败：命中 {text.count(OLD)} 次")
    MONITOR.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print("fixed")
