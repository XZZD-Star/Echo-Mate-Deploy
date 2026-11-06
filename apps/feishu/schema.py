import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from echomate_core.db import open_connection

from .runtime import DB_PATH


def init_deploy_schema(db_path):
    """Create Feishu event-deduplication tables."""
    db_path = Path(db_path)
    conn = open_connection(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            );
            """
        )
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat(timespec="seconds")
        conn.execute(
            "DELETE FROM processed_events WHERE processed_at < ?",
            (cutoff,),
        )
        conn.commit()
    finally:
        conn.close()

    if db_path.exists():
        os.chmod(db_path, 0o600)


def init_db():
    init_deploy_schema(DB_PATH)


def init_chat_table():
    """建立独立聊天历史表，不修改已有记忆和待办数据。"""
    conn = open_connection(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL
                    CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_messages_lookup
                ON chat_messages(owner_id, chat_id, id);
        """)
        conn.commit()
    finally:
        conn.close()

    os.chmod(DB_PATH, 0o600)
