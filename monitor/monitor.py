import argparse
import sqlite3
from contextlib import contextmanager
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psutil
import lark_oapi as lark
from dotenv import load_dotenv
import os
import json
import sys

# 主动播报队列：与 AIChat Server 共用同一份 SQLite。
sys.path.insert(0, "/root/echomate-deploy/app/Demo/AIChat_demo/Server")
from tools import notify_store


# ================= 配置 =================

os.umask(0o077)

BOT_DIR = Path("/root/echomate-deploy/feishu_bot")
load_dotenv(BOT_DIR / ".env")

APP_ID = (os.getenv("LARK_APP_ID") or "").strip()
APP_SECRET = (os.getenv("LARK_APP_SECRET") or "").strip()
OWNER_OPEN_ID = (os.getenv("LARK_OWNER_OPEN_ID") or "").strip()

DB_PATH = Path("/var/lib/echomate/assistant.db")

CHECK_INTERVAL = 60
DISK_INTERVAL = 300
ALERT_COOLDOWN = 1800

CPU_LIMIT = 90.0
CPU_DURATION = 300
MEMORY_LIMIT = 250 * 1024 * 1024
DISK_FREE_LIMIT = 3 * 1024 * 1024 * 1024
DISK_USAGE_LIMIT = 85.0

WATCH_SERVICES = [
    "echomate-server.service",
    "echomate-feishu-bot.service",
]

if not APP_ID or not APP_SECRET or not OWNER_OPEN_ID:
    raise SystemExit(
        "监控启动失败：飞书 App ID、App Secret 或 Open ID 未配置"
    )


# ================= 飞书通知 =================

api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .build()
)


def send_feishu(text):
    """通过飞书机器人向自己的 open_id 发送文字。"""
    request = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(OWNER_OPEN_ID)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )

    try:
        response = api_client.im.v1.message.create(request)
        if response.success():
            print("[通知] 飞书消息发送成功", flush=True)
            return True

        print(
            f"[通知] 发送失败：code={response.code}, "
            f"msg={response.msg}, log_id={response.get_log_id()}",
            flush=True,
        )
        return False
    except Exception as exc:
        print(
            f"[通知] 发送异常：{type(exc).__name__}",
            flush=True,
        )
        return False


# ================= 持久化告警状态 =================

def now_utc():
    return datetime.now(timezone.utc)


def now_text():
    return now_utc().isoformat(timespec="seconds")


def parse_time(value):
    return datetime.fromisoformat(value)


@contextmanager
def db_connect():
    """连接用完必须关闭，否则 WAL 下会累积句柄并长期持有写锁。"""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db_connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monitor_state (
                alert_key TEXT PRIMARY KEY,
                is_active INTEGER NOT NULL DEFAULT 0,
                bad_count INTEGER NOT NULL DEFAULT 0,
                first_bad_at TEXT,
                last_notified_at TEXT,
                details TEXT,
                updated_at TEXT NOT NULL
            )
        """)


def load_state(key):
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT is_active, bad_count, first_bad_at,
                   last_notified_at, details
            FROM monitor_state
            WHERE alert_key = ?
            """,
            (key,),
        ).fetchone()

    if not row:
        return {
            "active": False,
            "count": 0,
            "first_bad": None,
            "last_notified": None,
            "details": None,
        }

    return {
        "active": bool(row[0]),
        "count": row[1],
        "first_bad": row[2],
        "last_notified": row[3],
        "details": row[4],
    }


