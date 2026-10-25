import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path("/var/lib/echomate/assistant.db")
BACKUP_DIR = Path("/var/backups/echomate")
KEEP_DAYS = 14


def main():
    if not DB_PATH.is_file():
        raise SystemExit(f"数据库不存在：{DB_PATH}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_DIR / f"assistant_{timestamp}.sqlite3"
    temp = BACKUP_DIR / f".assistant_{timestamp}.tmp"

    try:
        # SQLite 在线备份，可以在应用运行时创建一致性备份。
        with sqlite3.connect(DB_PATH, timeout=30) as source:
            source.execute("PRAGMA busy_timeout=30000")

            with sqlite3.connect(temp, timeout=30) as destination:
                destination.execute("PRAGMA busy_timeout=30000")
                source.backup(destination)

                result = destination.execute(
                    "PRAGMA quick_check"
                ).fetchone()

                if not result or result[0] != "ok":
                    raise RuntimeError("备份完整性检查未通过")

                tables = destination.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()

                print(f"备份数据库表数量：{len(tables)}")

        os.chmod(temp, 0o600)
        os.replace(temp, target)

        # 清理超过 14 天的正式备份，不碰临时文件或源数据库。
        cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)

        for old in BACKUP_DIR.glob("assistant_*.sqlite3"):
            if old == target:
                continue

            modified = datetime.fromtimestamp(
                old.stat().st_mtime, tz=timezone.utc
            )

            if modified < cutoff:
                old.unlink()

        print("备份成功")
        print(f"文件大小：{target.stat().st_size} bytes")
        print(f"保留时间：最近 {KEEP_DAYS} 天")
        print(f"完整性检查：通过")
        print(f"备份路径：{target}")

    finally:
        if temp.exists():
            temp.unlink()


if __name__ == "__main__":
    main()
