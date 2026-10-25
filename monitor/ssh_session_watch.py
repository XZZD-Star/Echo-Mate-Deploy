import json
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psutil
import monitor

STATE_PATH = Path("/var/lib/echomate/ssh_session_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))
POLL_INTERVAL = 2
DISCONNECT_CONFIRM_SECONDS = 3

SERVICES = [
    ("echomate-server.service", "Echo-Mate Server"),
    ("echomate-feishu-bot.service", "飞书机器人"),
    ("echomate-monitor.service", "VPS 监控"),
]

MENU = (
    "🐣 Echo\n\n"
    "1 · 📊 状态\n"
    "2 · 📋 待办\n"
    "3 · ➕ 新待办\n"
    "4 · 📚 记忆\n"
    "5 · 💡 帮助\n"
    "6 · 🔊 播报"
)


def current_boot_id():
    try:
        return Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return str(int(psutil.boot_time()))


def get_terminal_count():
    """统计 who -u 中的伪终端，忽略本地 tty1 控制台。"""
    result = subprocess.run(
        ["who", "-u"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError("执行 who -u 失败")

    count = 0
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].startswith("pts/"):
            count += 1

    return count


def new_state(boot_id):
    return {
        "boot_id": boot_id,
        "count": 0,
        "zero_since": None,
    }


def load_state(boot_id):
    if not STATE_PATH.exists():
        return new_state(boot_id)

    try:
        state = json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("会话状态文件无法读取") from exc

    # 新的操作系统启动周期，不继承旧机器的终端数量。
    if state.get("boot_id") != boot_id:
        return new_state(boot_id)

    if not isinstance(state.get("count"), int):
        raise RuntimeError("会话状态文件中的 count 无效")

    if "zero_since" not in state:
        state["zero_since"] = None

    return state


def save_state(state):
    temp = STATE_PATH.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    temp.replace(STATE_PATH)


def make_connect_message():
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")

    now = datetime.now(LOCAL_TZ)
    boot = datetime.fromtimestamp(
        psutil.boot_time(), tz=timezone.utc
    ).astimezone(LOCAL_TZ)

    uptime = max(0, int(time.time() - psutil.boot_time()))
    days, remainder = divmod(uptime, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60

    down = [
        label for unit, label in SERVICES
        if not monitor.service_is_active(unit)
    ]

    if down:
        head = "🟡 连上了，有服务掉线"
    elif cpu > 90 or mem.available < 250 * 1024**2:
        head = "🟡 连上了，有点忙"
    else:
        head = "🟢 连上了"

    uptime = (
        f"{days} 天" if days else f"{hours} 小时"
    )

    report = (
        f"{head}\n\n"
        f"CPU {cpu:.0f}% · 内存 {mem.available / 1024**2:.0f}M"
        f" · 磁盘 {disk.free / 1024**3:.0f}G\n"
        f"运行 {uptime} · {len(SERVICES) - len(down)}/{len(SERVICES)} 服务在线"
    )

    if down:
        report += "\n掉线：" + "、".join(down)

    report += "\n\n0 回菜单"
    return report


def send_disconnect_message():
    now = datetime.now(LOCAL_TZ)
    message = (
        "🔴 断开 · 后台还在跑\n\n"
        f"{now:%H:%M}"
    )
    return monitor.send_feishu(message)


def main():
    boot_id = current_boot_id()
    state = load_state(boot_id)
    save_state(state)

    print(
        f"[SSH 监控] 已启动，当前状态记录为 "
        f"{state['count']} 个终端",
        flush=True,
    )

    while True:
        try:
            count = get_terminal_count()
            now = time.time()

            # 从没有终端变成有终端：发送报告与菜单。
            if count > 0:
                if state["zero_since"] is not None:
                    # 在断开确认窗口内重新连接，不发送断开通知。
                    state["zero_since"] = None
                    state["count"] = count
                    save_state(state)

                elif state["count"] == 0:
                    message = make_connect_message()
                    if monitor.send_feishu(message):
                        state["count"] = count
                        state["zero_since"] = None
                        save_state(state)
                        print(
                            "[SSH 监控] 首次连接报告与菜单已发送",
                            flush=True,
                        )

                elif state["count"] != count:
                    # 增加或关闭部分终端，不重复发送通知。
                    state["count"] = count
                    save_state(state)

            # 原来有终端，现在没有：启动 3 秒确认窗口。
            elif state["count"] > 0:
                if state["zero_since"] is None:
                    state["zero_since"] = now
                    save_state(state)

                elif now - state["zero_since"] >= DISCONNECT_CONFIRM_SECONDS:
                    if send_disconnect_message():
                        state["count"] = 0
                        state["zero_since"] = None
                        save_state(state)
                        print(
                            "[SSH 监控] 最后一个终端断开通知已发送",
                            flush=True,
                        )

        except Exception as exc:
            print(
                f"[SSH 监控] 检查失败：{type(exc).__name__}: {exc}",
                flush=True,
            )

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
