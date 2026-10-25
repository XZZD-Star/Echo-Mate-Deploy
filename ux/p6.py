#!/usr/bin/env python3
"""第 6 批：斜杠日期给出友好提示，而不是落到 AI 聊天。"""

from pathlib import Path

BOT = Path("/root/echomate-deploy/feishu_bot/bot.py")

OLD = '''    if text in {"添加待办", "新建待办"}:
        return (
            "新待办这样发\\n\\n"
            "添加待办 2026-10-25 15:00 检查 VPS"
        )

    match = re.match(
        r"^添加待办\\s+(\\d{4}-\\d{2}-\\d{2})\\s+"
        r"(\\d{2}:\\d{2})\\s+(.+)$",
        text,
        re.DOTALL,
    )
'''

NEW = '''    if text in {"添加待办", "新建待办"}:
        return (
            "新待办这样发\\n\\n"
            "添加待办 2026-10-25 15:00 检查 VPS"
        )

    # 看起来像在加待办但格式不对时，直接提示，不要落到 AI 聊天。
    if text.startswith(("添加待办", "新建待办")) and not re.match(
        r"^添加待办\\s+\\d{4}-\\d{2}-\\d{2}\\s+"
        r"\\d{2}:\\d{2}\\s+.+$",
        text,
        re.DOTALL,
    ):
        return (
            "😅 格式差一点\\n\\n"
            "添加待办 2026-10-25 15:00 检查 VPS"
        )

    match = re.match(
        r"^添加待办\\s+(\\d{4}-\\d{2}-\\d{2})\\s+"
        r"(\\d{2}:\\d{2})\\s+(.+)$",
        text,
        re.DOTALL,
    )
'''

text = BOT.read_text(encoding="utf-8")

if NEW in text:
    print("already applied")
else:
    if text.count(OLD) != 1:
        raise SystemExit(f"匹配失败：命中 {text.count(OLD)} 次")
    BOT.write_text(text.replace(OLD, NEW), encoding="utf-8")
    print("patched: bot.py")
