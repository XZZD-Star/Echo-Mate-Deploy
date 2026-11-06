import json
import sqlite3
from datetime import datetime, timezone

from echomate_core import notify as notify_store
from feishu_bot import pc_control_store

from .constants import PAGE_PARENT, PAGED_PAGE_SIZES, PAGES, PAGE_TTL_SECONDS
from .runtime import OWNER_OPEN_ID
from .time_utils import now_utc

def default_nav_stack(page):
    if page == "menu":
        return ["menu"]
    stack = [page]
    seen = {page}
    while stack[0] != "menu":
        parent = PAGE_PARENT.get(stack[0], "menu")
        if parent in seen:
            return ["menu", page]
        stack.insert(0, parent)
        seen.add(parent)
    return stack


def state_nav_stack(state):
    page = state.get("page", "menu")
    stack = state.get("nav_stack")
    if (
        isinstance(stack, list)
        and stack
        and stack[0] == "menu"
        and all(item in PAGES for item in stack)
        and stack[-1] == page
        and all(
            PAGE_PARENT.get(stack[index], "menu") == stack[index - 1]
            for index in range(1, len(stack))
        )
    ):
        return stack
    return default_nav_stack(page)


def page_key(chat_id):
    return f"menu:{chat_id}"


def _read_setting(conn, key, default=""):
    """优先用调用方连接读，读不到再回退到 notify_store。"""
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                return row[0]
        except sqlite3.Error:
            pass
    return notify_store.get_setting(key, default)


def _write_setting(conn, key, value):
    """用调用方连接写，和业务数据同一次事务提交。"""
    if conn is not None:
        try:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (key, str(value), now_utc()),
            )
            return True
        except sqlite3.Error:
            pass
    notify_store.set_setting(key, value)
    return True


def get_page_state(chat_id, conn=None):
    """读取当前页面和页面内映射；超过 24 小时未活动则回到主菜单。"""
    raw = _read_setting(conn, page_key(chat_id), "")
    if not raw:
        return {"page": "menu"}

    try:
        data = json.loads(raw)
        page = data.get("page", "menu")
        stamp = datetime.fromisoformat(data["at"])
    except (ValueError, KeyError, TypeError):
        return {"page": "menu"}

    if page not in PAGES:
        return {"page": "menu"}

    if (
        datetime.now(timezone.utc) - stamp
    ).total_seconds() > PAGE_TTL_SECONDS:
        if page == "pc_fixed":
            pc_control_store.pause_active_session(
                OWNER_OPEN_ID, chat_id, conn=conn
            )
        return {"page": "menu"}

    data["page"] = page
    if page in PAGED_PAGE_SIZES:
        pager = data.get("pager")
        if not isinstance(pager, dict):
            pager = {}
        pager["page_size"] = PAGED_PAGE_SIZES[page]
        try:
            pager["page_index"] = max(
                1, int(pager.get("page_index", 1))
            )
        except (TypeError, ValueError):
            pager["page_index"] = 1
        try:
            pager["total"] = max(0, int(pager.get("total", 0)))
        except (TypeError, ValueError):
            pager["total"] = 0
        ids = pager.get("item_ids")
        pager["item_ids"] = ids if isinstance(ids, list) else []
        data["pager"] = pager
    return data


def get_page(chat_id, conn=None):
    return get_page_state(chat_id, conn=conn)["page"]


def set_page(chat_id, page, state=None, conn=None):
    if page not in PAGES:
        page = "menu"
    data = {"page": page, "at": now_utc()}
    if state:
        data.update(state)
    _write_setting(
        conn,
        page_key(chat_id),
        json.dumps(data),
    )


def _pc_control_enabled(conn=None):
    return _read_setting(conn, "pc_control_enabled", "1") != "0"


def _pause_pc_session_for_navigation(conn, chat_id, old_page, new_page):
    if old_page == "pc_fixed" and new_page != "pc_fixed":
        pc_control_store.pause_active_session(
            OWNER_OPEN_ID, chat_id, conn=conn
        )
