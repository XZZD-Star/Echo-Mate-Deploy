import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
import psutil
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import lark_oapi as lark
from dotenv import load_dotenv, dotenv_values
from urllib.parse import urlparse
from openai import OpenAI

# 主动播报队列：与 AIChat Server 共用同一份 SQLite，纯标准库实现。
sys.path.insert(0, "/root/echomate-deploy/app/Demo/AIChat_demo/Server")
from tools import notify_store


# ==================== 配置 ====================

os.umask(0o077)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

APP_ID = (os.getenv("LARK_APP_ID") or "").strip()
APP_SECRET = (os.getenv("LARK_APP_SECRET") or "").strip()
OWNER_OPEN_ID = (os.getenv("LARK_OWNER_OPEN_ID") or "").strip()

DB_PATH = Path("/var/lib/echomate/assistant.db")
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

if not APP_ID or not APP_SECRET:
    raise SystemExit("错误：App ID 或 App Secret 未配置")

if not OWNER_OPEN_ID.startswith("ou_"):
    raise SystemExit("错误：LARK_OWNER_OPEN_ID 未正确配置")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
os.chmod(DB_PATH.parent, 0o700)


@contextmanager
def db_conn():
    """连接用完必须关闭，否则 WAL 下会累积句柄并持有写锁。"""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_owner_id
                ON memories(owner_id, id);

            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL,
                due_at_utc TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at_utc TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                reminded_at_utc TEXT,
                last_attempt_at_utc TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_todos_due
                ON todos(status, due_at_utc, reminded_at_utc);
        """)

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).isoformat(timespec="seconds")

        conn.execute(
            "DELETE FROM processed_events WHERE processed_at < ?",
            (cutoff,),
        )
        conn.commit()
    finally:
        conn.close()

    if DB_PATH.exists():
        os.chmod(DB_PATH, 0o600)


# ==================== 记忆与待办命令 ====================

def init_chat_table():
    """建立独立聊天历史表，不修改已有记忆和待办数据。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
    "memory", "help", "notify", "quiet", "records",
}

# 0 返回上一级；未列出的页面回到主菜单。
PAGE_PARENT = {
    "status_detail": "status",
    "quiet": "notify",
    "records": "notify",
}

SERVICE_UNITS = [
    ("echomate-server.service", "语音", "echomate-server"),
    ("echomate-feishu-bot.service", "飞书", "echomate-feishu-bot"),
    ("echomate-monitor.service", "监控", "echomate-monitor"),
]

PAGE_TTL_SECONDS = 86400


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


def get_page(chat_id, conn=None):
    """读取当前页面；超过 24 小时未活动则回到主菜单。"""
    raw = _read_setting(conn, page_key(chat_id), "")
    if not raw:
        return "menu"

    try:
        data = json.loads(raw)
        page = data.get("page", "menu")
        stamp = datetime.fromisoformat(data["at"])
    except (ValueError, KeyError, TypeError):
        return "menu"

    if page not in PAGES:
        return "menu"

    if (
        datetime.now(timezone.utc) - stamp
    ).total_seconds() > PAGE_TTL_SECONDS:
        return "menu"

    return page


def set_page(chat_id, page, conn=None):
    if page not in PAGES:
        page = "menu"
    _write_setting(
        conn,
        page_key(chat_id),
        json.dumps({"page": page, "at": now_utc()}),
    )


def _service_states():
    """返回 [(中文标签, systemd 状态, unit 简名), ...]。"""
    result = []
    for unit, label, short in SERVICE_UNITS:
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            state = (proc.stdout or "").strip() or "unknown"
        except (OSError, subprocess.TimeoutExpired):
            state = "unknown"
        result.append((label, state, short))
    return result


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
        "🐣 Echo\n\n"
        "1 · 📊 状态\n"
        "2 · 📋 待办\n"
        "3 · ➕ 新待办\n"
        "4 · 📚 记忆\n"
        "5 · 💡 帮助\n"
        "6 · 🔊 播报\n\n"
        "直接发数字就行"
    )


