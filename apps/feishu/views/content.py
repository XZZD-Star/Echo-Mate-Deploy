import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

import psutil

from echomate_core import notify as notify_store
from echomate_core import store as echomate_store
from echomate_core.db import open_connection

from ..constants import (
    BACKUP_UNIT,
    PAGE_CARD_CONFIG,
    PAGE_CARD_DROP_FIRST,
    PAGED_PAGE_SIZES,
    SERVICE_UNITS,
)
from ..runtime import DB_PATH, LOCAL_TZ, OWNER_OPEN_ID
from ..time_utils import now_utc
from .formatting import (
    clamp_page as _clamp_page,
    format_bytes as _format_bytes,
    kind_label as _kind_label,
    normalize_hhmm as _normalize_hhmm,
    pager_lines as _pager_lines,
    pager_state as _pager_state,
    unit_dot as _unit_dot,
    unit_state_text as _unit_state_text,
)

def _unit_state(unit):
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return (proc.stdout or "").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _service_states():
    """返回 [(中文标签, systemd 状态, unit 简名), ...]。"""
    return [
        (label, _unit_state(unit), short)
        for unit, label, short in SERVICE_UNITS
    ]


def _uptime_text():
    uptime = timedelta(
        seconds=max(0, int(time.time() - psutil.boot_time()))
    )
    if uptime.days:
        return f"{uptime.days} 天"
    return f"{uptime.seconds // 3600} 小时"


def _boot_text():
    boot = datetime.fromtimestamp(
        psutil.boot_time(), tz=timezone.utc
    ).astimezone(LOCAL_TZ)
    if boot.year == datetime.now(LOCAL_TZ).year:
        return boot.strftime("%m-%d %H:%M")
    return boot.strftime("%Y-%m-%d")


def menu_text():
    return (
        "🐣 Echo-Mate\n"
        "你的桌面助手\n\n"
        "1 · 📊 运行状态\n"
        "2 · 📋 待办清单\n"
        "3 · ➕ 新建待办\n"
        "4 · 🧠 记忆\n"
        "5 · 💡 使用帮助\n"
        "6 · 🔊 语音播报\n"
        "7 · 🖥 本机 Codex\n\n"
        "回复数字进入，0 返回上一级"
    )


def render_status():
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    states = _service_states()
    down = [label for label, state, _ in states if state != "active"]
    backup_state = _unit_state(BACKUP_UNIT[0])
    active_count = sum(state == "active" for _, state, _ in states)

    if down:
        lines = [
            "🛠️ Echo-Mate 需要看一眼",
            "",
            f"有 {len(down)} 个核心服务异常",
        ]
        lines.extend(
            f"🔴 {label}"
            for label, state, _ in states
            if state != "active"
        )
        normal_count = len(states) - len(down)
        if normal_count:
            lines.append(f"其余 {normal_count} 个核心服务正常")
    elif cpu > 90 or mem.available < 250 * 1024**2:
        lines = [
            "😮‍💨 当前稍微有点忙",
            "",
            f"CPU 正在加班，暂时 {cpu:.0f}%",
            f"{len(states)} 个核心服务都在线",
        ]
    elif backup_state != "active":
        lines = [
            "🗃️ 备份任务需要检查",
            "",
            f"{len(states)} 个核心服务都在线",
            "数据库备份没有正常运行",
        ]
    else:
        lines = [
            "✅ 一切正常，今天也稳稳在线",
            "",
            f"{active_count} 个核心服务都在线",
        ]

    lines.extend([
        "",
        "🗃️ 备份任务",
        f"{_unit_dot(backup_state)} {_unit_state_text(backup_state)}",
        "",
        "📊 资源余量",
        f"CPU：{cpu:.0f}%",
        f"可用内存：{_format_bytes(mem.available)}",
        f"剩余磁盘：{_format_bytes(disk.free)}",
        f"已稳定运行：{_uptime_text()}",
    ])

    lines.append("")
    lines.append("1 刷新 · 2 详情 · 0 返回")
    return "\n".join(lines)