def save_state(key, state, details):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO monitor_state (
                alert_key, is_active, bad_count, first_bad_at,
                last_notified_at, details, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alert_key) DO UPDATE SET
                is_active=excluded.is_active,
                bad_count=excluded.bad_count,
                first_bad_at=excluded.first_bad_at,
                last_notified_at=excluded.last_notified_at,
                details=excluded.details,
                updated_at=excluded.updated_at
            """,
            (
                key,
                int(state["active"]),
                state["count"],
                state["first_bad"],
                state["last_notified"],
                details,
                now_text(),
            ),
        )


def spoken_alert_text(title, details, recovery=False):
    """把监控项转成适合朗读的短句，不念多行原文。"""
    label = title

    if label.startswith("服务状态："):
        service = label.split("：", 1)[1]
        friendly = {
            "echomate-server.service": "语音服务",
            "echomate-feishu-bot.service": "飞书机器人",
        }.get(service, service)
        label = friendly

    metric = label.endswith(("不足", "过高", "过低", "已满"))

    if recovery:
        return f"{label}，恢复正常。" if metric else f"{label}好了。"

    # 指标类标题本身已含“不足/过高”，不再拼“异常”。
    if metric:
        return f"注意，{label}。"

    return f"注意，VPS 的{label}异常。"


def queue_spoken_alert(key, title, details, state,
                       recovery=False):
    """写入播报队列。失败只记日志，不影响飞书告警。"""
    try:
        dedupe_key = None
        if not recovery and state.get("first_bad"):
            dedupe_key = f"monitor:{key}:{state['first_bad']}"

        notify_store.enqueue(
            kind="recovery" if recovery else "alert",
            text=spoken_alert_text(title, details, recovery),
            dedupe_key=dedupe_key,
            priority=3 if recovery else 1,
            ttl_minutes=10 if recovery else 30,
        )
    except Exception as exc:
        print(
            f"[播报] {key} 入队失败：{type(exc).__name__}",
            flush=True,
        )


def evaluate(key, title, abnormal, details,
             min_count=1, min_duration=0):
    """记录连续异常，只在达到阈值时告警，恢复后发送恢复通知。"""
    state = load_state(key)
    current = now_utc()

    if abnormal:
        state["count"] += 1

        if state["first_bad"] is None:
            state["first_bad"] = current.isoformat(
                timespec="seconds"
            )

        elapsed = (
            current - parse_time(state["first_bad"])
        ).total_seconds()

        confirmed = (
            state["count"] >= min_count
            and elapsed >= min_duration
        )

        if confirmed:
            cooldown_passed = (
                state["last_notified"] is None
                or (
                    current - parse_time(
                        state["last_notified"]
                    )
                ).total_seconds() >= ALERT_COOLDOWN
            )

            if not state["active"] or cooldown_passed:
                short_title = title.split("：", 1)[-1]
                friendly = {
                    "echomate-server.service": "语音服务",
                    "echomate-feishu-bot.service": "飞书机器人",
                    "echomate-monitor.service": "监控服务",
                }.get(short_title, short_title)

                heading = (
                    f"⚠️ {friendly}还没好"
                    if state["active"]
                    else f"🚨 {friendly}掉线"
                )

                message = (
                    f"{heading}\n\n"
                    f"{details}\n"
                    f"{current.astimezone().strftime('%H:%M')}"
                )

                if send_feishu(message):
                    state["active"] = True
                    state["last_notified"] = current.isoformat(
                        timespec="seconds"
                    )
                    queue_spoken_alert(
                        key, title, details, state
                    )
    else:
        state["count"] = 0
        state["first_bad"] = None

        if state["active"]:
            short_title = title.split("：", 1)[-1]
            friendly = {
                "echomate-server.service": "语音服务",
                "echomate-feishu-bot.service": "飞书机器人",
                "echomate-monitor.service": "监控服务",
            }.get(short_title, short_title)

            message = (
                f"✅ {friendly}回来了\n\n"
                f"{current.astimezone().strftime('%H:%M')}"
            )

            if send_feishu(message):
                state["active"] = False
                state["last_notified"] = None
                queue_spoken_alert(
                    key, title, details, state, recovery=True
                )

    save_state(key, state, details)


# ================= 监控检查 =================

def service_is_active(name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_services():
    for service in WATCH_SERVICES:
        active = service_is_active(service)

        evaluate(
            key=f"service:{service}",
            title=f"服务状态：{service}",
            abnormal=not active,
            details=(
                "服务运行正常"
                if active
                else "服务不活跃或状态检查失败"
            ),
        )


def check_cpu_memory():
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()

    print(
        f"[采样] CPU={cpu:.1f}%, "
        f"可用内存={memory.available / 1024**2:.0f} MiB, "
        f"Swap={swap.percent:.1f}%",
        flush=True,
    )

    evaluate(
        key="cpu_high",
        title="CPU 使用率过高",
        abnormal=cpu > CPU_LIMIT,
        details=f"CPU 使用率：{cpu:.1f}%，阈值：{CPU_LIMIT:.0f}%",
        min_duration=CPU_DURATION,
    )

    evaluate(
        key="memory_low",
        title="可用内存不足",
        abnormal=memory.available < MEMORY_LIMIT,
        details=(
            f"可用内存：{memory.available / 1024**2:.0f} MiB，"
            f"阈值：{MEMORY_LIMIT / 1024**2:.0f} MiB"
        ),
        min_count=3,
    )


def check_disk():
    disk = psutil.disk_usage("/")
    free_gib = disk.free / 1024**3

    print(
        f"[采样] 根分区已使用={disk.percent:.1f}%, "
        f"剩余={free_gib:.2f} GiB",
        flush=True,
    )

    abnormal = (
        disk.free < DISK_FREE_LIMIT
        or disk.percent >= DISK_USAGE_LIMIT
    )

    evaluate(
        key="disk_low",
        title="根分区空间不足",
        abnormal=abnormal,
        details=(
            f"使用率：{disk.percent:.1f}%，"
            f"剩余空间：{free_gib:.2f} GiB；"
            f"阈值：剩余低于 3 GiB 或使用率达到 85%"
        ),
    )



def send_startup_report_once():
    """每个系统启动周期只发送一次状态报告。"""
    try:
        boot_id = Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        boot_id = str(int(psutil.boot_time()))

    with db_connect() as conn:
        row = conn.execute(
            "SELECT details FROM monitor_state WHERE alert_key = ?",
            ("vps_boot_report",),
        ).fetchone()

    # 同一次 VPS 开机期间，不因 SSH 重连或监控服务重启而重复发送。
    if row and row[0] == boot_id:
        return

    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")

    tz = timezone(timedelta(hours=8))
    current = datetime.now(tz)
    boot = datetime.fromtimestamp(
        psutil.boot_time(), tz=timezone.utc
    ).astimezone(tz)

    uptime = timedelta(
        seconds=max(0, int(time.time() - psutil.boot_time()))
    )
    uptime_text = (
        f"{uptime.days}天"
        f"{uptime.seconds // 3600}小时"
        f"{(uptime.seconds % 3600) // 60}分钟"
    )

    services = [
        ("echomate-server.service", "语音"),
        ("echomate-feishu-bot.service", "飞书"),
        ("echomate-monitor.service", "监控"),
    ]

    down = [label for unit, label in services if not service_is_active(unit)]

    # 首次启用时发送基线报告；以后只在新的系统启动周期报告。
    heading = (
        "📊 监控上线了"
        if row is None
        else "🟢 开机了"
    )

    report = (
        f"{heading}\n\n"
        f"CPU {cpu:.0f}% · 内存 {mem.available / 1024**2:.0f}M"
        f" · 磁盘 {disk.free / 1024**3:.0f}G\n"
        f"运行 {uptime_text} · {len(services) - len(down)}/{len(services)} 服务在线"
    )

    if down:
        report += "\n掉线：" + "、".join(down)

    report += "\n\n0 回菜单"

    if down:
        report += "\n掉线：" + "、".join(down)

    # 发送失败时不写入 Boot ID，下个检查周期会重试。
    if not send_feishu(report):
        return

    timestamp = now_text()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO monitor_state (
                alert_key, is_active, bad_count, first_bad_at,
                last_notified_at, details, updated_at
            )
            VALUES (?, 0, 0, NULL, ?, ?, ?)
            ON CONFLICT(alert_key) DO UPDATE SET
                last_notified_at = excluded.last_notified_at,
                details = excluded.details,
                updated_at = excluded.updated_at
            """,
            ("vps_boot_report", timestamp, boot_id, timestamp),
        )

    print("[监控] 本次开机状态报告已发送", flush=True)



