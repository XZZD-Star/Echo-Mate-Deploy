import json
import os
import re
import sqlite3
import subprocess
import sys
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


def menu_text():
    return (
        "Echo\n\n"
        "1 · 状态\n"
        "2 · 待办\n"
        "3 · 新待办\n"
        "4 · 记忆\n"
        "5 · 帮助\n"
        "6 · 播报"
    )


def help_text():
    return (
        "Echo 能做什么\n\n"
        "添加待办 2026-10-25 15:00 检查 VPS\n"
        "记住：xxx　查记忆 xxx\n"
        "静音 23:00 08:00\n\n"
        "发 6 看播报，发 1 看状态"
    )



def vps_status_text():
    """采集 VPS 实时资源与关键服务状态。"""
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")

    current = datetime.now(LOCAL_TZ)
    boot = datetime.fromtimestamp(
        psutil.boot_time(), tz=timezone.utc
    ).astimezone(LOCAL_TZ)

    uptime = timedelta(
        seconds=max(0, int(time.time() - psutil.boot_time()))
    )
    uptime_text = (
        f"{uptime.days} 天"
        if uptime.days
        else f"{uptime.seconds // 3600} 小时"
    )

    services = [
        ("echomate-server.service", "语音"),
        ("echomate-feishu-bot.service", "飞书"),
        ("echomate-monitor.service", "监控"),
    ]

    down = []
    for unit, label in services:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            active = result.stdout.strip() == "active"
        except (OSError, subprocess.TimeoutExpired):
            active = False

        if not active:
            down.append(label)

    if down:
        head = "🟡 有服务掉线"
    elif cpu > 90 or mem.available < 250 * 1024**2:
        head = "🟡 有点忙"
    else:
        head = "🟢 一切正常"

    lines = [
        head,
        "",
        f"CPU {cpu:.0f}% · 内存 {mem.available / 1024**2:.0f}M"
        f" · 磁盘 {disk.free / 1024**3:.0f}G",
        f"运行 {uptime_text} · {len(services) - len(down)}/{len(services)} 服务在线",
    ]

    if down:
        lines.append("掉线：" + "、".join(down))

    return "\n".join(lines)



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


def notify_settings_text():
    todo_on = notify_store.get_setting("notify_todo_enabled", "1") == "1"
    monitor_on = (
        notify_store.get_setting("notify_monitor_enabled", "1") == "1"
    )

    quiet = notify_store.quiet_text().replace("（北京时间）", "")
    quiet_line = (
        "静音 未启用" if quiet == "未启用" else f"静音 {quiet}"
    )

    lines = [
        "🔊 播报",
        "",
        quiet_line,
        f"待办 {'开' if todo_on else '关'}"
        f" · 监控 {'开' if monitor_on else '关'}",
    ]

    pending = notify_store.pending_count()
    if pending:
        lines.append(f"待播 {pending} 条")

    lines.extend([
        "",
        "静音 23:00 08:00",
        "关闭静音 · 播报记录",
    ])

    return "\n".join(lines)


def notify_records_text(limit=10):
    rows = notify_store.recent_records(limit)

    if not rows:
        return "目前还没有主动播报记录。"

    status_label = {
        "pending": "待播",
        "claimed": "播放中",
        "sent": "已播",
        "failed": "失败",
        "expired": "过期",
    }

    lines = [f"📢 最近 {len(rows)} 条"]

    for row in rows:
        status = status_label.get(row["status"], row["status"])

        if row["status"] == "sent":
            mark = f"{status} {'✓' if row['acked_at_utc'] else '·'}"
        elif row["last_error"]:
            mark = f"{status} · {row['last_error'][:24]}"
        else:
            mark = status

        lines.append(
            f"\n#{row['id']} · {_kind_label(row['kind'])} · {mark}"
            f"\n{row['text'][:50]}"
        )

    return "\n".join(lines)


