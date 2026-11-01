"""Persistence and state transitions for Feishu-driven local Codex jobs."""

from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
import uuid

from sqlite_db import connect as sqlite_connect


DB_PATH = Path("/var/lib/echomate/assistant.db")
MAX_RECOVERABLE_SESSIONS = 2

SESSION_STATES = {"pending", "active", "paused", "archived"}
JOB_STATES = {
    "queued",
    "dispatched",
    "running",
    "succeeded",
    "failed",
    "interrupted",
    "expired",
    "uncertain",
}
TERMINAL_JOB_STATES = {
    "succeeded",
    "failed",
    "interrupted",
    "expired",
    "uncertain",
}


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _database(conn=None, write=False, db_path=None):
    if conn is not None:
        yield conn
        return
    with sqlite_connect(Path(db_path or DB_PATH), write=write) as owned:
        yield owned


def init_schema(db_path=None):
    path = Path(db_path or DB_PATH)
    with sqlite_connect(path, write=True) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pc_devices (
                device_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                app_version TEXT NOT NULL,
                protocol_version INTEGER NOT NULL,
                last_seen_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pc_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                title TEXT NOT NULL,
                codex_thread_id TEXT,
                lifecycle TEXT NOT NULL
                    CHECK(lifecycle IN ('pending', 'active', 'paused', 'archived')),
                created_at_utc TEXT NOT NULL,
                last_active_at_utc TEXT NOT NULL,
                archived_at_utc TEXT,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pc_sessions_lookup
                ON pc_sessions(owner_id, chat_id, lifecycle, last_active_at_utc);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_pc_sessions_one_active
                ON pc_sessions(owner_id, chat_id)
                WHERE lifecycle = 'active';

            CREATE TABLE IF NOT EXISTS pc_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                session_id INTEGER,
                device_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('single', 'fixed')),
                status TEXT NOT NULL CHECK(
                    status IN (
                        'queued', 'dispatched', 'running', 'succeeded',
                        'failed', 'interrupted', 'expired', 'uncertain'
                    )
                ),
                prompt TEXT NOT NULL,
                remote_job_id TEXT NOT NULL UNIQUE,
                codex_thread_id TEXT,
                result_summary TEXT,
                error_code TEXT,
                cancel_requested_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                dispatched_at_utc TEXT,
                finished_at_utc TEXT,
                FOREIGN KEY(session_id) REFERENCES pc_sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_pc_jobs_poll
                ON pc_jobs(status, id);
            CREATE INDEX IF NOT EXISTS idx_pc_jobs_history
                ON pc_jobs(owner_id, chat_id, id DESC);

            CREATE TABLE IF NOT EXISTS pc_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                job_id INTEGER NOT NULL UNIQUE,
                user_text TEXT NOT NULL,
                assistant_text TEXT,
                status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                finished_at_utc TEXT,
                FOREIGN KEY(session_id) REFERENCES pc_sessions(id),
                FOREIGN KEY(job_id) REFERENCES pc_jobs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_pc_turns_session
                ON pc_turns(session_id, id);
            """
        )
        job_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pc_jobs)")
        }
        if "cancel_requested_at_utc" not in job_columns:
            conn.execute(
                "ALTER TABLE pc_jobs "
                "ADD COLUMN cancel_requested_at_utc TEXT"
            )


def touch_device(device_id, display_name="Windows PC", app_version="",
                 protocol_version=1, conn=None):
    with _database(conn, write=True) as target:
        target.execute(
            """
            INSERT INTO pc_devices
                (device_id, display_name, app_version, protocol_version,
                 last_seen_at_utc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                display_name = excluded.display_name,
                app_version = excluded.app_version,
                protocol_version = excluded.protocol_version,
                last_seen_at_utc = excluded.last_seen_at_utc
            """,
            (
                device_id,
                display_name,
                app_version,
                int(protocol_version),
                now_utc(),
            ),
        )


def get_device(device_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            "SELECT * FROM pc_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()


def create_session(owner_id, chat_id, device_id, profile_id, title,
                   conn=None):
    with _database(conn, write=True) as target:
        stamp = now_utc()
        cursor = target.execute(
            """
            INSERT INTO pc_sessions
                (owner_id, chat_id, device_id, profile_id, title,
                 lifecycle, created_at_utc, last_active_at_utc)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                owner_id,
                chat_id,
                device_id,
                profile_id,
                title or "本机任务",
                stamp,
                stamp,
            ),
        )
        return int(cursor.lastrowid)


