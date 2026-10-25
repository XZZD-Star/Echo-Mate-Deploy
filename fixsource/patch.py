#!/usr/bin/env python3
"""移除天气回复里的「地点来源」提示，这是实现细节。"""

from pathlib import Path

BOT = Path("/root/echomate-deploy/feishu_bot/bot.py")


def patch(path, pairs):
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if new in text:
            print("  skip")
            continue
        if text.count(old) != 1:
            raise SystemExit(
                f"匹配失败：命中 {text.count(old)} 次\n---\n{old[:200]}\n---"
            )
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"  patched: {path.name}")


OLD_TAIL = '''    lines.append("")
    lines.append(f"地点来源：{source}")
    lines.append("")
    lines.append("0 返回")
'''

NEW_TAIL = '''    lines.append("")
    lines.append("0 返回")
'''

OLD_VAR = '''    place = _extract_place(text)
    source = "你说的"

    if not place:
        places = _memory_places()
        if places:
            place = places[0]
            source = "记忆里的"
'''

NEW_VAR = '''    place = _extract_place(text)

    if not place:
        places = _memory_places()
        if places:
            place = places[0]
'''

print("[1/2] 去掉来源变量")
patch(BOT, [(OLD_VAR, NEW_VAR)])

print("[2/2] 去掉来源行")
patch(BOT, [(OLD_TAIL, NEW_TAIL)])

print("done")