def send_startup_menu_once():
    """每个系统启动周期只发送一次功能菜单。"""
    try:
        boot_id = Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        boot_id = str(int(psutil.boot_time()))

    with db_connect() as conn:
        row = conn.execute(
            "SELECT details FROM monitor_state WHERE alert_key = ?",
            ("vps_startup_menu",),
        ).fetchone()

    if row and row[0] == boot_id:
        return

    menu = (
        "🐣 Echo\n\n"
        "1 · 📊 状态\n"
        "2 · 📋 待办\n"
        "3 · ➕ 新待办\n"
        "4 · 📚 记忆\n"
        "5 · 💡 帮助\n"
        "6 · 🔊 播报"
    )

    if not send_feishu(menu):
        return

    timestamp = now_text()
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO monitor_state (
                alert_key, is_active, bad_count, first_bad_at,
                last_notified_at, details, updated_at
            )
            VALUES (?, 0, 0, NULL, ?, ?, ?)
            ON CONFLICT(alert_key) DO UPDATE SET
                last_notified_at = excluded.last_notified_at,
                details = excluded.details,
                updated_at = excluded.updated_at
            """,
            ("vps_startup_menu", timestamp, boot_id, timestamp),
        )

    print("[监控] 本启动周期功能菜单已发送", flush=True)

def monitor_loop():
    init_db()

    print("[监控] Echo-Mate VPS 监控已启动", flush=True)
    print(
        f"[监控] 常规检查间隔 {CHECK_INTERVAL}s；"
        f"磁盘检查间隔 {DISK_INTERVAL}s；"
        f"告警冷却 {ALERT_COOLDOWN // 60}min",
        flush=True,
    )

    last_disk_check = 0.0

    while True:
        loop_start = time.monotonic()

        try:
            # SSH 连接报告和菜单由独立会话监控服务负责。
            check_cpu_memory()
            check_services()

            if time.monotonic() - last_disk_check >= DISK_INTERVAL:
                check_disk()
                last_disk_check = time.monotonic()

        except Exception as exc:
            print(
                f"[监控] 本轮检查异常：{type(exc).__name__}",
                flush=True,
            )

        elapsed = time.monotonic() - loop_start
        time.sleep(max(1, CHECK_INTERVAL - elapsed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test",
        action="store_true",
        help="只发送一条测试通知，不启动持续监控",
    )
    args = parser.parse_args()

    if args.test:
        ok = send_feishu(
            "✅ Echo-Mate VPS 监控测试成功。\n"
            "飞书主动通知通道已打通。"
        )
        raise SystemExit(0 if ok else 1)

    monitor_loop()