def render_status_detail():
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    boot = datetime.fromtimestamp(
        psutil.boot_time(), tz=timezone.utc
    ).astimezone(LOCAL_TZ)
    states = _service_states()
    backup_state = _unit_state(BACKUP_UNIT[0])

    lines = [
        "🧰 Echo-Mate 系统详情",
        "",
        "🖥 主机",
        os.uname().nodename,
        "",
        "⏱ 运行情况",
        f"已稳定运行：{_uptime_text()}",
        f"最近开机：{boot:%m-%d %H:%M}",
        "",
        "🔧 核心服务",
    ]

    for label, state, _ in states:
        suffix = "" if state == "active" else " · 异常"
        lines.append(f"{_unit_dot(state)} {label}{suffix}")

    lines.extend([
        "",
        "🗃️ 定时任务",
        f"{_unit_dot(backup_state)} {BACKUP_UNIT[1]}",
        "",
        "📊 资源",
        f"CPU：{cpu:.0f}% · {psutil.cpu_count()} 核",
        f"内存：{_format_bytes(mem.used)} / {_format_bytes(mem.total)}",
    ])
    if swap.used:
        lines.append(
            f"交换空间：{_format_bytes(swap.used)} / "
            f"{_format_bytes(swap.total)}"
        )
    else:
        lines.append("交换空间：未使用")
    lines.append(
        f"磁盘：{_format_bytes(disk.used)} / {_format_bytes(disk.total)}"
    )

    lines.extend(["", "1 刷新 · 0 返回"])
    return "\n".join(lines)


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


def render_help():
    return (
        "💡 帮助\n\n"
        "📊 运行状态\n"
        "查看服务：状态\n\n"
        "📋 待办\n"
        "➕ 新建待办：添加待办 2026-10-29 09:00 交作业\n"
        "📄 查看待办：待办\n"
        "✅ 完成待办：完成 今天待办1\n"
        "🧹 取消待办：取消 明天待办1\n\n"
        "🧠 记忆\n"
        "打开记忆中心：记忆\n"
        "💾 存记忆：记住 喜欢简短回答\n"
        "🔎 查记忆：查记忆 服务器\n"
        "🗑 删除记忆：删除第 1 条\n\n"
        "💬 聊天记忆：聊天记忆\n\n"
        "🔊 播报\n"
        "🌙 设置静音：静音 23:00 08:00\n"
        "☀️ 取消静音：关闭静音\n\n"
        "🖥 本机 Codex\n"
        "打开页面：本机 Codex\n\n"
        "菜单\n"
        "随时回复“菜单”返回主菜单。\n\n"
        "0 返回"
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


def build_page_card(page, text):
    title, template = PAGE_CARD_CONFIG.get(page, ("Echo-Mate", "blue"))
    lines = str(text or "").strip().splitlines()

    if lines and lines[0].strip() in PAGE_CARD_DROP_FIRST:
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]

    footer = "回复编号继续 · 0 返回"
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if line and ("0 返回" in line or "0 取消" in line):
            footer = line
            del lines[index]
            break
    while lines and not lines[-1].strip():
        lines.pop()

    body = "\n".join(lines).strip()
    if page == "menu":
        body = re.sub(
            r"^(\d+) · (.*)$",
            r"**\1** · \2",
            body,
            flags=re.MULTILINE,
        )
    elif page == "status":
        status_lines = body.splitlines()
        if status_lines:
            status_lines[0] = f"**{status_lines[0]}**"
        for index, line in enumerate(status_lines):
            if line.startswith("核心服务 "):
                status_lines[index] = f"**{line}**"
        body = "\n".join(status_lines)
    if page in {"status", "status_detail"}:
        if "🔴" in body or "⚠️" in body:
            template = "orange"
        elif "🟢" in body or "✅" in body:
            template = "green"

    elements = []
    if body:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": body},
        })
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": footer,
            }],
        },
    ])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def render_page(conn, chat_id, page, page_index=1):
    if page == "menu":
        return menu_text()
    if page == "status":
        return render_status()
    if page == "status_detail":
        return render_status_detail()
    if page == "todo":
        return _build_todo_page(conn, page_index)[0]
    if page == "todo_new":
        return render_todo_new()
    if page == "todo_history":
        return _build_todo_history_page(conn, page_index)[0]
    if page == "memory_hub":
        return render_memory_hub(conn)
    if page == "memory":
        return _build_memory_page(conn, page_index)[0]
    if page == "chat_memory":
        return _build_chat_memory_page(conn, page_index)[0]
    if page == "help":
        return render_help()
    if page == "notify":
        return render_notify()
    if page == "quiet":
        return render_quiet()
    if page == "records":
        return _build_records_page(page_index)[0]
    if page == "memory_clear_confirm":
        return (
            "⚠️ 清空全部记忆？\n\n"
            "删除后无法恢复。\n"
            "确认发：确认清空记忆\n"
            "取消发：0"
        )
    return menu_text()
