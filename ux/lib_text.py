"""飞书文案精简：公共辅助函数与新文案常量。"""

from pathlib import Path

BOT = Path("/root/echomate-deploy/feishu_bot/bot.py")


def patch(path, pairs):
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if new in text:
            print(f"  skip: {path.name}")
            continue
        if text.count(old) != 1:
            raise SystemExit(
                f"匹配失败：命中 {text.count(old)} 次\n---\n{old[:300]}\n---"
            )
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"  patched: {path.name}")
