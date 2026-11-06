import re

from .constants import PAGED_PAGE_SIZES
from .page_state import (
    _pause_pc_session_for_navigation,
    default_nav_stack,
    get_page_state,
    set_page,
    state_nav_stack,
)
from .pages import _render_page_state
from .views.formatting import page_count as _page_count

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
