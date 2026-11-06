"""Feishu-specific tables that are not part of echomate_core."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from echomate_core.db import open_connection


def init_deploy_schema(db_path):
    """Create Feishu event-deduplication tables without owning core tables."""
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


__all__ = ["init_deploy_schema"]
