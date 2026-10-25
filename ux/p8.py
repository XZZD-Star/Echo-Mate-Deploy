#!/usr/bin/env python3
"""修 monitor.py 的 SQLite 连接泄漏（75 个句柄持有写锁）。"""

from pathlib import Path

MONITOR = Path("/root/echomate-deploy/monitor/monitor.py")

OLD_IMPORT = "import argparse\nimport sqlite3\n"
NEW_IMPORT = (
    "import argparse\nimport sqlite3\nfrom contextlib import contextmanager\n"
)

OLD_CONNECT = '''def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
'''

NEW_CONNECT = '''@contextmanager
def db_connect():
    """连接用完必须关闭，否则 WAL 下会累积句柄并长期持有写锁。"""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA busy_timeout=5000")
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
            raise SystemExit(f"匹配失败：命中 {text.count(old)} 次")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"  patched: {path.name}")


patch(MONITOR, [(OLD_IMPORT, NEW_IMPORT), (OLD_CONNECT, NEW_CONNECT)])
print("done")
