import json
import os
import re
import sqlite3
import subprocess
import sys
import psutil
import requests
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import lark_oapi as lark
from dotenv import load_dotenv, dotenv_values
from urllib.parse import urlparse
from openai import OpenAI

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
if str(DEPLOY_ROOT) not in sys.path:
    sys.path.insert(0, str(DEPLOY_ROOT))

from echomate_core.credentials import credential
from echomate_core import actions as assistant_actions
from echomate_core import assistant as assistant_core
from echomate_core import memory as memory_store
from echomate_core import notify as notify_store
from echomate_core import store as echomate_store
from echomate_core import tools as assistant_tools
from echomate_core.observer import MemoryObserver
from contextvars import ContextVar
from echomate_core.schema import init_core_schema
from echomate_core.db import connect as sqlite_connect
from echomate_core.db import open_connection
import agent_runtime
try:
    from . import deploy_schema
except ImportError:
    import deploy_schema
import pc_control_service
import pc_control_store
import pc_pages
from pc_routing import PC_PAGES
from apps.feishu.views.formatting import (
    clamp_page as _clamp_page,
    format_bytes as _format_bytes,
    kind_label as _kind_label,
    normalize_hhmm as _normalize_hhmm,
    page_count as _page_count,
    pager_lines as _pager_lines,
    pager_state as _pager_state,
    unit_dot as _unit_dot,
    unit_state_text as _unit_state_text,
)
from apps.feishu import transport as feishu_transport

CURRENT_CHAT_ID = ContextVar("CURRENT_CHAT_ID", default="")


# ==================== 配置 ====================

os.umask(0o077)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

APP_ID = credential("lark_app_id")
APP_SECRET = credential("lark_app_secret")
OWNER_OPEN_ID = credential("lark_owner_open_id")

DB_PATH = Path(
    os.environ.get("ECHOMATE_DB_PATH", "/var/lib/echomate/assistant.db")
)
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
PC_DEVICE_ID = os.environ.get("ECHOMATE_PC_DEVICE_ID", "windows-main")
PC_PROFILE_ID = os.environ.get("ECHOMATE_PC_PROFILE_ID", "default")

if not APP_ID or not APP_SECRET:
    raise SystemExit("错误：App ID 或 App Secret 未配置")

if not OWNER_OPEN_ID.startswith("ou_"):
    raise SystemExit("错误：LARK_OWNER_OPEN_ID 未正确配置")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
os.chmod(DB_PATH.parent, 0o700)


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_due(utc_text):
    dt = datetime.fromisoformat(utc_text).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def format_due_friendly(utc_text):
    """今天/明天/后天 + HH:MM，更远的用 MM-DD HH:MM。"""
    dt = datetime.fromisoformat(utc_text).astimezone(LOCAL_TZ)
    delta = (dt.date() - datetime.now(LOCAL_TZ).date()).days

    if delta == 0:
        day = "今天"
    elif delta == 1:
        day = "明天"
    elif delta == 2:
        day = "后天"
    else:
        return dt.strftime("%m-%d %H:%M")

    return f"{day} {dt:%H:%M}"


# ==================== 数据库 ====================

def init_db():
    deploy_schema.init_deploy_schema(DB_PATH)


# ==================== 记忆与待办命令 ====================

