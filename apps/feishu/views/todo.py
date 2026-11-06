from datetime import datetime, timedelta, timezone

from ..constants import PAGED_PAGE_SIZES
from ..runtime import LOCAL_TZ, OWNER_OPEN_ID
from ..time_utils import now_utc
from .formatting import (
    clamp_page as _clamp_page,
    pager_lines as _pager_lines,
    pager_state as _pager_state,
)

def _todo_window():
    """Return [today 00:00, day-after-tomorrow 00:00) in UTC."""
    today = datetime.now(LOCAL_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = today.astimezone(timezone.utc).isoformat(timespec="seconds")
    end = (today + timedelta(days=2)).astimezone(
        timezone.utc
    ).isoformat(timespec="seconds")
    return start, end


def _todo_rows(conn, limit=200):
    start, end = _todo_window()
    return conn.execute(
        """
        SELECT id, title, due_at_utc, reminded_at_utc
        FROM todos
        WHERE owner_id = ? AND status = 'pending'
          AND due_at_utc >= ? AND due_at_utc < ?
        ORDER BY due_at_utc ASC
        LIMIT ?
        """,
        (OWNER_OPEN_ID, start, end, limit),
    ).fetchall()


def _build_todo_page(conn, page_index=1):
    all_rows = _todo_rows(conn)
    total = len(all_rows)
    page_size = PAGED_PAGE_SIZES["todo"]
    page_index = _clamp_page(page_index, total, page_size)
    rows = all_rows[
        (page_index - 1) * page_size:page_index * page_size
    ]

    if not rows:
        reply = (
            "📋 今天和明天\n\n"
            "今天和明天都没有待办，休息一下吧 ☕\n\n"
            "3 新建待办\n"
            "4 历史待办\n"
            + "\n".join(_pager_lines(page_index, total, page_size))
        )
        return reply, {
            "day_key": datetime.now(LOCAL_TZ).date().isoformat(),
            "today_ids": [],
            "tomorrow_ids": [],
            "pager": _pager_state(page_index, total, page_size, []),
        }

    today = datetime.now(LOCAL_TZ).date()
    groups = {"今天": [], "明天": []}
    for row in rows:
        due = datetime.fromisoformat(row["due_at_utc"]).astimezone(LOCAL_TZ)
        day = "今天" if due.date() == today else "明天"
        groups[day].append(row)

    lines = [
        f"📋 今天 {len(groups['今天'])} 项 · 明天 {len(groups['明天'])} 项",
        "",
    ]
    for day, icon in (("今天", "🌤"), ("明天", "🌙")):
        items = groups[day]
        if not items:
            continue
        lines.append(f"{icon} {day}")
        for sequence, row in enumerate(items, 1):
            due = datetime.fromisoformat(
                row["due_at_utc"]
            ).astimezone(LOCAL_TZ)
            lines.append(
                f"{sequence}. {due:%H:%M}  {row['title']}"
            )
        lines.append("")

    complete_day = "今天" if groups["今天"] else "明天"
    cancel_day = "明天" if groups["明天"] else "今天"
    lines.extend([
        f"✅ 完成：完成 {complete_day}待办1",
        f"🧹 取消：取消 {cancel_day}待办1",
        "",
        "📜 4 历史待办",
        "➕ 3 新建待办",
        *_pager_lines(page_index, total, page_size),
    ])
    state = {
        "day_key": today.isoformat(),
        "today_ids": [row["id"] for row in groups["今天"]],
        "tomorrow_ids": [row["id"] for row in groups["明天"]],
        "pager": _pager_state(
            page_index,
            total,
            page_size,
            [row["id"] for row in rows],
        ),
    }
    return "\n".join(lines), state


def render_todo(conn):
    return _build_todo_page(conn)[0]


def _build_todo_history_page(conn, page_index=1):
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page_size = PAGED_PAGE_SIZES["todo_history"]
    total = conn.execute(
        """
        SELECT COUNT(*)
        FROM todos
        WHERE owner_id = ?
          AND (
              status IN ('completed', 'cancelled')
              OR (status = 'pending' AND due_at_utc < ?)
          )
        """,
        (OWNER_OPEN_ID, now_utc),
    ).fetchone()[0]

    if not total:
        return (
            "📜 历史待办\n\n还没有已完成或过期的待办\n\n0 返回",
            {"pager": _pager_state(1, 0, page_size, [])},
        )

    page_index = _clamp_page(page_index, total, page_size)
    rows = conn.execute(
        """
        SELECT id, title, due_at_utc, status
        FROM todos
        WHERE owner_id = ?
          AND (
              status IN ('completed', 'cancelled')
              OR (status = 'pending' AND due_at_utc < ?)
          )
        ORDER BY due_at_utc DESC
        LIMIT ? OFFSET ?
        """,
        (
            OWNER_OPEN_ID,
            now_utc,
            page_size,
            (page_index - 1) * page_size,
        ),
    ).fetchall()

    lines = ["📜 历史待办", ""]
    current_day = None
    for row in rows:
        due = datetime.fromisoformat(
            row["due_at_utc"]
        ).astimezone(LOCAL_TZ)
        day = due.strftime("%m-%d")
        if day != current_day:
            if len(lines) > 2 and lines[-1]:
                lines.append("")
            lines.append(day)
            current_day = day
        if row["status"] == "completed":
            mark = "✅ 已完成"
        elif row["status"] == "cancelled":
            mark = "🧹 已取消"
        else:
            mark = "⌛ 已过期"
        lines.append(
            f"{mark} · {due:%H:%M} · {row['title'][:50]}"
        )

    lines.extend([
        "",
        *_pager_lines(page_index, total, page_size),
    ])
    item_ids = [row["id"] for row in rows]
    return "\n".join(lines), {
        "pager": _pager_state(
            page_index, total, page_size, item_ids
        ),
        "history_ids": item_ids,
    }


def render_todo_history(conn, page_index=1):
    return _build_todo_history_page(
        conn, page_index=page_index
    )[0]


def render_todo_new():
    return (
        "➕ 新建待办\n\n"
        "这个页面只处理结构化待办。发：\n"
        "2026-10-27 09:00 交作业\n"
        "明天 9:00 交作业\n"
        "14:44 该睡觉了\n\n"
        "时间按北京时间解析。\n"
        "0 退出，退出后恢复普通聊天。"
    )
