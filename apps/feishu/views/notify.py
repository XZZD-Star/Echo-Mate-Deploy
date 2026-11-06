from echomate_core import notify as notify_store
from echomate_core.db import open_connection

from ..constants import PAGED_PAGE_SIZES
from ..runtime import DB_PATH
from .formatting import (
    clamp_page as _clamp_page,
    kind_label as _kind_label,
    normalize_hhmm as _normalize_hhmm,
    pager_lines as _pager_lines,
    pager_state as _pager_state,
)

def _quiet_state():
    start = notify_store.get_setting("notify_quiet_start", "")
    end = notify_store.get_setting("notify_quiet_end", "")
    if _normalize_hhmm(start) is None or _normalize_hhmm(end) is None:
        return "未开启", False
    return f"{start}–{end}", True


def render_notify():
    todo_on = notify_store.get_setting("notify_todo_enabled", "1") == "1"
    monitor_on = (
        notify_store.get_setting("notify_monitor_enabled", "1") == "1"
    )
    quiet_text, _ = _quiet_state()
    pending = notify_store.pending_count()

    lines = [
        "🔊 播报设置",
        "",
        f"🌙 静音时段　{quiet_text}",
        f"📋 待办提醒　{'已开启' if todo_on else '已关闭'}",
        f"📡 监控提醒　{'已开启' if monitor_on else '已关闭'}",
        f"📤 待播消息　{pending} 条",
        "",
        "操作",
        "1 静音设置",
        f"2 {'关闭' if todo_on else '开启'}待办播报",
        f"3 {'关闭' if monitor_on else '开启'}监控播报",
        "4 播报记录",
        "",
        "0 返回",
    ]
    return "\n".join(lines)


def render_quiet():
    quiet_text, enabled = _quiet_state()

    if enabled:
        return (
            "🌙 静音设置\n\n"
            f"当前　{quiet_text}\n"
            "该时段桌宠不播报，飞书通知不受影响\n\n"
            "修改：静音 23:00 08:00\n"
            "取消：关闭静音\n\n"
            "0 返回"
        )

    return (
        "🌙 静音设置\n\n"
        "当前　未开启\n"
        "设置后，该时段桌宠不播报，飞书通知不受影响\n\n"
        "设置：静音 23:00 08:00\n"
        "取消：关闭静音\n\n"
        "0 返回"
    )


def _record_total():
    conn = open_connection(DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM notify_outbox"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _build_records_page(page_index=1):
    total = _record_total()
    page_size = PAGED_PAGE_SIZES["records"]

    if not total:
        return (
            "📢 播报记录\n\n还没有记录\n\n0 返回",
            {"pager": _pager_state(1, 0, page_size, [])},
        )

    page_index = _clamp_page(page_index, total, page_size)
    offset = (page_index - 1) * page_size
    rows = notify_store.recent_records(offset + page_size + 1)
    rows = rows[offset:offset + page_size]

    status_label = {
        "pending": "等待播报",
        "claimed": "正在播报",
        "sent": "已发送",
        "failed": "播报失败",
        "expired": "已过期",
    }

    lines = ["📢 播报记录", ""]
    for sequence, row in enumerate(rows, 1):
        status = status_label.get(row["status"], row["status"])
        if row["status"] == "sent":
            mark = "已播放" if row["acked_at_utc"] else "已发送，待确认"
        elif row["last_error"]:
            mark = f"{status} · {row['last_error'][:20]}"
        else:
            mark = status
        lines.append(
            f"{sequence}. {_kind_label(row['kind'])} · {mark}"
        )

    lines.extend([
        "",
        *_pager_lines(page_index, total, page_size),
    ])
    return "\n".join(lines), {
        "pager": _pager_state(
            page_index,
            total,
            page_size,
            [row["id"] for row in rows],
        ),
    }


def render_records(page_index=1):
    return _build_records_page(page_index=page_index)[0]