def init_chat_table():
    """建立独立聊天历史表，不修改已有记忆和待办数据。"""
    conn = open_connection(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL
                    CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_messages_lookup
                ON chat_messages(owner_id, chat_id, id);
        """)
        conn.commit()
    finally:
        conn.close()

    os.chmod(DB_PATH, 0o600)


# ==================== 页面导航 ====================

PAGES = {
    "menu", "status", "status_detail", "todo", "todo_new",
    "todo_history",
    "memory_hub", "memory", "chat_memory", "help", "notify", "quiet",
    "records",
    "memory_clear_confirm",
    "pc", "pc_single", "pc_fixed", "pc_fixed_new",
    "pc_history", "pc_history_detail",
}

# 0 返回上一级；未列出的页面回到主菜单。
PAGE_PARENT = {
    "status_detail": "status",
    "todo_new": "todo",
    "todo_history": "todo",
    "quiet": "notify",
    "records": "notify",
    "memory": "memory_hub",
    "chat_memory": "memory_hub",
    "memory_clear_confirm": "memory",
    "pc_single": "pc",
    "pc_fixed": "pc",
    "pc_fixed_new": "pc_fixed",
    "pc_history": "pc",
    "pc_history_detail": "pc_history",
}


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


PAGE_UNKNOWN_HINT = {
    "status": "这里没有这个编号\n1 刷新 · 2 详情 · 0 返回",
    "status_detail": "这里没有这个编号\n1 刷新 · 0 返回",
    "todo": (
        "这里没有这个数字操作\n"
        "完成：完成 今天待办1\n"
        "取消：取消 明天待办1\n"
        "8 上一页 · 9 下一页 · 0 返回"
    ),
    "todo_new": "这里需要直接填写待办内容\n0 返回",
    "todo_history": "8 上一页 · 9 下一页 · 0 返回",
    "memory": "8 上一页 · 9 下一页 · 0 返回",
    "chat_memory": "8 上一页 · 9 下一页 · 0 返回",
    "memory_hub": "请选择 1 或 2，或回复 0 返回",
    "help": "帮助页可直接回复 0 返回",
    "notify": "这里没有这个编号\n请选择 1 到 4，或回复 0 返回",
    "quiet": "静音页请直接发送设置命令\n0 返回",
    "records": "8 上一页 · 9 下一页 · 0 返回",
    "memory_clear_confirm": "请发送“确认清空记忆”或 0 返回",
    "pc": "这里没有这个编号\n请选择 1 到 3，或回复 0 返回",
    "pc_single": "请直接发送一条任务\n0 取消",
    "pc_fixed": "请直接发送任务，或选择页面上的编号\n0 返回",
    "pc_fixed_new": "请直接发送第一条任务\n0 取消",
    "pc_history": "1-6 查看详情 · 8 上一页 · 9 下一页 · 0 返回",
    "pc_history_detail": "任务详情页可直接回复 0 返回",
}

PAGE_CARD_CONFIG = {
    "menu": ("Echo-Mate", "blue"),
    "status": ("运行状态", "green"),
    "status_detail": ("系统详情", "turquoise"),
    "todo": ("待办清单", "orange"),
    "todo_new": ("新建待办", "orange"),
    "todo_history": ("历史待办", "grey"),
    "memory": ("长期偏好", "purple"),
    "chat_memory": ("聊天记忆", "turquoise"),
    "memory_hub": ("记忆", "purple"),
    "help": ("使用帮助", "blue"),
    "notify": ("语音播报", "turquoise"),
    "quiet": ("静音设置", "grey"),
    "records": ("播报记录", "turquoise"),
    "memory_clear_confirm": ("清空记忆", "red"),
    "pc": ("本机 Codex", "purple"),
    "pc_single": ("本机 Codex", "purple"),
    "pc_fixed": ("本机 Codex", "purple"),
    "pc_fixed_new": ("本机 Codex", "purple"),
    "pc_history": ("本机 Codex", "purple"),
    "pc_history_detail": ("本机 Codex", "purple"),
}

PAGE_CARD_DROP_FIRST = {
    "🐣 Echo-Mate",
    "🔍 状态详情",
    "🧰 Echo-Mate 系统详情",
    "➕ 新建待办",
    "📜 历史待办",
    "💡 帮助",
    "🔊 播报设置",
    "🌙 静音设置",
    "📢 播报记录",
    "🧠 记忆",
    "💬 聊天记忆",
    "⚠️ 清空全部记忆？",
}

SERVICE_UNITS = [
    ("echomate-server.service", "语音服务", "echomate-server"),
    ("echomate-feishu-bot.service", "飞书机器人", "echomate-feishu-bot"),
    ("echomate-monitor.service", "资源监控", "echomate-monitor"),
    (
        "echomate-ssh-session-watch.service",
        "SSH 通知",
        "echomate-ssh-session-watch",
    ),
    ("echomate-flowview.service", "Flow 查看", "echomate-flowview"),
]
BACKUP_UNIT = ("echomate-db-backup.timer", "数据库备份", "echomate-db-backup")

PAGE_TTL_SECONDS = 86400
PAGED_PAGE_SIZES = {
    "todo": 8,
    "todo_history": 8,
    "memory": 5,
    "chat_memory": 8,
    "records": 8,
    "pc_history": 6,
}


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


def _toggle(kind_key, target):
    current = notify_store.get_setting(kind_key, "1")
    new_value = "0" if current == "1" else "1"
    notify_store.set_setting(kind_key, new_value)
    state_text = "关闭" if new_value == "0" else "开启"
    return f"✅ {target}播报{state_text}\n\n4 播报记录 · 0 返回"


def _handle_page_number(conn, chat_id, page, text):
    """页面内的数字指令。返回 None 表示不归本页处理。"""
    if page == "menu":
        target = {
            "1": "status",
            "2": "todo",
            "3": "todo_new",
            "4": "memory_hub",
            "5": "help",
            "6": "notify",
            "7": "pc",
        }.get(text)
        if target:
            if target == "pc" and not _pc_control_enabled(conn):
                return "🖥 本机 Codex 暂时关闭"
            return _enter(conn, chat_id, target)

    elif page == "status":
        if text == "1":
            return render_status()
        if text == "2":
            return _enter(conn, chat_id, "status_detail")

    elif page == "status_detail":
        if text == "1":
            return render_status_detail()

    elif page == "todo":
        if text == "3":
            return _enter(conn, chat_id, "todo_new")
        if text == "4":
            return _enter(conn, chat_id, "todo_history")

    elif page == "notify":
        if text == "1":
            return _enter(conn, chat_id, "quiet")
        if text == "2":
            return _toggle("notify_todo_enabled", "待办")
        if text == "3":
            return _toggle("notify_monitor_enabled", "监控")
        if text == "4":
            return _enter(conn, chat_id, "records")

    elif page == "memory_hub":
        if text == "1":
            return _enter(conn, chat_id, "memory")
        if text == "2":
            return _enter(conn, chat_id, "chat_memory")

    return None


def _pc_event_key(event_id):
    return event_id or f"pc-direct-{time.time_ns()}"


def _pc_job_ack(suffix=""):
    line = "✅ 已提交到本机 Codex\n正在等待电脑执行"
    if suffix:
        return f"{line}\n{suffix}"
    return line


def _handle_pc_context(conn, chat_id, page, text, event_id=None):
    if text == "0":
        return _navigate_back(conn, chat_id)

    if not _pc_control_enabled(conn):
        return "🖥 本机 Codex 暂时关闭\n\n0 返回"

    if len(text) > 20000:
        return "😅 任务太长了，20000 字以内"

    online, health = pc_control_service.health_snapshot()
    event_key = _pc_event_key(event_id)

    if page == "pc":
        target = {
            "1": "pc_single",
            "2": "pc_fixed",
            "3": "pc_history",
        }.get(text)
        if target:
            return _enter(conn, chat_id, target)
        return PAGE_UNKNOWN_HINT["pc"]

    if page == "pc_single":
        if not online:
            return pc_pages.render_pc_single(online, health)
        existing = pc_control_store.get_job_by_event(
            event_key, conn=conn
        )
        if existing is None:
            active_job = pc_control_store.get_active_job_for_device(
                PC_DEVICE_ID, conn=conn
            )
            if active_job is not None:
                return (
                    "🖥 电脑正在执行另一个任务\n\n"
                    "本机同一时间只执行一个任务。"
                )
            existing = pc_control_store.create_job(
                OWNER_OPEN_ID,
                chat_id,
                None,
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                "single",
                text,
                event_key,
                conn=conn,
            )
        home = _enter(conn, chat_id, "pc", mode="replace")
        return f"{_pc_job_ack()}\n\n{home}"

    if page == "pc_fixed_new":
        if not online:
            return pc_pages.render_pc_fixed_new(online, health)
        existing = pc_control_store.get_job_by_event(
            event_key, conn=conn
        )
        if existing is None:
            active_job = pc_control_store.get_active_job_for_device(
                PC_DEVICE_ID, conn=conn
            )
            if active_job is not None:
                return (
                    "🖥 电脑正在执行另一个任务\n\n"
                    "请等待完成或先中断当前任务。"
                )
            session_id = pc_control_store.create_session(
                OWNER_OPEN_ID,
                chat_id,
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                pc_pages.task_title(text),
                conn=conn,
            )
            pc_control_store.activate_session(
                OWNER_OPEN_ID, chat_id, session_id, conn=conn
            )
            existing = pc_control_store.create_job(
                OWNER_OPEN_ID,
                chat_id,
                session_id,
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                "fixed",
                text,
                event_key,
                conn=conn,
            )
        fixed_page = _enter(
            conn, chat_id, "pc_fixed", mode="replace"
        )
        return f"{_pc_job_ack()}\n\n{fixed_page}"

    if page == "pc_fixed":
        active = pc_control_store.get_active_session(
            OWNER_OPEN_ID, chat_id, conn=conn
        )
        if active is None:
            recoverable = pc_control_store.list_recoverable_sessions(
                OWNER_OPEN_ID, chat_id, conn=conn
            )
            if text in {"1", "2"}:
                index = int(text) - 1
                if index < len(recoverable):
                    pc_control_store.activate_session(
                        OWNER_OPEN_ID,
                        chat_id,
                        recoverable[index]["id"],
                        conn=conn,
                    )
                    return _enter(
                        conn, chat_id, "pc_fixed", mode="replace"
                    )
            if text == "3":
                return _enter(conn, chat_id, "pc_fixed_new")
            return (
                "🖥 请选择要恢复的会话，或发送 3 新建固定会话\n\n"
                "0 返回"
            )

        active_job = pc_control_store.get_active_job_for_device(
            PC_DEVICE_ID, conn=conn
        )

        if text == "1":
            return pc_pages.render_pc_job_status(active_job)

        if text == "2":
            if active_job is None:
                return "🖥 当前没有执行中的任务\n\n继续发送任务即可"
            requested = pc_control_store.request_job_cancel(
                active_job["id"],
                conn=conn,
            )
            if requested:
                return (
                    "🧹 已请求中断当前任务\n"
                    "后台会确认本机执行状态。"
                )
            return "🖥 这个任务已经结束，不需要中断"

        if text == "3":
            if active_job is not None:
                return "🖥 当前任务还在执行，先发送 2 中断"
            pc_control_store.pause_active_session(
                OWNER_OPEN_ID, chat_id, conn=conn
            )
            return _enter(conn, chat_id, "pc_fixed_new")

        if active_job is not None:
            state = pc_pages.JOB_LABELS.get(
                active_job["status"], active_job["status"]
            )
            return (
                f"🖥 当前任务仍在{state}\n\n"
                "发送 1 查看状态，或 2 中断"
            )

        if not online:
            return pc_pages.render_pc_fixed(
                active,
                pc_control_store.list_recoverable_sessions(
                    OWNER_OPEN_ID, chat_id, conn=conn
                ),
                active_job,
                online,
                health,
                pc_control_store.count_session_turns(
                    active["id"], conn=conn
                ),
            )

        existing = pc_control_store.get_job_by_event(
            event_key, conn=conn
        )
        if existing is None:
            existing = pc_control_store.create_job(
                OWNER_OPEN_ID,
                chat_id,
                active["id"],
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                "fixed",
                text,
                event_key,
                conn=conn,
            )
        fixed_page = _render_page_state(
            conn, chat_id, "pc_fixed"
        )[0]
        return f"{_pc_job_ack()}\n\n{fixed_page}"

    if page == "pc_history":
        state = get_page_state(chat_id, conn=conn)
        job_ids = state.get("history_job_ids") or []
        if text.isdigit():
            index = int(text) - 1
            if 0 <= index < len(job_ids):
                job = pc_control_store.get_history_job(
                    job_ids[index],
                    OWNER_OPEN_ID,
                    chat_id,
                    conn=conn,
                )
                stack = state_nav_stack(state) + [
                    "pc_history_detail"
                ]
                set_page(
                    chat_id,
                    "pc_history_detail",
                    state={
                        "history_job_id": job_ids[index],
                        "nav_stack": stack,
                    },
                    conn=conn,
                )
                return pc_pages.render_pc_history_detail(job)
        return PAGE_UNKNOWN_HINT["pc_history"]

    if page == "pc_history_detail":
        return PAGE_UNKNOWN_HINT["pc_history_detail"]

    return PAGE_UNKNOWN_HINT.get(page, "🖥 本机 Codex\n\n0 返回")


def _add_todo(conn, chat_id, date_text, time_text, title):
    """创建待办，供严格格式与自然输入共用。"""
    title = (title or "").strip()

    if not title:
        return "😅 待办得写点内容\n\n直接按格式重发 · 0 返回"

    if len(title) > 200:
        return "😅 标题太长，200 字以内\n\n直接按格式重发 · 0 返回"

    try:
        due_local = datetime.strptime(
            f"{date_text} {time_text}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return "😅 日期写成 2026-10-26 09:30 这样\n\n直接按格式重发 · 0 返回"

    if due_local <= datetime.now(LOCAL_TZ):
        return (
            "😅 这个时间不能早于当前时间\n\n"
            f"现在是 {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}\n"
            "请填写之后的时间\n\n"
            "直接按格式重发 · 0 返回"
        )

    due_utc = due_local.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )

    todo_id, result = echomate_store.create_todo(
        OWNER_OPEN_ID,
        title,
        due_utc,
        source="feishu",
        chat_id=chat_id,
        notify_deskbot=True,
        notify_feishu=True,
        conn=conn,
    )

    if result == "duplicate":
        return_message = (
            "ℹ️ 这条待办已经存在\n\n"
            f"{title} · {format_due_friendly(due_utc)}"
        )
        return return_message + "\n\n" + _enter(
            conn, chat_id, "todo", mode="replace"
        )

    if result != "created":
        return "😅 待办没保存成功，稍后再试\n\n直接按格式重发 · 0 返回"

    return_message = (
        "✅ 已记下\n\n"
        f"{title} · {format_due_friendly(due_utc)}\n"
        "到点我会提醒你"
    )
    return return_message + "\n\n" + _enter(
        conn, chat_id, "todo", mode="replace"
    )


def _parse_natural_todo(text, allow_time_only=False):
    """解析自然输入，返回 (日期, 时间, 标题)、("past", None, None, None) 或 None。

    支持：
      2026-10-25 14：54 呼叫林梦雨   （全角/半角/点号均可）
      今天 14:54 呼叫林梦雨
      明天 9:05 交周报
      14:54 呼叫林梦雨              （仅待办页内）
    """
    raw = (text or "").strip()
    sep = r"[:：.．]"

    match = re.match(
        rf"^(\d{{4}}-\d{{2}}-\d{{2}})\s+(\d{{1,2}})\s*{sep}\s*(\d{{2}})\s+(.+)$",
        raw,
        re.DOTALL,
    )
    if match:
        date_text, hour, minute, title = match.groups()
        return date_text, f"{int(hour):02d}:{minute}", title.strip()

    match = re.match(
        rf"^(今天|明天|后天)\s+(\d{{1,2}})\s*{sep}\s*(\d{{2}})\s+(.+)$",
        raw,
        re.DOTALL,
    )
    if match:
        day_text, hour, minute, title = match.groups()
        offset = {"今天": 0, "明天": 1, "后天": 2}[day_text]
        target = datetime.now(LOCAL_TZ).date() + timedelta(days=offset)
        return (
            target.strftime("%Y-%m-%d"),
            f"{int(hour):02d}:{minute}",
            title.strip(),
        )

    if allow_time_only:
        match = re.match(
            rf"^(\d{{1,2}})\s*{sep}\s*(\d{{2}})\s+(.+)$",
            raw,
            re.DOTALL,
        )
        if match:
            hour, minute, title = match.groups()
            now = datetime.now(LOCAL_TZ)
            target = now.date()
            candidate = now.replace(
                hour=int(hour), minute=int(minute),
                second=0, microsecond=0,
            )
            if candidate <= now:
                return "past", None, None, None
            return (
                target.strftime("%Y-%m-%d"),
                f"{int(hour):02d}:{minute}",
                title.strip(),
            )

    return None


def _handle_todo_action(conn, text, chat_id):
    match = re.match(
        r"^(完成|取消)\s*(今天|明天)待办\s*#?(\d+)\s*$",
        text,
    )
    if not match:
        return None

    action, day, index_text = match.groups()
    state = get_page_state(chat_id, conn=conn)
    if state.get("page") != "todo":
        return "😅 请先进入待办页再操作\n\n发送：待办"

    today_key = datetime.now(LOCAL_TZ).date().isoformat()
    if state.get("day_key") != today_key:
        return (
            "😅 待办页已经跨天，已重新加载\n\n"
            + _enter(conn, chat_id, "todo", mode="replace")
        )

    id_key = "today_ids" if day == "今天" else "tomorrow_ids"
    todo_ids = state.get(id_key) or []
    index = int(index_text)
    if index < 1 or index > len(todo_ids):
        return (
            f"😅 没有找到{day}待办{index}\n\n"
            + _enter(conn, chat_id, "todo", mode="replace")
        )

    todo_id = int(todo_ids[index - 1])
    status = "completed" if action == "完成" else "cancelled"
    cursor = conn.execute(
        """
        UPDATE todos SET status = ?
        WHERE id = ? AND owner_id = ? AND status = 'pending'
        """,
        (status, todo_id, OWNER_OPEN_ID),
    )
    if not cursor.rowcount:
        return (
            f"😅 {day}待办{index} 已经变化\n\n"
            + _enter(conn, chat_id, "todo", mode="replace")
        )

    marker = "✅ 已完成" if action == "完成" else "🧹 已取消"
    return f"{marker}：{day}待办{index}\n\n" + _enter(
        conn, chat_id, "todo"
    )


def handle_command(conn, text, chat_id, event_id=None):
    text = text.strip()

    # Global escape commands must work from every page, including PC capture.
    if text in {"菜单", "功能"}:
        return _enter(conn, chat_id, "menu", mode="reset")
    if text == "0":
        return _navigate_back(conn, chat_id)

    page = get_page(chat_id, conn=conn)

    paged = _handle_pagination(conn, chat_id, text)
    if paged is not None:
        return paged

    if page in PC_PAGES:
        return _handle_pc_context(
            conn, chat_id, page, text, event_id=event_id
        )

    todo_action = _handle_todo_action(conn, text, chat_id)
    if todo_action is not None:
        return todo_action

    match = re.match(r"^(确认|取消)候选\s*(\d+)\s*$", text)
    if match:
        action, index_text = match.groups()
        display_index = int(index_text)
        candidates = echomate_store.list_memory_candidates(
            OWNER_OPEN_ID, 10, conn=conn
        )
        if 1 <= display_index <= len(candidates):
            candidate_id = candidates[display_index - 1]["id"]
            decision = "accept" if action == "确认" else "reject"
            if echomate_store.decide_memory_candidate(
                OWNER_OPEN_ID, candidate_id, decision, conn=conn
            ):
                result = "写入" if action == "确认" else "取消"
                return f"✅ 候选记忆已{result}"
        return "没有找到这条候选记忆\n\n回复：记忆候选"

    match = re.match(r"^确认记忆\s*#?(\d+)\s*$", text)
    if match:
        candidate_id = int(match.group(1))
        if echomate_store.decide_memory_candidate(
            OWNER_OPEN_ID, candidate_id, "accept", conn=conn
        ):
            return "✅ 记忆已确认写入"
        return "没有找到这条候选记忆"

    match = re.match(r"^取消记忆\s*#?(\d+)\s*$", text)
    if match:
        candidate_id = int(match.group(1))
        if echomate_store.decide_memory_candidate(
            OWNER_OPEN_ID, candidate_id, "reject", conn=conn
        ):
            return "✅ 已取消候选记忆"
        return "没有找到这条候选记忆"

    if text in {"记忆候选", "待确认记忆"}:
        candidates = echomate_store.list_memory_candidates(
            OWNER_OPEN_ID, 10, conn=conn
        )
        if not candidates:
            return "📚 没有待确认的候选记忆"
        lines = [f"📝 待确认候选 {len(candidates)} 条", ""]
        for sequence, item in enumerate(candidates, 1):
            lines.append(f"{sequence}. {item['content'][:60]}")
        lines.extend([
            "",
            "确认：确认候选 1",
            "取消：取消候选 1",
        ])
        return "\n".join(lines)

    # -------- 页面内数字 --------

    if text.isdigit():
        handled = None
        if text in {"1", "2", "3", "4", "5", "6", "7"}:
            handled = _handle_page_number(conn, chat_id, page, text)
        if handled is not None:
            return handled
        if page != "menu":
            return PAGE_UNKNOWN_HINT.get(
                page,
                "😅 本页没有这个数字操作\n0 返回",
            )
        return "😅 主菜单没有这个编号\n请输入 1 到 7"

    # -------- 直接进入页面的文字命令 --------

    if text in {
        "检查 VPS", "检查VPS", "VPS 状态", "VPS状态",
        "服务器状态", "状态",
    }:
        return _enter(conn, chat_id, "status", mode="reset")

    if text in {"状态详情", "详情"}:
        return _enter(conn, chat_id, "status_detail", mode="reset")

    if text == "帮助":
        return _enter(conn, chat_id, "help", mode="reset")

    if text in {"播报设置", "语音播报设置"}:
        return _enter(conn, chat_id, "notify", mode="reset")

    if text in {"播报记录", "语音播报记录"}:
        return _enter(conn, chat_id, "records", mode="reset")

    if text in {"我的待办", "待办列表", "查看待办", "待办"}:
        return _enter(conn, chat_id, "todo", mode="reset")

    if text in {"历史待办", "待办历史"}:
        return _enter(conn, chat_id, "todo_history", mode="reset")

    if text in {"记忆", "记忆中心"}:
        return _enter(conn, chat_id, "memory_hub", mode="reset")

    if text in {
        "列出记忆", "我的记忆", "记忆列表",
        "长期记忆", "长期偏好",
    }:
        return _enter(conn, chat_id, "memory", mode="reset")

    if text in {"聊天记忆", "聊天记忆列表"}:
        return _enter(conn, chat_id, "chat_memory", mode="reset")

    match = re.match(r"^删除聊天记忆\s*(\d+)\s*$", text)
    if match:
        display_index = int(match.group(1))
        state = get_page_state(chat_id, conn=conn)
        ids = state.get("chat_memory_ids") or []
        if not (1 <= display_index <= len(ids)):
            return "没有找到这条聊天记忆\n\n聊天记忆"
        if not echomate_store.delete_chat_memory(
            OWNER_OPEN_ID, ids[display_index - 1], conn=conn
        ):
            return "这条聊天记忆已经不存在\n\n聊天记忆"
        return (
            "🗑 已删除聊天记忆\n\n"
            + _enter(conn, chat_id, "chat_memory", mode="replace")
        )

    if text in {"添加待办", "新建待办"}:
        return _enter(conn, chat_id, "todo_new", mode="reset")

    if text in {"本机 Codex", "本机Codex", "电脑控制", "本机电脑"}:
        if not _pc_control_enabled(conn):
            return "🖥 本机 Codex 暂时关闭"
        return _enter(conn, chat_id, "pc", mode="reset")

    # -------- 静音设置 --------

    match = re.match(
        r"^静音\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s*$",
        text,
    )

    if match:
        start_text, end_text = match.groups()
        start = _normalize_hhmm(start_text)
        end = _normalize_hhmm(end_text)

        if start is None or end is None:
            return "😅 时间写成 23:00 08:00 这样\n\n0 返回"

        if start == end:
            return "😅 开始和结束不能一样\n\n0 返回"

        notify_store.set_setting("notify_quiet_start", start)
        notify_store.set_setting("notify_quiet_end", end)

        return (
            f"🔇 静音 {start}–{end}\n\n"
            "这段桌宠闭嘴，飞书照常\n\n"
            "0 返回"
        )

    if text in {"关闭静音", "取消静音"}:
        notify_store.set_setting("notify_quiet_start", "")
        notify_store.set_setting("notify_quiet_end", "")
        return "🔔 静音关掉了\n\n0 返回"

    match = re.match(r"^(关闭|开启)(待办|监控)播报$", text)

    if match:
        action, target = match.groups()
        key = (
            "notify_todo_enabled"
            if target == "待办"
            else "notify_monitor_enabled"
        )
        notify_store.set_setting(key, "1" if action == "开启" else "0")
        state_text = "开启" if action == "开启" else "关闭"
        return f"✅ {target}播报{state_text}\n\n0 返回"

    # -------- 创建待办 --------

    if text.startswith(("添加待办", "新建待办")):
        # A valid explicit command works anywhere. Invalid text falls through
        # to the current page or to the AI router instead of being intercepted.
        prefix_match = re.match(
            r"^(?:添加待办|新建待办)\s+"
            r"(\d{4}-\d{2}-\d{2})\s+"
            r"(\d{1,2})\s*[:：.．]\s*(\d{2})\s+(.+)$",
            text,
            re.DOTALL,
        )
        if prefix_match:
            date_text, hour, minute, title = prefix_match.groups()
            return _add_todo(
                conn, chat_id, date_text,
                f"{int(hour):02d}:{minute}", title,
            )

    match = re.match(
        r"^添加待办\s+(\d{4}-\d{2}-\d{2})\s+"
        r"(\d{2}:\d{2})\s+(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        date_text, time_text, title = match.groups()
        return _add_todo(conn, chat_id, date_text, time_text, title)

    # -------- 完成或取消待办 --------

    match = re.match(
        r"^(完成待办|取消待办)\s*[:：#]?\s*(\d+)\s*$",
        text,
    )

    if match:
        action, todo_id_text = match.groups()
        todo_id = int(todo_id_text)
        new_status = "completed" if action == "完成待办" else "cancelled"

        cursor = conn.execute(
            """
            UPDATE todos
            SET status = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (new_status, todo_id, OWNER_OPEN_ID),
        )

        if not cursor.rowcount:
            return "没有找到这条待办\n\n发送：待办"

        if action == "取消待办":
            return (
                "🧹 待办已取消\n\n"
                + _enter(conn, chat_id, "todo", mode="replace")
            )

        return "✅ 待办已完成\n\n" + _enter(
            conn, chat_id, "todo", mode="replace"
        )

    # -------- 保存记忆 --------

    match = re.match(
        r"^(?:记住|保存记忆)(?:\s*[:：]\s*|\s+)(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        content = match.group(1).strip()

        if not content:
            return "😅 要记的内容是空的\n\n4 看记忆 · 0 返回"

        if len(content) > 2000:
            return "😅 太长了，2000 字以内\n\n4 看记忆 · 0 返回"

        candidate_id, result = echomate_store.enqueue_memory_candidate(
            OWNER_OPEN_ID, content, source="feishu", conn=conn
        )
        if result == "created":
            display_index = _memory_candidate_display_index(
                conn, candidate_id
            )
            return (
                "📝 已生成记忆候选\n\n"
                f"{content[:150]}\n\n"
                f"确认：确认候选 {display_index}\n"
                f"取消：取消候选 {display_index}"
            )
        if result == "duplicate":
            return "ℹ️ 已有相同候选记忆\n\n回复：记忆候选"
        if result == "too_long":
            return "😅 太长了，2000 字以内"
        return "😅 要记的内容是空的"

    # -------- 搜索记忆 --------

    match = re.match(
        r"^(?:查记忆|搜索记忆)\s*[:：]?\s*(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        keyword = match.group(1).strip()

        rows = conn.execute(
            """
            SELECT id, content
            FROM memories
            WHERE owner_id = ? AND content LIKE ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (OWNER_OPEN_ID, f"%{keyword}%"),
        ).fetchall()

        if not rows:
            return (
                f"😅 没找到「{keyword}」\n\n"
                "1 全部 · 0 返回"
            )

        lines = [f"🔎 找到 {len(rows)} 条", ""]
        for sequence, (_, content) in enumerate(rows, 1):
            lines.append(f"{sequence}. {content[:120]}")
        lines.extend(["", "4 看记忆 · 0 返回"])
        return "\n".join(lines)

    # -------- 删除记忆 --------

    match = re.match(r"^删除第\s*(\d+)\s*条(?:记忆)?$", text)
    if match:
        display_index = int(match.group(1))
        state = get_page_state(chat_id, conn=conn)
        memory_ids = state.get("memory_ids") or []
        if display_index < 1 or display_index > len(memory_ids):
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        memory_id = int(memory_ids[display_index - 1])
        row = conn.execute(
            "SELECT content FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, OWNER_OPEN_ID),
        ).fetchone()
        if row is None:
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        content = row[0]
        if not echomate_store.delete_memory(
            OWNER_OPEN_ID, memory_id, conn=conn
        ):
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        return (
            "✅ 已删除这条记忆\n\n"
            f"{content[:40]}\n\n"
            "4 看记忆 · 0 返回"
        )

    match = re.match(r"^删除记忆\s*[:：#]?\s*(\d+)\s*$", text)

    if match:
        memory_id = int(match.group(1))
        row = conn.execute(
            "SELECT content FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, OWNER_OPEN_ID),
        ).fetchone()

        if not row:
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"

        if not echomate_store.delete_memory(
            OWNER_OPEN_ID, memory_id, conn=conn
        ):
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        preview = row[0][:40]
        return (
            "✅ 已删除这条记忆\n\n"
            f"{preview}\n\n"
            "4 看记忆 · 0 返回"
        )

    # 清空全部记忆需要两步确认，避免误触。
    if text in {"清空记忆", "删除全部记忆"}:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not total:
            return "📚 本来就没有记忆\n\n0 返回"

        set_page(chat_id, "memory_clear_confirm", conn=conn)
        return (
            f"⚠️ 要清空全部 {total} 条记忆？\n\n"
            "删除后无法恢复。\n"
            "确认发：确认清空记忆\n"
            "取消发：0\n\n"
            "0 取消"
        )

    if text == "确认清空记忆":
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not total:
            set_page(chat_id, "memory", conn=conn)
            return "📚 本来就没有记忆\n\n0 返回"

        removed = echomate_store.clear_memories(
            OWNER_OPEN_ID, conn=conn
        )
        set_page(chat_id, "memory", conn=conn)
        return (
            f"✅ 清空了 {removed} 条记忆\n\n"
            "以后想存新的，发：记住 内容\n\n"
            "0 返回"
        )

    # -------- 页面上下文：PC 页面已在入口处分流 --------

    if page == "todo_new":
        parsed = _parse_natural_todo(text, allow_time_only=True)
        if parsed and parsed[0] == "past":
            return (
                "😅 这个时间不能早于当前时间\n\n"
                f"现在是 {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}\n"
                "请填写之后的时间\n\n"
                "直接按格式重发 · 0 返回"
            )
        if parsed is not None:
            date_text, time_text, title = parsed
            return _add_todo(conn, chat_id, date_text, time_text, title)
        return render_todo_new() + "\n\n😅 这次没解析成功，请按上面的格式重发。"

    # Outside todo_new, free text belongs to the AI tool router.
    return None


# ==================== 飞书消息发送 ====================

api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .build()
)



# ==================== Qwen 普通聊天 ====================

server_cfg = dotenv_values(
    Path("/root/echomate-deploy/app/Demo/AIChat_demo/Server/.env")
)

QWEN_API_KEY = credential("dashscope_api_key")
QWEN_MODEL = (server_cfg.get("CHAT_MODEL") or "qwen-max").strip()
MEMORY_MODEL = (server_cfg.get("MEMORY_MODEL") or "qwen-plus").strip()

# 联网搜索凭据由 systemd 单独注入，不进仓库。
TAVILY_API_KEY = credential("tavily_api_key")
TAVILY_URL = "https://api.tavily.com/search"
SEARCH_CACHE_TTL = 600
_search_cache = {}

# 沿用此前独立 API 测试成功的地域/Workspace 端点选择方式。
ws_endpoint = (
    server_cfg.get("DASHSCOPE_BASE_WEBSOCKET_API_URL") or ""
).strip()
ws_host = urlparse(ws_endpoint).hostname

if ws_host and ws_host.endswith(".maas.aliyuncs.com"):
    QWEN_BASE_URL = f"https://{ws_host}/compatible-mode/v1"
else:
    QWEN_BASE_URL = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

qwen_client = None
if QWEN_API_KEY:
    qwen_client = OpenAI(
        api_key=QWEN_API_KEY,
        base_url=QWEN_BASE_URL,
        timeout=35.0,
        max_retries=0,
    )

memory_observer = (
    MemoryObserver(qwen_client, MEMORY_MODEL)
    if qwen_client is not None
    else None
)


MEMORY_CONTEXT_LIMIT = 50


def _normalize_memory(text):
    """用于去重的归一化：去空白、去标点、统一小写。"""
    return re.sub(r"[\s，。！？、,.!?：:；;]+", "", text or "").lower()


def load_memory_context(limit=MEMORY_CONTEXT_LIMIT):
    """读取最近记忆，去重后返回列表。"""
    conn = open_connection(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT content FROM memories
            WHERE owner_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (OWNER_OPEN_ID, limit * 3),
        ).fetchall()
    finally:
        conn.close()

    seen = set()
    items = []

    for (content,) in rows:
        content = (content or "").strip()
        if not content:
            continue

        key = _normalize_memory(content)
        if not key or key in seen:
            continue

        seen.add(key)
        items.append(content)

        if len(items) >= limit:
            break

    return items


def memory_system_block(items):
    """把记忆整理成 system prompt 片段。"""
    if not items:
        return ""

    lines = "\n".join(f"- {item}" for item in items)
    return (
        "\n\n以下是用户主动让你记住的信息，回答时可以直接使用：\n"
        "<memory>\n"
        f"{lines}\n"
        "</memory>\n"
        "使用规则：\n"
        "1. 问题涉及用户个人属性（居住地、生日、职业、偏好、称呼等）时，"
        "必须使用上面的记忆，不要反问用户。\n"
        "2. 记忆里没有的信息，直接说不知道，不要编造。\n"
        "3. 记忆和问题确实无关时才可以忽略。"
    )


WEATHER_KEYWORDS = ("天气", "气温", "温度", "下雨", "降雨", "下雪")

# WMO 天气代码 -> 中文
WMO_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

WEATHER_SUFFIXES = (
    "市", "区", "县", "镇", "乡", "省", "自治区", "特别行政区",
)


def _is_weather_question(text):
    return any(word in (text or "") for word in WEATHER_KEYWORDS)


PLACE_STOPWORDS = (
    "这段时间", "这几天", "最近", "现在", "当前", "今天", "明天",
    "后天", "未来", "之后", "这边", "那边", "那里", "这里",
    "怎么样", "如何", "多少", "几度", "天气", "气温", "温度",
)


def _extract_place(text):
    """从问句里抽地名；时间副词和疑问词不算地名。"""
    raw = (text or "").strip()

    cleaned = raw
    for word in (
        *PLACE_STOPWORDS,
        "这边", "那边", "会", "下", "有", "的", "吗", "呢", "啊",
        "？", "?", "，", ",", "。", ".", "！", "!",
    ):
        cleaned = cleaned.replace(word, " ")

    for token in re.split(r"\s+", cleaned):
        token = token.strip()
        if len(token) < 2:
            continue

        # 剩余 token 仍是时间/程度词时不算地名。
        if any(stop in token for stop in PLACE_STOPWORDS):
            continue

        return token

    return None


def _memory_places():
    """从记忆里抽居住地候选，按记忆新旧排序。"""
    candidates = []

    for item in load_memory_context():
        if not any(
            word in item for word in ("居住", "住在", "家住", "所在地", "城市")
        ):
            continue

        match = re.search(
            r"(?:居住地|住在|家住|所在地|城市)(?:是|在|为|：|:)?\s*"
            r"([\u4e00-\u9fa5]{2,20})",
            item,
        )
        if match:
            candidates.append(match.group(1))

    return candidates


PROVINCES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林",
    "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西",
    "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆",
    "香港", "澳门",
)


def _place_candidates(place):
    """生成地名候选，覆盖「江西南昌」「南昌市」「南昌市西湖区」等写法。"""
    candidates = []

    def add(value):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(place)

    # 去掉行政后缀
    trimmed = place
    for suffix in WEATHER_SUFFIXES:
        if trimmed.endswith(suffix) and len(trimmed) > len(suffix):
            trimmed = trimmed[: -len(suffix)]
            add(trimmed)
            break

    # 去掉省级前缀：「江西南昌」→「南昌」
    for province in PROVINCES:
        if trimmed.startswith(province) and len(trimmed) > len(province):
            add(trimmed[len(province):])
            break

    # 「南昌市西湖区」这类：再截到区县
    for suffix in ("区", "县"):
        if trimmed.endswith(suffix):
            core = trimmed[: -len(suffix)]
            for province in PROVINCES:
                if core.startswith(province):
                    core = core[len(province):]
                    break
            add(core)
            break

    return candidates


def _geocode(place):
    """把地名换成经纬度，按候选列表逐个尝试。"""
    for candidate in _place_candidates(place):
        try:
            response = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={
                    "name": candidate,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
                timeout=8,
            )
            data = response.json()
        except Exception:
            continue

        results = data.get("results") or []
        if results:
            top = results[0]
            return {
                "name": top.get("name", candidate),
                "admin": top.get("admin1", ""),
                "lat": top.get("latitude"),
                "lon": top.get("longitude"),
            }

    return None


def _fetch_weather(place_info, days=1):
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place_info["lat"],
                "longitude": place_info["lon"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "weather_code,precipitation_probability_max"
                ),
                "timezone": "Asia/Shanghai",
                "forecast_days": days,
            },
            timeout=8,
        )
        return response.json()
    except Exception:
        return None


def weather_reply(text):
    """回答天气问题；无法回答时返回 None，交给普通聊天。"""
    if not _is_weather_question(text):
        return None

    asked_place = _extract_place(text)

    place_info = None
    asked_label = None

    # 先试问句里的地名；查不到不报错，回退记忆。
    if asked_place:
        place_info = _geocode(asked_place)
        if place_info:
            asked_label = asked_place

    if not place_info:
        memory_places = _memory_places()
        if memory_places:
            place_info = _geocode(memory_places[0])

    if not place_info:
        # 问句指了地名但没查到，且记忆也没有 → 这才提示。
        if asked_place:
            return (
                f"😅 没找到「{asked_place}」这个地方\n\n"
                "发「南昌天气」试试，或者发\n"
                "记住 我现在居住地是城市名\n"
            )

        return (
            "🌤 想知道哪里的天气？\n\n"
            "发「南昌天气」这样就行\n"
            "也可以先存一条居住地：\n"
            "记住 我现在居住地是江西南昌"
        )

    data = _fetch_weather(place_info)
    if not data:
        return "😅 天气服务暂时连不上，稍后再试"

    current = data.get("current") or {}
    daily = data.get("daily") or {}

    temp = current.get("temperature_2m")
    humidity = current.get("relative_humidity_2m")
    code = current.get("weather_code")

    if temp is None:
        return "😅 天气数据不完整，稍后再试"

    condition = WMO_TEXT.get(code, "未知")

    label = place_info["name"]
    admin = place_info.get("admin") or ""
    if (
        admin
        and admin not in label
        and label not in admin
    ):
        label = f"{admin}{label}"

    # 问的是「这几天/最近」时，带未来三天预报。
    wants_range = any(
        word in (text or "")
        for word in ("这几天", "这段时间", "最近", "未来")
    )

    if wants_range and len(daily.get("temperature_2m_max") or []) >= 3:
        highs = daily["temperature_2m_max"]
        lows = daily["temperature_2m_min"]
        codes = daily.get("weather_code") or []
        dates = daily.get("time") or []

        lines = [
            f"🌤 {label}未来三天",
            "",
        ]

        day_names = ["今天", "明天", "后天"]
        for index in range(3):
            day = day_names[index] if index < 3 else dates[index]
            day_condition = WMO_TEXT.get(
                codes[index] if index < len(codes) else -1, ""
            )
            lines.append(
                f"{day} {lows[index]:.0f}–{highs[index]:.0f}°C"
                f"{' · ' + day_condition if day_condition else ''}"
            )

        return "\n".join(lines)

    lines = [
        f"🌤 {label}天气",
        "",
        f"现在 {temp:.0f}°C · {condition}",
    ]

    if humidity is not None:
        lines[-1] += f" · 湿度 {humidity:.0f}%"

    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    if highs and lows:
        lines.append(f"今天 {lows[0]:.0f}–{highs[0]:.0f}°C")

    rain = daily.get("precipitation_probability_max") or []
    if rain and rain[0] is not None:
        lines.append(f"降水概率 {rain[0]:.0f}%")

    return "\n".join(lines)


CHAT_TOOLS = assistant_core.CHAT_TOOLS
READ_ONLY_TOOLS = assistant_core.READ_ONLY_TOOLS


def _tool_get_weather(args):
    place = (args.get("place") or "").strip()
    days = args.get("days") or 3

    try:
        days = max(1, min(int(days), 7))
    except (TypeError, ValueError):
        days = 3

    try:
        offset = max(0, min(int(args.get("day_offset") or 0), 6))
    except (TypeError, ValueError):
        offset = 0

    # 从今天开始取，保证 offset 之后的日期都在结果里。
    fetch_days = min(7, offset + days)

    place_info = None

    if place:
        place_info = _geocode(place)

    if not place_info:
        places = _memory_places()
        if places:
            place_info = _geocode(places[0])
            if place_info and not place:
                place = places[0]

    if not place_info:
        return {
            "ok": False,
            "error": (
                f"找不到地点「{place}」"
                if place
                else "没有地点，也没有记忆里的居住地"
            ),
        }

    data = _fetch_weather(place_info, days=fetch_days)
    if not data:
        return {"ok": False, "error": "天气服务暂时不可用"}

    current = data.get("current") or {}
    daily = data.get("daily") or {}

    label = place_info["name"]
    admin = place_info.get("admin") or ""
    if admin and admin not in label and label not in admin:
        label = f"{admin}{label}"

    forecast = []
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    rains = daily.get("precipitation_probability_max") or []

    for index in range(offset, min(offset + days, len(dates))):
        forecast.append({
            "date": dates[index],
            "high_c": round(highs[index], 1) if index < len(highs) else None,
            "low_c": round(lows[index], 1) if index < len(lows) else None,
            "condition": WMO_TEXT.get(
                codes[index] if index < len(codes) else -1, "未知"
            ),
            "rain_chance": (
                round(rains[index]) if index < len(rains)
                and rains[index] is not None else None
            ),
        })

    return {
        "ok": True,
        "place": label,
        "now": {
            "temp_c": (
                round(current["temperature_2m"], 1)
                if current.get("temperature_2m") is not None else None
            ),
            "condition": WMO_TEXT.get(current.get("weather_code"), "未知"),
            "humidity": (
                round(current["relative_humidity_2m"])
                if current.get("relative_humidity_2m") is not None else None
            ),
        },
        "forecast": forecast,
    }


def _tool_list_todos(args):
    conn = open_connection(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT id, title, due_at_utc
            FROM todos
            WHERE owner_id = ? AND status = 'pending'
            ORDER BY due_at_utc ASC
            LIMIT 20
            """,
            (OWNER_OPEN_ID,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "ok": True,
        "count": len(rows),
        "items": [
            {
                "id": todo_id,
                "title": title,
                "due": format_due_friendly(due_utc),
            }
            for todo_id, title, due_utc in rows
        ],
    }


def _tool_search_memories(args):
    keyword = (args.get("keyword") or "").strip()

    conn = open_connection(DB_PATH)
    try:
        if keyword:
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE owner_id = ? AND content LIKE ?
                ORDER BY id DESC LIMIT 10
                """,
                (OWNER_OPEN_ID, f"%{keyword}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE owner_id = ?
                ORDER BY id DESC LIMIT 10
                """,
                (OWNER_OPEN_ID,),
            ).fetchall()
    finally:
        conn.close()

    return {
        "ok": True,
        "count": len(rows),
        "items": [
            {"id": memory_id, "content": content}
            for memory_id, content in rows
        ],
    }


def _tool_add_todo(args):
    title = (
        args.get("title")
        or args.get("content")
        or args.get("description")
        or ""
    ).strip()
    due_text = (args.get("due_at") or "").strip()
    due = assistant_actions._parse_local_datetime(due_text)
    if not title:
        return {"ok": False, "error": "empty", "due_at": due_text}
    if due is None:
        return {"ok": False, "error": "bad_due", "due_at": due_text}

    now_local = datetime.now(LOCAL_TZ)
    readable_due = due.strftime("%Y-%m-%d %H:%M")
    if due <= now_local:
        return {
            "ok": False,
            "error": "past_due",
            "due_text": readable_due,
            "current_time": now_local.strftime("%Y-%m-%d %H:%M"),
        }

    todo_id, result = echomate_store.create_todo(
        OWNER_OPEN_ID,
        title,
        due.astimezone(timezone.utc).isoformat(timespec="seconds"),
        source="feishu",
        chat_id=CURRENT_CHAT_ID.get(),
        notify_deskbot=True,
        notify_feishu=True,
    )
    if result not in ("created", "duplicate"):
        response = {
            "ok": False,
            "error": result,
            "due_text": readable_due,
        }
        if result == "past_due":
            response["current_time"] = datetime.now(LOCAL_TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
        return response
    return {
        "ok": True,
        "todo_id": todo_id,
        "duplicate": result == "duplicate",
        "title": title,
        "due_text": due.strftime("%Y-%m-%d %H:%M"),
    }


def _tool_propose_memory(args):
    content = (args.get("content") or "").strip()
    category = (args.get("category") or "").strip() or None
    candidate_id, result = echomate_store.enqueue_memory_candidate(
        OWNER_OPEN_ID,
        content,
        source="feishu_ai",
        category=category,
    )
    return {
        "ok": result in ("created", "duplicate"),
        "candidate_id": candidate_id,
        "result": result,
    }


def _tool_web_search(args):
    """调用 Tavily 搜索，返回标题、链接、摘要。"""
    query = (args.get("query") or "").strip()

    if not query:
        return {"ok": False, "error": "缺少搜索关键词"}

    if not TAVILY_API_KEY:
        return {"ok": False, "error": "未配置 TAVILY_API_KEY"}

    max_results = args.get("max_results") or 5
    try:
        max_results = max(1, min(int(max_results), 8))
    except (TypeError, ValueError):
        max_results = 5

    cache_key = f"{query.lower()}|{max_results}"
    cached = _search_cache.get(cache_key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1]

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "include_answer": True,
        "search_depth": "basic",
    }

    try:
        response = requests.post(TAVILY_URL, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"[搜索] 请求失败：{type(exc).__name__}", flush=True)
        return {"ok": False, "error": "搜索服务暂时不可用"}

    results = []
    for item in (data.get("results") or [])[:max_results]:
        results.append({
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "content": (item.get("content") or "")[:1200],
        })

    if not results:
        return {"ok": False, "error": f"没有搜到「{query}」的结果"}

    result = {
        "ok": True,
        "query": query,
        "answer": (data.get("answer") or "")[:800],
        "results": results,
    }

    _search_cache[cache_key] = (time.time(), result)

    # 简单限长，避免缓存无限增长。
    if len(_search_cache) > 50:
        oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
        _search_cache.pop(oldest, None)

    return result


TOOL_HANDLERS = {
    "get_weather": lambda args: assistant_tools.get_weather(
        args, OWNER_OPEN_ID
    ),
    "list_todos": lambda args: assistant_tools.list_todos(
        args, OWNER_OPEN_ID
    ),
    "add_todo": _tool_add_todo,
    "propose_memory": _tool_propose_memory,
    "search_memories": lambda args: assistant_tools.search_memories(
        args, OWNER_OPEN_ID
    ),
    "web_search": assistant_tools.web_search,
}


def _run_tool(name, arguments):
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"未知工具：{name}"}

    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}

    try:
        result = handler(args)
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        print(f"[工具] {name} ok={str(ok).lower()}", flush=True)
        return result
    except Exception as exc:
        print(f"[工具] {name} 执行失败：{type(exc).__name__}", flush=True)
        return {"ok": False, "error": "工具执行失败"}


