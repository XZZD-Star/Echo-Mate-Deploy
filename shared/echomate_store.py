"""Echo-Mate shared business data.

Single SQLite-backed source of truth for deskbot and Feishu bot.
Only standard library dependencies are used.
"""

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("/var/lib/echomate/assistant.db")
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
MAX_MEMORY_LENGTH = 2000


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
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


def init_schema():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_owner_id
                ON memories(owner_id, id);

            CREATE TABLE IF NOT EXISTS memory_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'fact',
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'ai_chat',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at_utc TEXT NOT NULL,
                decided_at_utc TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_memory_candidates_pending
                ON memory_candidates(status, owner_id, id);

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL,
                due_at_utc TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at_utc TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                reminded_at_utc TEXT,
                last_attempt_at_utc TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_todos_due
                ON todos(status, due_at_utc, reminded_at_utc);

            CREATE TABLE IF NOT EXISTS reminder_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                todo_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at_utc TEXT,
                sent_at_utc TEXT,
                acked_at_utc TEXT,
                last_error TEXT,
                UNIQUE(todo_id, channel)
            );
            CREATE INDEX IF NOT EXISTS idx_delivery_pending
                ON reminder_deliveries(channel, status, id);
            """
        )

        memory_columns = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        if "category" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN category TEXT NOT NULL DEFAULT 'fact'")
        if "source" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT 'feishu'")
        if "updated_at" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN updated_at TEXT")

        todo_columns = {r["name"] for r in conn.execute("PRAGMA table_info(todos)")}
        additions = {
            "source": "TEXT NOT NULL DEFAULT 'feishu'",
            "dedupe_key": "TEXT",
            "notify_deskbot": "INTEGER NOT NULL DEFAULT 1",
            "notify_feishu": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in additions.items():
            if name not in todo_columns:
                conn.execute(f"ALTER TABLE todos ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_dedupe "
            "ON todos(owner_id, dedupe_key) WHERE dedupe_key IS NOT NULL"
        )


def _normalize(text):
    return re.sub(r"[\s，。！？、,.!?：:；;]+", "", text or "").lower()


def detect_category(text):
    value = text or ""
    if any(word in value for word in ("我叫", "名字是", "居住", "住在", "家住", "生日", "职业")):
        return "profile"
    if any(word in value for word in ("喜欢", "讨厌", "偏好", "想听", "不要")):
        return "preference"
    if any(word in value for word in ("记住待办", "别忘了", "提醒")):
        return "task"
    return "fact"


def list_memories(owner_id, limit=50):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, content, category, source
            FROM memories WHERE owner_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (owner_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def memory_context(owner_id, limit=50):
    seen = set()
    result = []
    for item in list_memories(owner_id, limit * 3):
        key = _normalize(item["content"])
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def enqueue_memory_candidate(owner_id, content, source="ai_chat", category=None):
    content = (content or "").strip()
    if not content:
        return None, "empty"
    if len(content) > MAX_MEMORY_LENGTH:
        return None, "too_long"

    category = category or detect_category(content)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, content FROM memory_candidates "
            "WHERE owner_id = ? AND status = 'pending'",
            (owner_id,),
        ).fetchall()
        key = _normalize(content)
        for row in rows:
            if _normalize(row["content"]) == key:
                return row["id"], "duplicate"

        cursor = conn.execute(
            """
            INSERT INTO memory_candidates
                (owner_id, category, content, source, created_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner_id, category, content, source, now_utc()),
        )
        return cursor.lastrowid, "created"


def list_memory_candidates(owner_id, limit=10):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, category, content, source, created_at_utc
            FROM memory_candidates
            WHERE owner_id = ? AND status = 'pending'
            ORDER BY id ASC LIMIT ?
            """,
            (owner_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def decide_memory_candidate(owner_id, candidate_id, decision):
    if decision not in ("accept", "reject"):
        return False
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, category, content, source FROM memory_candidates
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (int(candidate_id), owner_id),
        ).fetchone()
        if not row:
            return False

        if decision == "accept":
            conn.execute(
                """
                INSERT INTO memories
                    (owner_id, content, created_at, category, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (owner_id, row["content"], now_utc(), row["category"],
                 row["source"], now_utc()),
            )
        conn.execute(
            "UPDATE memory_candidates SET status = ?, decided_at_utc = ? WHERE id = ?",
            ("accepted" if decision == "accept" else "rejected",
             now_utc(), int(candidate_id)),
        )
    return True


def delete_memory(owner_id, memory_id):
    with connect() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND owner_id = ?",
            (int(memory_id), owner_id),
        )
        return cursor.rowcount > 0


def clear_memories(owner_id):
    with connect() as conn:
        cursor = conn.execute("DELETE FROM memories WHERE owner_id = ?", (owner_id,))
        return cursor.rowcount