def render_status():
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    states = _service_states()
    down = [label for label, state, _ in states if state != "active"]

    if down:
        head = "🟡 有服务掉线"
    elif cpu > 90 or mem.available < 250 * 1024**2:
        head = "🟡 有点忙"
    else:
        head = "📊 一切正常"

    service_line = " · ".join(
        f"{label} {'🟢' if state == 'active' else '🔴'}"
        for label, state, _ in states
    )

    lines = [
        head,
        "",
        f"CPU {cpu:.0f}% · 内存 {mem.available / 1024**2:.0f}M"
        f" · 磁盘 {disk.free / 1024**3:.0f}G",
        f"已跑 {_uptime_text()} · 开机 {_boot_text()}",
        "",
        service_line,
    ]

    if down:
        lines.append("掉线：" + "、".join(down))

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

    lines = [
        "🔍 状态详情",
        "",
        f"🖥 {os.uname().nodename}",
        f"⏱ 开机 {boot:%Y-%m-%d %H:%M:%S}",
        f"📈 已跑 {_uptime_text()}",
        "",
        f"CPU {cpu:.0f}% · {psutil.cpu_count()} 核",
        f"内存 {mem.used / 1024**2:.0f}M / {mem.total / 1024**3:.1f}G",
        f"Swap {swap.used / 1024**2:.0f}M / {swap.total / 1024**3:.1f}G",
        f"磁盘 {disk.used / 1024**3:.0f}G / {disk.total / 1024**3:.0f}G",
        "",
    ]

    for label, state, short in states:
        icon = "🟢" if state == "active" else "🔴"
        lines.append(f"{label} {icon} {state} · {short}")

    lines.extend(["", "1 刷新 · 0 返回"])
    return "\n".join(lines)


def _todo_rows(conn, limit=20):
    return conn.execute(
        """
        SELECT id, title, due_at_utc
        FROM todos
        WHERE owner_id = ? AND status = 'pending'
        ORDER BY due_at_utc ASC
        LIMIT ?
        """,
        (OWNER_OPEN_ID, limit),
    ).fetchall()


def render_todo(conn):
    rows = _todo_rows(conn)

    if not rows:
        return (
            "📋 今天很轻松\n\n"
            "没有待办，去歇会儿吧\n\n"
            "3 新建 · 0 返回"
        )

    lines = [f"📋 你有 {len(rows)} 件事", ""]
    for todo_id, title, due_utc in rows:
        lines.append(
            f"#{todo_id} · {format_due_friendly(due_utc)} · {title}"
        )

    first_id = rows[0][0]
    lines.extend([
        "",
        f"完成 → 完成待办 {first_id}",
        "3 新建 · 0 返回",
    ])
    return "\n".join(lines)


def render_todo_new():
    return (
        "➕ 建个待办\n\n"
        "照着发：\n"
        "添加待办 2026-10-26 09:30 交周报\n\n"
        "时间用 24 小时制，北京时间\n\n"
        "0 返回"
    )


