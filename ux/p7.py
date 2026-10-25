#!/usr/bin/env python3
"""修 SQLite 连接泄漏：with 语句不关闭连接，必须显式 close。

改为 contextmanager，with 退出时 commit/rollback 并 close。
"""

from pathlib import Path

SERVER = Path(
    "/root/echomate-deploy/app/Demo/AIChat_demo/Server/tools/notify_store.py"
)

OLD_IMPORT = "import sqlite3\nimport time\n"
NEW_IMPORT = "import sqlite3\nimport time\nfrom contextlib import contextmanager\n"

OLD_CONNECT = '''def _connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn
'''

NEW_CONNECT = '''@contextmanager
def _connect():
    """连接用完必须关闭，否则 WAL 下会不断累积句柄并导致 database is locked。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
'''


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


patch(SERVER, [(OLD_IMPORT, NEW_IMPORT), (OLD_CONNECT, NEW_CONNECT)])
print("done")
