#!/usr/bin/env python3
"""修 bot.py 的 SQLite 连接泄漏：with sqlite3.connect 不关闭连接。"""

from pathlib import Path

BOT = Path("/root/echomate-deploy/feishu_bot/bot.py")

OLD_IMPORT = "import subprocess\nimport sys\n"
NEW_IMPORT = (
    "import subprocess\nimport sys\nfrom contextlib import contextmanager\n"
)

OLD_FUNC = "def now_utc():\n"

NEW_FUNC = '''@contextmanager
def db_conn():
    """连接用完必须关闭，否则 WAL 下会累积句柄并持有写锁。"""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_utc():
'''


def patch(path, pairs):
    text = path.read_text(encoding="utf-8")
    for old, new, count in pairs:
        if new in text:
            print("  skip")
            continue
        if text.count(old) != count:
            raise SystemExit(
                f"匹配失败：期望 {count} 次，实际 {text.count(old)} 次\n{old[:120]}"
            )
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"  patched: {path.name}")


patch(
    BOT,
    [
        (OLD_IMPORT, NEW_IMPORT, 1),
        (OLD_FUNC, NEW_FUNC, 1),
        (
            "with sqlite3.connect(DB_PATH, timeout=10) as conn:",
            "with db_conn() as conn:",
            3,
        ),
    ],
)

print("done")