def _parse_due(due_text):
    value = (due_text or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def create_todo(owner_id, title, due_at_utc, source="feishu",
                chat_id="", dedupe_key=None, notify_deskbot=True,
                notify_feishu=True):
    title = (title or "").strip()
    if not title:
        return None, "empty"
    if len(title) > 200:
        return None, "too_long"

    due = _parse_due(due_at_utc)
    if due is None:
        return None, "bad_due"
    if due <= datetime.now(timezone.utc):
        return None, "past_due"

    due_text = due.isoformat(timespec="seconds")
    if not dedupe_key:
        raw = f"{owner_id}|{title}|{due_text}".encode("utf-8")
        dedupe_key = "todo:" + hashlib.sha256(raw).hexdigest()[:24]

    with connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM todos
            WHERE owner_id = ? AND dedupe_key = ?
              AND status = 'pending'
            """,
            (owner_id, dedupe_key),
        ).fetchone()
        if row:
            return row["id"], "duplicate"

        cursor = conn.execute(
            """
            INSERT INTO todos
                (owner_id, title, due_at_utc, timezone, status,
                 created_at_utc, chat_id, source, dedupe_key,
                 notify_deskbot, notify_feishu)
            VALUES (?, ?, ?, 'Asia/Shanghai', 'pending',
                    ?, ?, ?, ?, ?, ?)
            """,
            (owner_id, title, due_text, now_utc(), chat_id or "",
             source, dedupe_key, int(bool(notify_deskbot)),
             int(bool(notify_feishu))),
        )
        return cursor.lastrowid, "created"


def list_todos(owner_id, include_done=False, limit=50):
    where = "" if include_done else "AND status = 'pending'"
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, title, due_at_utc, status, source, chat_id,
                   notify_deskbot, notify_feishu
            FROM todos WHERE owner_id = ? {where}
            ORDER BY due_at_utc ASC LIMIT ?
            """,
            (owner_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def set_todo_status(owner_id, todo_id, status):
    if status not in ("pending", "completed", "cancelled"):
        return False
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE todos SET status = ? WHERE id = ? AND owner_id = ?",
            (status, int(todo_id), owner_id),
        )
        return cursor.rowcount > 0


def materialize_due_reminders():
    """Create one delivery per channel for due pending todos."""
    now = now_utc()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, notify_deskbot, notify_feishu
            FROM todos
            WHERE status = 'pending' AND due_at_utc <= ?
              AND reminded_at_utc IS NULL
            ORDER BY due_at_utc ASC LIMIT 50
            """,
            (now,),
        ).fetchall()
        for row in rows:
            if not row["notify_deskbot"] and not row["notify_feishu"]:
                conn.execute(
                    "UPDATE todos SET reminded_at_utc = ? WHERE id = ?",
                    (now, row["id"]),
                )
        created = 0
        for row in rows:
            channels = []
            if row["notify_deskbot"]:
                channels.append("deskbot")
            if row["notify_feishu"]:
                channels.append("feishu")
            for channel in channels:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO reminder_deliveries
                        (todo_id, channel, status)
                    VALUES (?, ?, 'pending')
                    """,
                    (row["id"], channel),
                )
                created += cursor.rowcount
        return created


def pending_deskbot_deliveries():
    """Return claimed/sent deskbot deliveries awaiting ACK reconciliation."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id AS delivery_id, todo_id, status
            FROM reminder_deliveries
            WHERE channel = 'deskbot'
              AND status IN ('claimed', 'sent')
              AND acked_at_utc IS NULL
            ORDER BY id ASC LIMIT 20
            """
        ).fetchall()
    return [dict(row) for row in rows]


def claim_delivery(channel):
    with connect() as conn:
        row = conn.execute(
            """
            SELECT d.id AS delivery_id, d.todo_id, d.attempts,
                   t.title, t.due_at_utc, t.chat_id, t.owner_id
            FROM reminder_deliveries d
            JOIN todos t ON t.id = d.todo_id
            WHERE d.channel = ? AND d.status = 'pending'
              AND t.status = 'pending'
            ORDER BY d.id ASC LIMIT 1
            """,
            (channel,),
        ).fetchone()
        if not row:
            return None
        cursor = conn.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'claimed', last_attempt_at_utc = ?, attempts = attempts + 1
            WHERE id = ? AND status = 'pending'
            """,
            (now_utc(), row["delivery_id"]),
        )
        if cursor.rowcount != 1:
            return None
        return dict(row)


def mark_delivery(delivery_id, status, error=None):
    if status not in ("sent", "failed", "skipped"):
        return
    with connect() as conn:
        conn.execute(
            """
            UPDATE reminder_deliveries
            SET status = ?, sent_at_utc = CASE WHEN ? = 'sent' THEN ? ELSE sent_at_utc END,
                last_error = ?
            WHERE id = ?
            """,
            (status, status, now_utc(), (str(error)[:300] if error else None),
             int(delivery_id)),
        )


def acknowledge_delivery(delivery_id):
    """Mark the client ACK and finalize the todo when all channels finish."""
    with connect() as conn:
        row = conn.execute(
            "SELECT todo_id FROM reminder_deliveries WHERE id = ?",
            (int(delivery_id),),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'sent', acked_at_utc = ?
            WHERE id = ? AND acked_at_utc IS NULL
            """,
            (now_utc(), int(delivery_id)),
        )
        todo_id = row["todo_id"]

    finalize_todo_if_delivered(todo_id)
    return True


def finalize_todo_if_delivered(todo_id):
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM reminder_deliveries
            WHERE todo_id = ? AND status IN ('pending', 'claimed', 'failed')
            """,
            (int(todo_id),),
        ).fetchone()
        if row and row["n"] == 0:
            conn.execute(
                "UPDATE todos SET reminded_at_utc = ? "
                "WHERE id = ? AND reminded_at_utc IS NULL",
                (now_utc(), int(todo_id)),
            )
