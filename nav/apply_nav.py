#!/usr/bin/env python3
"""把 bot.py 的菜单/页面部分整体替换为 v6 设计。"""

from pathlib import Path

BOT = Path("/root/echomate-deploy/feishu_bot/bot.py")
NEW = Path("/root/echomate-deploy/nav/new_block.py")

START = "def menu_text():\n"
END = "# ==================== 飞书消息发送 ===================="

text = BOT.read_text(encoding="utf-8")
block = NEW.read_text(encoding="utf-8")

if "🐣 Echo" in text and "PAGE_PARENT" in text:
    print("already applied")
    raise SystemExit(0)

start_index = text.index(START)
end_index = text.index(END)

new_text = text[:start_index] + block + text[end_index:]

BOT.write_text(new_text, encoding="utf-8")
print("replaced block")
print(f"removed {end_index - start_index} chars, added {len(block)} chars")
