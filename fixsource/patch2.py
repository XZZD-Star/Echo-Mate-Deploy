#!/usr/bin/env python3
"""精确删除天气尾段的来源行。"""

from pathlib import Path

BOT = Path("/root/echomate-deploy/feishu_bot/bot.py")

OLD = '''    lines.append("")
    lines.append(f"地点来源：{source}")
    lines.append("")
    lines.append("0 返回")
'''

NEW = '''    lines.append("")
    lines.append("0 返回")
'''

text = BOT.read_text(encoding="utf-8")

if NEW in text and "地点来源" not in text:
    print("already fixed")
else:
    if text.count(OLD) != 1:
        raise SystemExit(
            f"匹配失败：命中 {text.count(OLD)} 次"
        )
    BOT.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print("fixed")