def handle_command(conn, text, chat_id):
    # Exact menu command mapping
    text = {
        "1": "检查 VPS",
        "2": "我的待办",
        "3": "添加待办",
        "4": "列出记忆",
        "5": "帮助",
        "6": "播报设置",
    }.get(text, text)
    text = text.strip()

    if text in {"检查 VPS", "检查VPS", "VPS 状态", "VPS状态", "服务器状态"}:
        return vps_status_text()

    if text in {"菜单", "功能"}:
        return menu_text()

    if text == "帮助":
        return help_text()

    # -------- 主动播报设置（P3） --------

    if text in {"播报设置", "语音播报设置"}:
        return notify_settings_text()

    match = re.match(
        r"^静音\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s*$",
        text,
    )

    if match:
        start_text, end_text = match.groups()
        start = _normalize_hhmm(start_text)
        end = _normalize_hhmm(end_text)

        if start is None or end is None:
            return "😅 时间写成 23:00 08:00 这样"

        if start == end:
            return "😅 开始和结束不能一样"

        notify_store.set_setting("notify_quiet_start", start)
        notify_store.set_setting("notify_quiet_end", end)

        return (
            f"🔇 静音 {start}–{end}\n\n"
            "这段桌宠闭嘴，飞书照常"
        )

    if text in {"关闭静音", "取消静音"}:
        notify_store.set_setting("notify_quiet_start", "")
        notify_store.set_setting("notify_quiet_end", "")
        return "🔔 静音关掉了"

    match = re.match(
        r"^(关闭|开启)(待办|监控)播报$",
        text,
    )

    if match:
        action, target = match.groups()
        enabled = "1" if action == "开启" else "0"
        key = (
            "notify_todo_enabled"
            if target == "待办"
            else "notify_monitor_enabled"
        )
        notify_store.set_setting(key, enabled)

        state_text = "开了" if enabled == "1" else "关了"
        return f"✅ {target}播报{state_text}"

    if text in {"播报记录", "语音播报记录"}:
        return notify_records_text()


    # -------- 创建待办 --------

    if text in {"添加待办", "新建待办"}:
        return (
            "新待办这样发\n\n"
            "添加待办 2026-10-25 15:00 检查 VPS"
        )

    # 看起来像在加待办但格式不对时，直接提示，不要落到 AI 聊天。
    if text.startswith(("添加待办", "新建待办")) and not re.match(
        r"^添加待办\s+\d{4}-\d{2}-\d{2}\s+"
        r"\d{2}:\d{2}\s+.+$",
        text,
        re.DOTALL,
    ):
        return (
            "😅 格式差一点\n\n"
            "添加待办 2026-10-25 15:00 检查 VPS"
        )

    match = re.match(
        r"^添加待办\s+(\d{4}-\d{2}-\d{2})\s+"
        r"(\d{2}:\d{2})\s+(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        date_text, time_text, title = match.groups()
        title = title.strip()

        if not title:
            return "😅 待办得写点内容"

        if len(title) > 200:
            return "😅 标题太长，200 字以内"

        try:
            due_local = datetime.strptime(
                f"{date_text} {time_text}",
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            return "😅 日期写成 2026-10-25 15:00 这样"

        if due_local <= datetime.now(LOCAL_TZ):
            return "😅 这个时间已经过去了"

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
            "到点提醒你"
        )

    # -------- 查询待办 --------

    if text in {"我的待办", "待办列表", "查看待办"}:
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

        if not rows:
            return "📋 空空的，没有待办"

        lines = [f"📋 {len(rows)} 件事"]

        for todo_id, title, due_utc in rows:
            lines.append(
                f"\n#{todo_id} · {format_due_friendly(due_utc)}"
                f" · {title}"
            )

        return "\n".join(lines)

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

        if cursor.rowcount:
            if action == "完成待办":
                return f"✅ #{todo_id} 完成"
            return f"✅ #{todo_id} 取消"

        return f"😅 没有 #{todo_id} 这个待办"

    # -------- 保存记忆 --------

    match = re.match(
        r"^(?:记住|保存记忆)\s*[:：]\s*(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        content = match.group(1).strip()

        if not content:
            return "😅 要记的内容是空的"

        if len(content) > 2000:
            return "😅 太长了，2000 字以内"

        cursor = conn.execute(
            """
            INSERT INTO memories (owner_id, content, created_at)
            VALUES (?, ?, ?)
            """,
            (OWNER_OPEN_ID, content, now_utc()),
        )

        return (
            f"✅ 记下了 #{cursor.lastrowid}\n\n"
            f"{content[:150]}"
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
            return f"😅 没有找到“{keyword}”"

        lines = [f"🔎 {len(rows)} 条"]
        lines.extend(
            f"\n#{row[0]} · {row[1][:180]}"
            for row in rows
        )
        return "\n".join(lines)

    # -------- 列出记忆 --------

    if text in {"列出记忆", "我的记忆", "记忆列表"}:
        rows = conn.execute(
            """
            SELECT id, content
            FROM memories
            WHERE owner_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (OWNER_OPEN_ID,),
        ).fetchall()

        if not rows:
            return "📚 还没有记忆"

        lines = [f"📚 最近 {len(rows)} 条"]
        lines.extend(
            f"\n#{row[0]} · {row[1][:180]}"
            for row in rows
        )
        return "\n".join(lines)

    # -------- 删除记忆 --------

    match = re.match(
        r"^删除记忆\s*[:：#]?\s*(\d+)\s*$",
        text,
    )

    if match:
        memory_id = int(match.group(1))

        cursor = conn.execute(
            """
            DELETE FROM memories
            WHERE id = ? AND owner_id = ?
            """,
            (memory_id, OWNER_OPEN_ID),
        )

        if cursor.rowcount:
            return f"✅ #{memory_id} 删掉了"

        return f"😅 没有 #{memory_id}"

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
                "请使用简体中文回答，表达自然、清晰、简洁。"
                "你不能直接执行服务器命令，也不能声称已经完成"
                "实际未执行的操作。"
                "管理记忆请提示用户使用“记住：内容”；"
                "管理待办请提示用户使用机器人支持的待办命令。"
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

            with sqlite3.connect(DB_PATH, timeout=10) as conn:
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
                with sqlite3.connect(DB_PATH, timeout=10) as conn:
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
                    with sqlite3.connect(DB_PATH, timeout=10) as conn:
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