def _memory_rows(conn, limit):
    return conn.execute(
        """
        SELECT id, content
        FROM memories
        WHERE owner_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (OWNER_OPEN_ID, limit),
    ).fetchall()


def render_memory(conn, show_all=False):
    total = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
        (OWNER_OPEN_ID,),
    ).fetchone()[0]

    if not total:
        return (
            "📚 记忆 · 0 条\n\n"
            "还没有记忆\n\n"
            "存：记住：喜欢简短回答\n\n"
            "0 返回"
        )

    rows = _memory_rows(conn, 30 if show_all else 3)

    lines = [f"📚 记忆 · {total} 条", ""]
    for memory_id, content in rows:
        lines.append(f"#{memory_id} · {content[:60]}")

    if not show_all and total > len(rows):
        lines.append(f"…还有 {total - len(rows)} 条")

    lines.extend([
        "",
        "查：查记忆 回答",
        "存：记住：喜欢简短回答",
    ])

    if show_all:
        lines.extend(["", "0 返回"])
    else:
        lines.extend(["", "1 全部 · 0 返回"])

    return "\n".join(lines)


def render_help():
    return (
        "💡 Echo 会这些\n\n"
        "📋 待办\n"
        "添加待办 2026-10-26 09:30 交周报\n"
        "我的待办 · 完成待办 6\n\n"
        "📚 记忆\n"
        "存：记住：喜欢简短回答\n"
        "查：查记忆 回答\n\n"
        "🔊 播报\n"
        "静音 23:00 08:00 · 关闭静音\n\n"
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
        f"📋 待办播报　{'开启 ●' if todo_on else '未开启'}",
        f"📡 监控播报　{'开启 ●' if monitor_on else '未开启'}",
        f"📤 待播通知　{pending} 条",
        "",
        "⚙️ 操作",
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


def render_records():
    rows = notify_store.recent_records(10)

    if not rows:
        return "📢 播报记录\n\n还没有记录\n\n0 返回"

    status_label = {
        "pending": "待播",
        "claimed": "播放中",
        "sent": "已播",
        "failed": "失败",
        "expired": "过期",
    }

    lines = ["📢 播报记录", ""]
    for row in rows:
        status = status_label.get(row["status"], row["status"])
        if row["status"] == "sent":
            mark = f"{status} {'✓' if row['acked_at_utc'] else '·'}"
        elif row["last_error"]:
            mark = f"{status} · {row['last_error'][:20]}"
        else:
            mark = status
        lines.append(f"#{row['id']} · {_kind_label(row['kind'])} · {mark}")

    lines.extend(["", "0 返回"])
    return "\n".join(lines)


def render_page(conn, chat_id, page):
    if page == "menu":
        return menu_text()
    if page == "status":
        return render_status()
    if page == "status_detail":
        return render_status_detail()
    if page == "todo":
        return render_todo(conn)
    if page == "todo_new":
        return render_todo_new()
    if page == "memory":
        return render_memory(conn)
    if page == "help":
        return render_help()
    if page == "notify":
        return render_notify()
    if page == "quiet":
        return render_quiet()
    if page == "records":
        return render_records()
    return menu_text()


def _normalize_hhmm(value):
    """把 9:05 这类写法规范成 09:05，非法返回 None。"""
    try:
        hour, minute = value.split(":", 1)
        hour = int(hour)
        minute = int(minute)
    except (TypeError, ValueError):
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    return f"{hour:02d}:{minute:02d}"


def _kind_label(kind):
    return {
        "todo": "待办",
        "alert": "告警",
        "recovery": "恢复",
        "manual": "测试",
    }.get(kind, kind)


def _enter(conn, chat_id, page):
    set_page(chat_id, page, conn=conn)
    return render_page(conn, chat_id, page)


def _toggle(kind_key, target):
    current = notify_store.get_setting(kind_key, "1")
    new_value = "0" if current == "1" else "1"
    notify_store.set_setting(kind_key, new_value)
    state_text = "关闭" if new_value == "0" else "开启"
    return f"✅ {target}播报{state_text}\n\n6 看播报 · 0 返回"


def _handle_page_number(conn, chat_id, page, text):
    """页面内的数字指令。返回 None 表示不归本页处理。"""
    if page == "menu":
        target = {
            "1": "status",
            "2": "todo",
            "3": "todo_new",
            "4": "memory",
            "5": "help",
            "6": "notify",
        }.get(text)
        if target:
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

    elif page == "memory":
        if text == "1":
            return render_memory(conn, show_all=True)

    elif page == "notify":
        if text == "1":
            return _enter(conn, chat_id, "quiet")
        if text == "2":
            return _toggle("notify_todo_enabled", "待办")
        if text == "3":
            return _toggle("notify_monitor_enabled", "监控")
        if text == "4":
            return _enter(conn, chat_id, "records")

    return None


def _add_todo(conn, chat_id, date_text, time_text, title):
    """创建待办，供严格格式与自然输入共用。"""
    title = (title or "").strip()

    if not title:
        return "😅 待办得写点内容\n\n3 重填 · 0 返回"

    if len(title) > 200:
        return "😅 标题太长，200 字以内\n\n3 重填 · 0 返回"

    try:
        due_local = datetime.strptime(
            f"{date_text} {time_text}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return "😅 日期写成 2026-10-26 09:30 这样\n\n3 重填 · 0 返回"

    if due_local <= datetime.now(LOCAL_TZ):
        return "😅 这个时间已经过去了\n\n3 重填 · 0 返回"

    due_utc = due_local.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )

    cursor = conn.execute(
        """
        INSERT INTO todos
            (owner_id, title, due_at_utc, timezone,
             status, created_at_utc, chat_id)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            OWNER_OPEN_ID,
            title,
            due_utc,
            "Asia/Shanghai",
            now_utc(),
            chat_id,
        ),
    )

    return (
        f"✅ 记下了 #{cursor.lastrowid}\n\n"
        f"{title} · {format_due_friendly(due_utc)}\n"
        "到点我会提醒你\n\n"
        "2 看待办 · 0 返回"
    )