def get_session(session_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            "SELECT * FROM pc_sessions WHERE id = ?",
            (int(session_id),),
        ).fetchone()


def get_active_session(owner_id, chat_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            """
            SELECT * FROM pc_sessions
            WHERE owner_id = ? AND chat_id = ? AND lifecycle = 'active'
            ORDER BY last_active_at_utc DESC, id DESC
            LIMIT 1
            """,
            (owner_id, chat_id),
        ).fetchone()


def list_recoverable_sessions(owner_id, chat_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            """
            SELECT * FROM pc_sessions
            WHERE owner_id = ? AND chat_id = ?
              AND lifecycle IN ('active', 'paused')
            ORDER BY last_active_at_utc DESC, id DESC
            LIMIT ?
            """,
            (owner_id, chat_id, MAX_RECOVERABLE_SESSIONS),
        ).fetchall()


def pause_active_session(owner_id, chat_id, conn=None):
    with _database(conn, write=True) as target:
        cursor = target.execute(
            """
            UPDATE pc_sessions
            SET lifecycle = 'paused'
            WHERE owner_id = ? AND chat_id = ? AND lifecycle = 'active'
            """,
            (owner_id, chat_id),
        )
        return cursor.rowcount


def pause_all_active_sessions(conn=None):
    with _database(conn, write=True) as target:
        cursor = target.execute(
            "UPDATE pc_sessions SET lifecycle = 'paused' "
            "WHERE lifecycle = 'active'"
        )
        return cursor.rowcount


def activate_session(owner_id, chat_id, session_id, conn=None):
    with _database(conn, write=True) as target:
        row = target.execute(
            """
            SELECT id FROM pc_sessions
            WHERE id = ? AND owner_id = ? AND chat_id = ?
              AND lifecycle IN ('pending', 'active', 'paused')
            """,
            (int(session_id), owner_id, chat_id),
        ).fetchone()
        if row is None:
            return False

        target.execute(
            """
            UPDATE pc_sessions
            SET lifecycle = 'paused'
            WHERE owner_id = ? AND chat_id = ?
              AND lifecycle = 'active' AND id <> ?
            """,
            (owner_id, chat_id, int(session_id)),
        )
        stamp = now_utc()
        target.execute(
            """
            UPDATE pc_sessions
            SET lifecycle = 'active', last_active_at_utc = ?,
                archived_at_utc = NULL, last_error = NULL
            WHERE id = ?
            """,
            (stamp, int(session_id)),
        )
        _archive_excess_sessions(target, owner_id, chat_id, int(session_id))
        return True


def set_session_thread(session_id, thread_id, conn=None):
    with _database(conn, write=True) as target:
        session = target.execute(
            "SELECT owner_id, chat_id FROM pc_sessions WHERE id = ?",
            (int(session_id),),
        ).fetchone()
        if session is None:
            return False
        target.execute(
            """
            UPDATE pc_sessions
            SET codex_thread_id = ?, last_error = NULL
            WHERE id = ?
            """,
            (thread_id, int(session_id)),
        )
        return activate_session(
            session["owner_id"],
            session["chat_id"],
            session_id,
            conn=target,
        )


def set_session_error(session_id, error_code, conn=None):
    with _database(conn, write=True) as target:
        target.execute(
            "UPDATE pc_sessions SET last_error = ? WHERE id = ?",
            ((error_code or "")[:200], int(session_id)),
        )


