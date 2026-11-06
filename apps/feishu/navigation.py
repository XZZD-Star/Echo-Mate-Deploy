"""Page rendering state and navigation transitions."""

import re

from feishu_bot import pc_control_service, pc_control_store, pc_pages
from feishu_bot.pc_routing import PC_PAGES

from .config import OWNER_OPEN_ID, PAGED_PAGE_SIZES, PC_DEVICE_ID
from .storage import (
    _pause_pc_session_for_navigation,
    _pc_control_enabled,
    default_nav_stack,
    get_page_state,
    set_page,
    state_nav_stack,
)
from .views import (
    _build_memory_page,
    _build_records_page,
    _build_todo_history_page,
    _build_todo_page,
    clamp_page as _clamp_page,
    page_count as _page_count,
    pager_state as _pager_state,
    render_page,
)


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


def _enter(conn, chat_id, page, mode="push"):
    current_state = get_page_state(chat_id, conn=conn)
    current_page = current_state.get("page", "menu")
    stack = state_nav_stack(current_state)
    _pause_pc_session_for_navigation(conn, chat_id, current_page, page)

    if mode == "reset":
        stack = default_nav_stack(page)
    elif mode == "replace":
        if len(stack) >= 2 and stack[-2] == page:
            stack = stack[:-1]
        else:
            stack = stack[:-1] + [page] if stack else [page]
    elif current_page != page:
        stack = stack + [page]

    stack = state_nav_stack({"page": page, "nav_stack": stack})
    reply, extra_state = _render_page_state(
        conn, chat_id, page, page_index=1
    )
    extra_state["nav_stack"] = stack
    set_page(chat_id, page, state=extra_state, conn=conn)
    return reply


def _navigate_back(conn, chat_id):
    state = get_page_state(chat_id, conn=conn)
    current_page = state.get("page", "menu")
    stack = state_nav_stack(state)
    if len(stack) <= 1:
        return _enter(conn, chat_id, "menu", mode="reset")

    target = stack[-2]
    _pause_pc_session_for_navigation(conn, chat_id, current_page, target)
    next_stack = stack[:-1]
    reply, extra_state = _render_page_state(
        conn, chat_id, target, page_index=1
    )
    extra_state["nav_stack"] = next_stack
    set_page(chat_id, target, state=extra_state, conn=conn)
    return reply


def _open_page_index(conn, chat_id, page_index):
    state = get_page_state(chat_id, conn=conn)
    page = state.get("page", "menu")
    stack = state_nav_stack(state)
    reply, extra_state = _render_page_state(
        conn, chat_id, page, page_index=page_index
    )
    extra_state["nav_stack"] = stack
    set_page(chat_id, page, state=extra_state, conn=conn)
    return reply


def _handle_pagination(conn, chat_id, text):
    state = get_page_state(chat_id, conn=conn)
    page = state.get("page", "menu")
    if page not in PAGED_PAGE_SIZES:
        return None
    pager = state.get("pager") or {}
    current = max(1, int(pager.get("page_index", 1)))
    total = max(0, int(pager.get("total", 0)))
    page_size = PAGED_PAGE_SIZES[page]
    pages = _page_count(total, page_size)
    target = current

    if text in {"8", "上一页"}:
        target = current - 1
    elif text in {"9", "下一页"}:
        target = current + 1
    else:
        match = re.match(r"^第\s*(\d+)\s*页$", text)
        if not match:
            return None
        target = int(match.group(1))

    if target < 1:
        return "已经是第一页\n\n0 返回"
    if target > pages:
        return "已经是最后一页\n\n0 返回"
    return _open_page_index(conn, chat_id, target)