MAX_TOOL_ROUNDS = 4


def _chat_with_tools(messages, tools):
    """跑工具调用循环，返回最终回复和真实工具执行记录。"""
    working = list(messages)
    tool_runs = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = qwen_client.chat.completions.create(
            model=QWEN_MODEL,
            messages=working,
            tools=tools,
            tool_choice="auto",
            max_tokens=700,
        )

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            content = message.content
            reply = content.strip() if isinstance(content, str) else ""
            return reply, tool_runs

        # 把助手的 tool_calls 原样放回对话，再逐个回填结果。
        working.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in tool_calls
            ],
        })

        for call in tool_calls:
            result = _run_tool(
                call.function.name, call.function.arguments
            )
            tool_runs.append(
                agent_runtime.normalize_tool_run(
                    call.function.name, result
                )
            )
            payload = json.dumps(result, ensure_ascii=False)

            # 搜索结果可能很长，提示模型这是唯一事实来源。
            if call.function.name == "web_search":
                payload = (
                    "以下是本次搜索的全部事实来源，"
                    "回答只能基于这些内容：\n" + payload
                )

            working.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": payload,
            })

    # 工具轮数用完，最后要一次纯文本回答。
    final = qwen_client.chat.completions.create(
        model=QWEN_MODEL,
        messages=working
        + [{
            "role": "user",
            "content": "请基于上面的工具结果直接回答，不要再调用工具。",
        }],
        max_tokens=700,
    )
    content = final.choices[0].message.content
    reply = content.strip() if isinstance(content, str) else ""
    return reply, tool_runs


