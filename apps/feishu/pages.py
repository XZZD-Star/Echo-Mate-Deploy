from feishu_bot import pc_control_service, pc_control_store, pc_pages
from feishu_bot.pc_routing import PC_PAGES

from .constants import PAGED_PAGE_SIZES
from .page_state import _pc_control_enabled, get_page_state
from .runtime import OWNER_OPEN_ID, PC_DEVICE_ID
from .views.content import (
    _build_memory_page,
    _build_records_page,
    _build_todo_history_page,
    _build_todo_page,
    render_page,
)
from .views.formatting import _clamp_page, _pager_state

def _build_pc_page(conn, chat_id, page, page_index=1):
    if not _pc_control_enabled(conn):
        return "🖥 本机 Codex 暂时关闭\n\n0 返回", {}

    online, health = pc_control_service.health_snapshot()

    if page == "pc":
        return pc_pages.render_pc_home(online, health), {}
    if page == "pc_single":
        return pc_pages.render_pc_single(online, health), {}
    if page == "pc_fixed_new":
        return pc_pages.render_pc_fixed_new(online, health), {}
    if page == "pc_fixed":
        active = pc_control_store.get_active_session(
            OWNER_OPEN_ID, chat_id, conn=conn
        )
        recoverable = pc_control_store.list_recoverable_sessions(
            OWNER_OPEN_ID, chat_id, conn=conn
        )
        active_job = pc_control_store.get_active_job_for_device(
            PC_DEVICE_ID, conn=conn
        )
        turn_count = (
            pc_control_store.count_session_turns(
                active["id"], conn=conn
            )
            if active is not None
            else 0
        )
        return pc_pages.render_pc_fixed(
            active,
            recoverable,
            active_job,
            online,
            health,
            turn_count,
        ), {}
    if page == "pc_history":
        page_size = PAGED_PAGE_SIZES["pc_history"]
        total = conn.execute(
            """
            SELECT COUNT(*) FROM pc_jobs
            WHERE owner_id = ? AND chat_id = ?
            """,
            (OWNER_OPEN_ID, chat_id),
        ).fetchone()[0]
        page_index = _clamp_page(page_index, total, page_size)
        rows = pc_control_store.list_history(
            OWNER_OPEN_ID,
            chat_id,
            limit=(page_index * page_size) + 1,
            conn=conn,
        )
        offset = (page_index - 1) * page_size
        rows = rows[offset:offset + page_size]
        return pc_pages.render_pc_history(
            rows, page_index, total, page_size
        ), {
            "history_job_ids": [row["id"] for row in rows],
            "pager": _pager_state(
                page_index,
                total,
                page_size,
                [row["id"] for row in rows],
            ),
        }
    if page == "pc_history_detail":
        state = get_page_state(chat_id, conn=conn)
        job = pc_control_store.get_history_job(
            state.get("history_job_id", 0),
            OWNER_OPEN_ID,
            chat_id,
            conn=conn,
        )
        return pc_pages.render_pc_history_detail(job), {}
    return "🖥 本机 Codex\n\n0 返回", {}


def _render_page_state(conn, chat_id, page, page_index=1):
    state = None
    if page == "todo":
        reply, state = _build_todo_page(conn, page_index)
    elif page == "todo_history":
        reply, state = _build_todo_history_page(
            conn, page_index
        )
    elif page == "memory":
        reply, state = _build_memory_page(conn, page_index)
    elif page == "records":
        reply, state = _build_records_page(page_index)
    elif page in PC_PAGES:
        reply, state = _build_pc_page(
            conn, chat_id, page, page_index
        )
    else:
        reply = render_page(conn, chat_id, page, page_index)
    return reply, state or {}