def _parse_natural_todo(text, allow_time_only=False):
    """解析自然输入，返回 (日期, 时间, 标题) 或 None。

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
                target = target + timedelta(days=1)
            return (
                target.strftime("%Y-%m-%d"),
                f"{int(hour):02d}:{minute}",
                title.strip(),
            )

    return None


def _looks_like_todo(text):
    """判断消息是否明显在说待办，用于拦截 AI。"""
    raw = (text or "").strip()

    if any(
        word in raw
        for word in ("待办", "提醒我", "提醒一下", "记得提醒")
    ):
        return True

    if _parse_natural_todo(raw) is not None:
        return True

    return False


def todo_hint_reply(text):
    """看起来像待办但没解析成功时，给格式引导而不是交给 AI。"""
    if not _looks_like_todo(text):
        return None

    return (
        "😅 这条没接上\n\n"
        "新待办这样发：\n"
        "添加待办 2026-10-26 09:30 交周报\n\n"
        "也可以直接发：\n"
        "2026-10-26 09:30 交周报\n\n"
        "3 新建 · 0 返回"
    )


def handle_command(conn, text, chat_id):
    text = text.strip()
    page = get_page(chat_id, conn=conn)

    # -------- 全局导航 --------

    if text in {"菜单", "功能"}:
        return _enter(conn, chat_id, "menu")

    if text == "0":
        parent = PAGE_PARENT.get(page, "menu")
        return _enter(conn, chat_id, parent)

    # -------- 页面内数字 --------

    if text in {"1", "2", "3", "4", "5", "6"}:
        handled = _handle_page_number(conn, chat_id, page, text)
        if handled is not None:
            return handled

        # 本页没有这个数字，但主菜单有：直接跳到主菜单对应页面。
        target = {
            "1": "status",
            "2": "todo",
            "3": "todo_new",
            "4": "memory",
            "5": "help",
            "6": "notify",
        }.get(text)

        if target and page != "menu":
            return _enter(conn, chat_id, target)

    # -------- 直接进入页面的文字命令 --------

    if text in {
        "检查 VPS", "检查VPS", "VPS 状态", "VPS状态",
        "服务器状态", "状态",
    }:
        return _enter(conn, chat_id, "status")

    if text in {"状态详情", "详情"}:
        return _enter(conn, chat_id, "status_detail")

    if text == "帮助":
        return _enter(conn, chat_id, "help")

    if text in {"播报设置", "语音播报设置"}:
        return _enter(conn, chat_id, "notify")

    if text in {"播报记录", "语音播报记录"}:
        return _enter(conn, chat_id, "records")

    if text in {"我的待办", "待办列表", "查看待办", "待办"}:
        return _enter(conn, chat_id, "todo")

    if text in {"列出记忆", "我的记忆", "记忆列表", "记忆"}:
        return _enter(conn, chat_id, "memory")

    if text in {"添加待办", "新建待办"}:
        return _enter(conn, chat_id, "todo_new")

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
            return "😅 时间写成 23:00 08:00 这样\n\n6 看播报 · 0 返回"

        if start == end:
            return "😅 开始和结束不能一样\n\n6 看播报 · 0 返回"

        notify_store.set_setting("notify_quiet_start", start)
        notify_store.set_setting("notify_quiet_end", end)

        return (
            f"🔇 静音 {start}–{end}\n\n"
            "这段桌宠闭嘴，飞书照常\n\n"
            "6 看播报 · 0 返回"
        )

    if text in {"关闭静音", "取消静音"}:
        notify_store.set_setting("notify_quiet_start", "")
        notify_store.set_setting("notify_quiet_end", "")
        return "🔔 静音关掉了\n\n6 看播报 · 0 返回"

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
        return f"✅ {target}播报{state_text}\n\n6 看播报 · 0 返回"

    # -------- 创建待办 --------

    if text.startswith(("添加待办", "新建待办")):
        # 先尝试宽松解析：前缀 + 日期 + 任意冒号/点号 + 事项
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

        if not re.match(
            r"^添加待办\s+\d{4}-\d{2}-\d{2}\s+"
            r"\d{2}:\d{2}\s+.+$",
            text,
            re.DOTALL,
        ):
            return (
                "😅 格式差一点\n\n"
                "添加待办 2026-10-26 09:30 交周报\n\n"
                "3 重填 · 0 返回"
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
            return (
                f"😅 没有 #{todo_id} 这个待办\n\n"
                "发 2 看看有哪些\n\n"
                "2 看待办 · 0 返回"
            )

        if action == "取消待办":
            return f"✅ #{todo_id} 取消\n\n2 看待办 · 0 返回"

        remaining = conn.execute(
            """
            SELECT COUNT(*) FROM todos
            WHERE owner_id = ? AND status = 'pending'
            """,
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not remaining:
            return (
                "🎉 全部完成\n\n"
                "今天没有待办\n\n"
                "3 新建 · 0 返回"
            )

        return (
            f"✅ #{todo_id} 完成\n\n"
            f"还有 {remaining} 件事，加油 💪\n\n"
            "2 看待办 · 0 返回"
        )

    # -------- 保存记忆 --------

    match = re.match(
        r"^(?:记住|保存记忆)\s*[:：]\s*(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        content = match.group(1).strip()

        if not content:
            return "😅 要记的内容是空的\n\n4 看记忆 · 0 返回"

        if len(content) > 2000:
            return "😅 太长了，2000 字以内\n\n4 看记忆 · 0 返回"

        cursor = conn.execute(
            """
            INSERT INTO memories (owner_id, content, created_at)
            VALUES (?, ?, ?)
            """,
            (OWNER_OPEN_ID, content, now_utc()),
        )

        return (
            f"✅ 记下了 #{cursor.lastrowid}\n\n"
            f"{content[:150]}\n\n"
            "4 看记忆 · 0 返回"
        )

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
        for memory_id, content in rows:
            lines.append(f"#{memory_id} · {content[:120]}")
        lines.extend(["", "4 看记忆 · 0 返回"])
        return "\n".join(lines)

    # -------- 删除记忆 --------

    match = re.match(r"^删除记忆\s*[:：#]?\s*(\d+)\s*$", text)

    if match:
        memory_id = int(match.group(1))
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, OWNER_OPEN_ID),
        )

        if cursor.rowcount:
            return f"✅ #{memory_id} 删掉了\n\n4 看记忆 · 0 返回"

        return f"😅 没有 #{memory_id}\n\n4 看记忆 · 0 返回"

    # -------- 自然输入：日期/今天明天 + 时间 + 事项 --------

    parsed = _parse_natural_todo(
        text, allow_time_only=page in {"todo", "todo_new"}
    )
    if parsed is not None:
        date_text, time_text, title = parsed
        return _add_todo(conn, chat_id, date_text, time_text, title)

    return None


# ==================== 飞书消息发送 ====================

api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .build()
)



# ==================== Qwen 普通聊天 ====================

SERVER_ENV = Path(
    "/root/echomate-deploy/app/Demo/AIChat_demo/Server/.env"
)
server_cfg = dotenv_values(SERVER_ENV)

QWEN_API_KEY = (server_cfg.get("DASHSCOPE_API_KEY") or "").strip()
QWEN_MODEL = (server_cfg.get("CHAT_MODEL") or "qwen-max").strip()

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


def generate_ai_reply(chat_id, user_text):
    """使用 Qwen 回复普通聊天，并保存近期上下文。"""
    if qwen_client is None:
        return "😅 AI 还没配好"

    if len(user_text) > 4000:
        return "😅 消息太长了，4000 字以内"

    # 先读取历史，再关闭数据库连接，避免网络请求期间占用数据库。
    conn = sqlite3.connect(DB_PATH, timeout=10)
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

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Echo-Mate Personal Assistant，"
                "请使用简体中文回答，表达自然、清晰、简洁。\n\n"
                "重要限制：你没有添加、查询、修改、删除待办或记忆的"
                "能力，也没有读取数据库的能力。\n"
                "当用户想管理待办或记忆时，绝对不要假装已经完成，"
                "不要编造任何待办列表、编号或记忆内容。\n"
                "只回复一句引导语：\n"
                "- 添加待办：添加待办 2026-10-26 09:30 事项\n"
                "- 查看待办：我的待办\n"
                "- 保存记忆：记住：内容\n"
                "- 查看记忆：列出记忆\n"
                "你不能声称已经完成任何实际未执行的操作。"
            ),
        },
        *history,
        {"role": "user", "content": user_text},
    ]

    try:
        response = qwen_client.chat.completions.create(
            model=QWEN_MODEL,
            messages=messages,
            max_tokens=700,
        )

        reply = response.choices[0].message.content
        if not isinstance(reply, str) or not reply.strip():
            return "😅 这次没想出来，换个说法试试"

        reply = reply.strip()

    except Exception as exc:
        # 避免将请求内容或凭证写入日志。
        print(
            f"[Qwen] 请求失败，异常类型={type(exc).__name__}",
            flush=True,
        )
        return "😅 AI 卡了一下，稍后再试"

    # 只有成功获得回答后才保存本轮聊天。
    conn = sqlite3.connect(DB_PATH, timeout=10)
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

    print("[Qwen] 普通聊天回复成功", flush=True)
    return reply


def send_text(receive_id, text, receive_id_type="chat_id"):
    request = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )

    response = api_client.im.v1.message.create(request)

    if response.success():
        print("[飞书] 消息发送成功", flush=True)
        return True

    print(
        f"[飞书] 消息发送失败：code={response.code}, "
        f"msg={response.msg}, log_id={response.get_log_id()}",
        flush=True,
    )
    return False


# ==================== 到期提醒线程 ====================

def reminder_loop():
    print("[待办] 到期提醒调度器已启动，检查间隔 15 秒", flush=True)

    while True:
        try:
            current_time = now_utc()
            retry_before = (
                datetime.now(timezone.utc) - timedelta(seconds=60)
            ).isoformat(timespec="seconds")

            with db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT id, title, due_at_utc, chat_id
                    FROM todos
                    WHERE status = 'pending'
                      AND reminded_at_utc IS NULL
                      AND due_at_utc <= ?
                      AND (
                        last_attempt_at_utc IS NULL
                        OR last_attempt_at_utc <= ?
                      )
                    ORDER BY due_at_utc
                    LIMIT 20
                    """,
                    (current_time, retry_before),
                ).fetchall()

            for todo_id, title, due_utc, chat_id in rows:
                attempt_time = now_utc()

                # 先记录尝试时间，避免发送失败时每 15 秒刷屏。
                with db_conn() as conn:
                    cursor = conn.execute(
                        """
                        UPDATE todos
                        SET last_attempt_at_utc = ?
                        WHERE id = ?
                          AND status = 'pending'
                          AND reminded_at_utc IS NULL
                          AND (
                            last_attempt_at_utc IS NULL
                            OR last_attempt_at_utc <= ?
                          )
                        """,
                        (attempt_time, todo_id, retry_before),
                    )

                if cursor.rowcount == 0:
                    continue

                due_display = format_due_friendly(due_utc)
                content = (
                    f"⏰ {title}\n\n"
                    f"{due_display} · #{todo_id}\n"
                    f"完成 → 完成待办 {todo_id}"
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
                    with db_conn() as conn:
                        conn.execute(
                            """
                            UPDATE todos
                            SET reminded_at_utc = ?
                            WHERE id = ?
                              AND status = 'pending'
                              AND reminded_at_utc IS NULL
                            """,
                            (now_utc(), todo_id),
                        )

                    # 飞书提醒成功后，同步排入桌宠播报队列。
                    # 失败不影响飞书，也不阻塞调度器。
                    try:
                        notify_store.enqueue(
                            kind="todo",
                            text=f"待办提醒：{title}",
                            dedupe_key=f"todo:{todo_id}",
                            priority=5,
                            ttl_minutes=120,
                        )
                    except Exception as exc:
                        print(
                            f"[待办] #{todo_id} 入播报队列失败："
                            f"{type(exc).__name__}",
                            flush=True,
                        )
                else:
                    print(
                        f"[待办] #{todo_id} 提醒发送失败，稍后重试",
                        flush=True,
                    )

        except Exception as exc:
            print(
                f"[待办] 调度器异常：{type(exc).__name__}",
                flush=True,
            )

        time.sleep(15)


# ==================== 飞书事件处理 ====================

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

    conn = sqlite3.connect(DB_PATH, timeout=10)
    reply = None

    try:
        with conn:
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

            reply = handle_command(conn, user_text, message.chat_id)

    except Exception as exc:
        print(
            f"[数据库] 处理失败：{type(exc).__name__}",
            flush=True,
        )
        return
    finally:
        conn.close()

    if reply is None:
        reply = todo_hint_reply(user_text)

    if reply is None:
        reply = generate_ai_reply(message.chat_id, user_text)

    if reply:
        try:
            send_text(message.chat_id, reply)
        except Exception as exc:
            print(
                f"[飞书] 回复异常：{type(exc).__name__}",
                flush=True,
            )


# ==================== 启动 ====================

init_db()
init_chat_table()

threading.Thread(
    target=reminder_loop,
    name="todo-reminder",
    daemon=True,
).start()

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