def _execute_planned_actions(actions):
    tool_runs = []
    for action in actions:
        arguments = json.dumps(
            action["arguments"], ensure_ascii=False
        )
        result = _run_tool(action["name"], arguments)
        tool_runs.append(
            agent_runtime.normalize_tool_run(action["name"], result)
        )
    return tool_runs


def _compose_or_fallback(user_text, tool_runs):
    try:
        return agent_runtime.compose_grounded_reply(
            qwen_client,
            QWEN_MODEL,
            user_text,
            tool_runs,
            datetime.now(LOCAL_TZ),
        )
    except Exception as exc:
        print(
            f"[动作回复] 校验失败，使用保底文案：{type(exc).__name__}",
            flush=True,
        )
        return agent_runtime.fallback_action_reply(tool_runs)


def generate_ai_reply(chat_id, user_text, turn_id=""):
    """使用 Qwen 回复普通聊天，并保存近期上下文。"""
    if qwen_client is None:
        return "😅 AI 还没配好"

    if len(user_text) > 4000:
        return "😅 消息太长了，4000 字以内"

    # 先读取历史，再关闭数据库连接，避免网络请求期间占用数据库。
    conn = open_connection(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT role, content
            FROM chat_messages
            WHERE owner_id = ? AND chat_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (OWNER_OPEN_ID, chat_id),
        ).fetchall()
    finally:
        conn.close()

    history = [
        {"role": role, "content": content}
        for role, content in reversed(rows)
    ]
    safe_history = agent_runtime.sanitize_history(history)
    session_id = (
        f"feishu:{datetime.now(LOCAL_TZ).date().isoformat()}:{chat_id}"
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Echo-Mate，用户的个人助手。"
                "你已连接共享待办和记忆。"
                f"当前北京时间是 {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}。"
                "用简体中文自然对话，像朋友聊天，简洁但有信息量。\n"
                "普通聊天只负责回答和只读查询；写操作由前置动作层执行。\n"
                "聊天历史只用于语境，不代表业务状态；"
                "只有在当前轮工具返回 ok=true 时才能声称操作完成。\n"
                "不要套固定格式，不要每次都列清单，"
                "不要加「0 返回」这类导航提示。\n\n"
                "你可以调用工具获取实时信息：\n"
                "- get_weather：天气、气温、下雨、穿衣、未来几天趋势\n"
                "- list_todos：用户有哪些待办\n"
                "- search_memories：翻找用户过去记过的事\n"
                "- web_search：联网查最新资料（新闻、价格、版本、赛事）\n"
                "需要数据时直接调用工具，然后基于结果自然回答；"
                "能对比、能建议、能解释原因就多说一句。\n"
                "用了 web_search 时，严格按下面的规则写：\n"
                "1. 只使用工具返回的 title / content / answer 里出现过的事实，"
                "不要添加任何工具结果里没有的新闻、公司名、金额或版本号。\n"
                "2. 来源链接只能从工具结果的 url 字段原样复制，"
                "禁止自己拼 URL 或改路径。\n"
                "3. 结果里信息不足就直说「搜到的资料有限」，不要补全。\n"
                "4. 如果多个结果说法冲突，指出冲突，不要挑一个当结论。\n"
                "5. 回答末尾附 1-3 个来源，格式：标题 - URL。\n"
                "工具返回 ok=false 时如实说明，不要编造数据。\n\n"
                "问天气时，如果用户说今天/明天/后天，"
                "调用 get_weather 并传对应 day_offset。"
                + assistant_actions.memory_prompt_block(
                    OWNER_OPEN_ID,
                    query=user_text,
                    session_id=session_id,
                )
            ),
        },
        *safe_history,
        {"role": "user", "content": user_text},
    ]

    now = datetime.now(LOCAL_TZ)
    tool_runs = []
    explicit_action = agent_runtime.parse_explicit_reminder(
        user_text, now
    )
    if explicit_action is not None:
        plan = {"actions": [explicit_action], "clarification": ""}
        print("[动作计划] 使用明确时间快速路径", flush=True)
    else:
        try:
            plan = agent_runtime.plan_actions(
                qwen_client,
                QWEN_MODEL,
                user_text,
                now,
            )
            print(
                f"[动作计划] actions={len(plan['actions'])}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[动作计划] 失败，异常类型={type(exc).__name__}",
                flush=True,
            )
            plan = {"actions": [], "clarification": ""}

    if plan["actions"]:
        tool_runs = _execute_planned_actions(plan["actions"])
        if all(run["ok"] for run in tool_runs):
            reply = _compose_or_fallback(user_text, tool_runs)
        else:
            reply = agent_runtime.fallback_action_reply(tool_runs)
    elif plan["clarification"]:
        reply = plan["clarification"]
    else:
        try:
            reply, tool_runs = _chat_with_tools(
                messages, READ_ONLY_TOOLS
            )
            if not reply:
                return "😅 这次没想出来，换个说法试试"

            if agent_runtime.looks_like_completion_claim(reply):
                reply = agent_runtime.false_action_reply()

        except Exception as exc:
            # 避免将请求内容或凭证写入日志。
            print(
                f"[Qwen] 请求失败，异常类型={type(exc).__name__}",
                flush=True,
            )
            return "😅 AI 卡了一下，稍后再试"

    # 只有成功获得回答后才保存本轮聊天。
    conn = open_connection(DB_PATH)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO chat_messages
                    (owner_id, chat_id, role, content, created_at_utc)
                VALUES (?, ?, 'user', ?, ?)
                """,
                (OWNER_OPEN_ID, chat_id, user_text, now_utc()),
            )
            conn.execute(
                """
                INSERT INTO chat_messages
                    (owner_id, chat_id, role, content, created_at_utc)
                VALUES (?, ?, 'assistant', ?, ?)
                """,
                (OWNER_OPEN_ID, chat_id, reply, now_utc()),
            )

            # 每个聊天只保留最近 12 条消息。
            conn.execute(
                """
                DELETE FROM chat_messages
                WHERE owner_id = ? AND chat_id = ?
                  AND id NOT IN (
                    SELECT id
                    FROM chat_messages
                    WHERE owner_id = ? AND chat_id = ?
                    ORDER BY id DESC
                    LIMIT 12
                  )
                """,
                (
                    OWNER_OPEN_ID, chat_id,
                    OWNER_OPEN_ID, chat_id,
                ),
            )
    finally:
        conn.close()

    if (
        memory_observer is not None
        and not any(
            run.get("tool") == "propose_memory" for run in tool_runs
        )
    ):
        memory_observer.enqueue_turn(
            owner_id=OWNER_OPEN_ID,
            session_id=session_id,
            turn_id=turn_id or now_utc(),
            source="feishu",
            user_text=user_text,
            tool_runs=tool_runs,
        )

    print("[回复] 已生成", flush=True)
    return reply


def send_text(receive_id, text, receive_id_type="chat_id"):
    return feishu_transport.send_text(
        api_client, receive_id, text, receive_id_type
    )


def send_card(receive_id, card, receive_id_type="chat_id"):
    return feishu_transport.send_card(
        api_client, receive_id, card, receive_id_type
    )


def send_page_card(chat_id, page, text):
    if send_card(chat_id, build_page_card(page, text)):
        return True
    return send_text(chat_id, text)


# ==================== 到期提醒线程 ====================

def reminder_loop():
    print("[待办] 飞书提醒投递线程已启动，检查间隔 15 秒", flush=True)

    while True:
        try:
            delivery = echomate_store.claim_delivery("feishu")

            if delivery:
                todo_id = delivery["todo_id"]
                title = delivery["title"]
                chat_id = delivery["chat_id"]
                delivery_id = delivery["delivery_id"]

                if not chat_id:
                    # A deskbot-created todo has no Feishu chat target.
                    # Keep the shared record, but this channel cannot deliver it.
                    echomate_store.mark_delivery(
                        delivery_id, "skipped", "no Feishu chat_id"
                    )
                    echomate_store.finalize_todo_if_delivered(todo_id)
                else:
                    due_display = format_due_friendly(delivery["due_at_utc"])
                    content = (
                        f"⏰ {title}\n\n"
                        f"{due_display}\n\n"
                        "完成后打开待办清单操作"
                    )
                    try:
                        sent = send_text(chat_id, content)
                    except Exception as exc:
                        print(
                            f"[待办] #{todo_id} 发送异常：{type(exc).__name__}",
                            flush=True,
                        )
                        sent = False

                    if sent:
                        echomate_store.mark_delivery(delivery_id, "sent")
                        echomate_store.finalize_todo_if_delivered(todo_id)
                    else:
                        echomate_store.mark_delivery(
                            delivery_id, "failed", "Feishu send failed"
                        )

            time.sleep(15)

        except Exception as exc:
            print(
                f"[待办] 调度异常：{type(exc).__name__}",
                flush=True,
            )
            time.sleep(5)

def on_message(data: lark.im.v1.P2ImMessageReceiveV1):
    header = getattr(data, "header", None)
    event = getattr(data, "event", None)

    if event is None:
        return

    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)

    if message is None or sender is None:
        return

    # 私人记忆和待办仅在单聊中处理。
    if getattr(message, "chat_type", "") != "p2p":
        return

    if getattr(sender, "sender_type", "") != "user":
        return

    sender_id = getattr(sender, "sender_id", None)
    open_id = getattr(sender_id, "open_id", "")

    if open_id != OWNER_OPEN_ID:
        print("[安全] 已忽略非授权用户消息", flush=True)
        return

    if getattr(message, "message_type", "") != "text":
        return

    try:
        payload = json.loads(message.content or "{}")
        user_text = str(payload.get("text", "")).strip()
    except (json.JSONDecodeError, TypeError):
        return

    if not user_text:
        return

    event_id = getattr(header, "event_id", None)
    event_id = event_id or getattr(message, "message_id", None)

    if not event_id:
        print("[飞书] 事件缺少唯一 ID，已忽略", flush=True)
        return

    CURRENT_CHAT_ID.set(message.chat_id)
    reply = None
    reply_page = None

    try:
        with sqlite_connect(DB_PATH, write=True) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO processed_events
                    (event_id, processed_at)
                VALUES (?, ?)
                """,
                (event_id, now_utc()),
            )

            if cursor.rowcount == 0:
                print("[飞书] 重复事件已忽略", flush=True)
                return

        with sqlite_connect(DB_PATH, write=True) as conn:
            reply = handle_command(
                conn,
                user_text,
                message.chat_id,
                event_id=event_id,
            )
            if reply is not None:
                reply_page = get_page(message.chat_id, conn=conn)

    except Exception as exc:
        print(
            f"[数据库] 处理失败：{type(exc).__name__}",
            flush=True,
        )
        reply = "😅 这条消息保存失败，请稍后重试。\n\n不会重复创建，直接重发即可。"
    if reply is None:
        match = re.match(
            r"^(?:记住|保存记忆)(?:\s*[:：]\s*|\s+)(.+)$",
            user_text,
            re.DOTALL,
        )
        if match:
            candidate_id, result = memory_store.enqueue_candidate(
                OWNER_OPEN_ID, match.group(1).strip()
            )
            if result == "created":
                reply = (
                    "📝 已生成记忆候选\n"
                    "回复：记忆候选\n"
                    "查看并确认"
                )
            elif result == "duplicate":
                reply = "已有相同候选记忆\n回复：记忆候选"
            elif result == "too_long":
                reply = "😅 太长了，2000 字以内"
            else:
                reply = "😅 要记的内容是空的"

    if reply is None:
        reply = generate_ai_reply(
            message.chat_id,
            user_text,
            turn_id=event_id,
        )

    if reply:
        try:
            if reply_page:
                send_page_card(message.chat_id, reply_page, reply)
            else:
                send_text(message.chat_id, reply)
        except Exception as exc:
            print(
                f"[飞书] 回复异常：{type(exc).__name__}",
                flush=True,
            )


# ==================== 启动 ====================

def main():
    echomate_store.DB_PATH = DB_PATH
    notify_store.DB_PATH = DB_PATH
    init_db()
    init_chat_table()
    init_core_schema()
    pc_control_store.init_schema()
    pc_control_store.pause_all_active_sessions()

    threading.Thread(
        target=reminder_loop,
        name="todo-reminder",
        daemon=True,
    ).start()

    pc_control_service.start_worker(send_text)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
    )

    print("[记忆助手] SQLite 初始化完成", flush=True)
    print("[记忆助手] 记忆和待办仅允许授权账号私聊操作", flush=True)
    print(f"[记忆助手] 数据库位置：{DB_PATH}", flush=True)
    print("[飞书] 正在连接开放平台……", flush=True)

    client.start()


if __name__ == "__main__":
    main()
