import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import psutil

from ..constants import BACKUP_UNIT, SERVICE_UNITS
from ..runtime import LOCAL_TZ
from .formatting import (
    format_bytes as _format_bytes,
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
