import os

from echomate_core.db import open_connection
from feishu_bot import deploy_schema

from .runtime import DB_PATH

def init_db():
    deploy_schema.init_deploy_schema(DB_PATH)


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
