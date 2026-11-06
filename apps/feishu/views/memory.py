from datetime import datetime

from echomate_core import store as echomate_store

from ..constants import PAGED_PAGE_SIZES
from ..runtime import LOCAL_TZ, OWNER_OPEN_ID
from ..time_utils import now_utc
from .formatting import (
    clamp_page as _clamp_page,
    pager_lines as _pager_lines,
    pager_state as _pager_state,
)

def _memory_rows(conn, limit, offset=0):
    return conn.execute(
        """
        SELECT id, content
        FROM memories
        WHERE owner_id = ?
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (OWNER_OPEN_ID, limit, offset),
    ).fetchall()


def _memory_candidate_display_index(conn, candidate_id):
    candidates = echomate_store.list_memory_candidates(
        OWNER_OPEN_ID, 10, conn=conn
    )
    for index, item in enumerate(candidates, 1):
        if item["id"] == candidate_id:
            return index
    return 1


def _build_memory_page(conn, page_index=1):
    total = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
        (OWNER_OPEN_ID,),
    ).fetchone()[0]
    page_size = PAGED_PAGE_SIZES["memory"]

    if not total:
        return (
            "📚 长期偏好\n\n还没有记忆\n\n"
            "💾 存记忆：记住 喜欢简短回答\n\n"
            "0 返回",
            {"pager": _pager_state(1, 0, page_size, [])},
        )

    page_index = _clamp_page(page_index, total, page_size)
    rows = _memory_rows(
        conn,
        page_size,
        (page_index - 1) * page_size,
    )
    memory_ids = [row[0] for row in rows]

    lines = [f"📚 长期偏好 · {total} 条", ""]
    for sequence, (_, content) in enumerate(rows, 1):
        lines.append(f"{sequence}. {content[:60]}")

    lines.extend([
        "",
        "🔎 查记忆：查记忆 服务器",
        "💾 存记忆：记住 喜欢简短回答",
        "🗑 删除记忆：删除第 1 条",
        "💬 聊天记忆：聊天记忆",
        "",
        *_pager_lines(page_index, total, page_size),
    ])
    return "\n".join(lines), {
        "pager": _pager_state(
            page_index, total, page_size, memory_ids
        ),
        "memory_ids": memory_ids,
    }


def render_memory(conn, page_index=1):
    return _build_memory_page(conn, page_index=page_index)[0]


def render_memory_hub(conn):
    long_term_total = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
        (OWNER_OPEN_ID,),
    ).fetchone()[0]
    chat_total = conn.execute(
        """
        SELECT COUNT(*) FROM chat_memories
        WHERE owner_id = ?
          AND (expires_at_utc IS NULL OR expires_at_utc > ?)
        """,
        (OWNER_OPEN_ID, now_utc()),
    ).fetchone()[0]
    return (
        "🧠 记忆\n\n"
        "我会把记忆分成两类\n\n"
        f"1 · 📚 长期偏好 · {long_term_total} 条\n"
        f"2 · 💬 聊天记忆 · {chat_total} 条\n\n"
        "0 返回"
    )


def _build_chat_memory_page(conn, page_index=1):
    total = conn.execute(
        """
        SELECT COUNT(*) FROM chat_memories
        WHERE owner_id = ?
          AND (expires_at_utc IS NULL OR expires_at_utc > ?)
        """,
        (OWNER_OPEN_ID, now_utc()),
    ).fetchone()[0]
    page_size = PAGED_PAGE_SIZES["chat_memory"]
    if not total:
        return (
            "💬 聊天记忆\n\n"
            "还没有自动记住的聊天内容\n\n"
            "0 返回",
            {"pager": _pager_state(1, 0, page_size, [])},
        )

    page_index = _clamp_page(page_index, total, page_size)
    rows = echomate_store.list_chat_memories(
        OWNER_OPEN_ID,
        limit=page_size,
        offset=(page_index - 1) * page_size,
        conn=conn,
    )
    item_ids = [row["id"] for row in rows]
    lines = [f"💬 聊天记忆 · {total} 条", ""]
    for sequence, row in enumerate(rows, 1):
        try:
            stamp = datetime.fromisoformat(
                row["created_at_utc"]
            ).astimezone(LOCAL_TZ).strftime("%m-%d")
        except (TypeError, ValueError):
            stamp = ""
        lines.append(
            f"{sequence}. {row['content'][:64]}"
            + (f" · {stamp}" if stamp else "")
        )
    lines.extend([
        "",
        "🗑 删除：删除聊天记忆 1",
        "",
        *_pager_lines(page_index, total, page_size),
    ])
    return "\n".join(lines), {
        "pager": _pager_state(page_index, total, page_size, item_ids),
        "chat_memory_ids": item_ids,
    }