def _archive_excess_sessions(conn, owner_id, chat_id, keep_session_id):
    rows = conn.execute(
        """
        SELECT id FROM pc_sessions
        WHERE owner_id = ? AND chat_id = ?
          AND lifecycle IN ('active', 'paused')
        ORDER BY last_active_at_utc DESC, id DESC
        """,
        (owner_id, chat_id),
    ).fetchall()
    keep = {int(keep_session_id)}
    for row in rows:
        if len(keep) >= MAX_RECOVERABLE_SESSIONS:
            break
        keep.add(int(row["id"]))

    stamp = now_utc()
    for row in rows:
        session_id = int(row["id"])
        if session_id in keep:
            continue
        conn.execute(
            """
            UPDATE pc_sessions
            SET lifecycle = 'archived', archived_at_utc = ?
            WHERE id = ?
            """,
            (stamp, session_id),
        )


def archive_excess_sessions(owner_id, chat_id, keep_session_id, conn=None):
    with _database(conn, write=True) as target:
        _archive_excess_sessions(
            target, owner_id, chat_id, int(keep_session_id)
        )


def create_job(owner_id, chat_id, session_id, device_id, profile_id,
               kind, prompt, event_id, conn=None):
    if kind not in {"single", "fixed"}:
        raise ValueError("invalid job kind")

    with _database(conn, write=True) as target:
        existing = target.execute(
            "SELECT * FROM pc_jobs WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            return existing

        remote_job_id = f"job-{uuid.uuid4().hex}"
        stamp = now_utc()
        cursor = target.execute(
            """
            INSERT INTO pc_jobs
                (event_id, owner_id, chat_id, session_id, device_id,
                 profile_id, kind, status, prompt, remote_job_id,
                 created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (
                event_id,
                owner_id,
                chat_id,
                int(session_id) if session_id is not None else None,
                device_id,
                profile_id,
                kind,
                prompt,
                remote_job_id,
                stamp,
            ),
        )
        job_id = int(cursor.lastrowid)
        target.execute(
            """
            INSERT INTO pc_turns
                (session_id, job_id, user_text, status, created_at_utc)
            VALUES (?, ?, ?, 'queued', ?)
            """,
            (
                int(session_id) if session_id is not None else None,
                job_id,
                prompt,
                stamp,
            ),
        )
        return target.execute(
            "SELECT * FROM pc_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()


def get_job(job_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            "SELECT * FROM pc_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()


def get_job_by_event(event_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            "SELECT * FROM pc_jobs WHERE event_id = ?",
            (event_id,),
        ).fetchone()


def get_active_job(owner_id, chat_id, session_id=None, conn=None):
    with _database(conn) as target:
        session_clause = ""
        values = [owner_id, chat_id]
        if session_id is not None:
            session_clause = " AND session_id = ?"
            values.append(int(session_id))
        return target.execute(
            f"""
            SELECT * FROM pc_jobs
            WHERE owner_id = ? AND chat_id = ?
              AND status IN ('queued', 'dispatched', 'running')
              {session_clause}
            ORDER BY id DESC
            LIMIT 1
            """,
            values,
        ).fetchone()


def get_active_job_for_device(device_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            """
            SELECT * FROM pc_jobs
            WHERE device_id = ?
              AND status IN ('queued', 'dispatched', 'running')
            ORDER BY id DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()


def list_pollable_jobs(limit=20, conn=None):
    with _database(conn) as target:
        return target.execute(
            """
            SELECT * FROM pc_jobs
            WHERE status IN ('queued', 'dispatched', 'running')
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()


def request_job_cancel(job_id, conn=None):
    with _database(conn, write=True) as target:
        current = target.execute(
            "SELECT status FROM pc_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if current is None or current["status"] in TERMINAL_JOB_STATES:
            return False
        target.execute(
            """
            UPDATE pc_jobs
            SET cancel_requested_at_utc = COALESCE(
                cancel_requested_at_utc, ?
            )
            WHERE id = ?
            """,
            (now_utc(), int(job_id)),
        )
        return True


def list_cancel_requests(limit=10, conn=None):
    with _database(conn) as target:
        return target.execute(
            """
            SELECT * FROM pc_jobs
            WHERE cancel_requested_at_utc IS NOT NULL
              AND status IN ('queued', 'dispatched', 'running')
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()


def update_job_status(job_id, status, result_summary=None, error_code=None,
                      codex_thread_id=None, conn=None):
    if status not in JOB_STATES:
        raise ValueError("invalid job status")

    with _database(conn, write=True) as target:
        current = target.execute(
            "SELECT status FROM pc_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if current is None:
            raise ValueError("job does not exist")
        if current["status"] in TERMINAL_JOB_STATES:
            return get_job(job_id, conn=target)

        stamp = now_utc()
        fields = ["status = ?"]
        values = [status]

        if status == "dispatched":
            fields.append("dispatched_at_utc = COALESCE(dispatched_at_utc, ?)")
            values.append(stamp)
        if result_summary is not None:
            fields.append("result_summary = ?")
            values.append(result_summary[:12000])
        if error_code is not None:
            fields.append("error_code = ?")
            values.append(error_code[:200])
        if codex_thread_id is not None:
            fields.append("codex_thread_id = ?")
            values.append(codex_thread_id)
        if status in TERMINAL_JOB_STATES:
            fields.append("finished_at_utc = ?")
            values.append(stamp)

        values.append(int(job_id))
        target.execute(
            f"UPDATE pc_jobs SET {', '.join(fields)} WHERE id = ?",
            values,
        )

        if status in TERMINAL_JOB_STATES:
            target.execute(
                """
                UPDATE pc_turns
                SET assistant_text = ?, status = ?, finished_at_utc = ?
                WHERE job_id = ?
                """,
                (
                    (result_summary or "")[:12000],
                    status,
                    stamp,
                    int(job_id),
                ),
            )
        return get_job(job_id, conn=target)


def mark_interrupted_jobs(owner_id, chat_id, conn=None):
    with _database(conn, write=True) as target:
        rows = target.execute(
            """
            SELECT id FROM pc_jobs
            WHERE owner_id = ? AND chat_id = ?
              AND status IN ('queued', 'dispatched', 'running')
            """,
            (owner_id, chat_id),
        ).fetchall()
        for row in rows:
            update_job_status(
                row["id"],
                "interrupted",
                result_summary="任务已中断。",
                error_code="user_cancelled",
                conn=target,
            )
        return len(rows)


def list_history(owner_id, chat_id, limit=10, conn=None):
    with _database(conn) as target:
        return target.execute(
            """
            SELECT * FROM pc_jobs
            WHERE owner_id = ? AND chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (owner_id, chat_id, int(limit)),
        ).fetchall()


def get_history_job(job_id, owner_id, chat_id, conn=None):
    with _database(conn) as target:
        return target.execute(
            """
            SELECT * FROM pc_jobs
            WHERE id = ? AND owner_id = ? AND chat_id = ?
            """,
            (int(job_id), owner_id, chat_id),
        ).fetchone()


def count_session_turns(session_id, conn=None):
    with _database(conn) as target:
        return int(target.execute(
            "SELECT COUNT(*) FROM pc_turns WHERE session_id = ?",
            (int(session_id),),
        ).fetchone()[0])


def count_sessions(owner_id, chat_id, conn=None):
    with _database(conn) as target:
        return int(target.execute(
            """
            SELECT COUNT(*) FROM pc_sessions
            WHERE owner_id = ? AND chat_id = ?
            """,
            (owner_id, chat_id),
        ).fetchone()[0])
