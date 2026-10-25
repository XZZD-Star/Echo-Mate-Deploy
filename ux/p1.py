#!/usr/bin/env python3
"""第 1 批：友好时间、菜单、帮助、VPS 状态。"""

import sys

sys.path.insert(0, "/root/echomate-deploy/ux")
from lib_text import BOT, patch

OLD_DUE = '''def format_due(utc_text):
    dt = datetime.fromisoformat(utc_text).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")
'''

NEW_DUE = '''def format_due(utc_text):
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
'''

OLD_MENU = '''def menu_text():
    return (
        "🤖 Echo-Mate 功能菜单\\n\\n"
        "1. 检查 VPS\\n"
        "2. 我的待办\\n"
        "3. 添加待办\\n"
        "4. 列出记忆\\n"
        "5. 帮助\\n"
        "6. 播报设置\\n\\n"
        "可以发送数字或单独发送对应的功能指令。"
    )
'''

NEW_MENU = '''def menu_text():
    return (
        "Echo\\n\\n"
        "1 · 状态\\n"
        "2 · 待办\\n"
        "3 · 新待办\\n"
        "4 · 记忆\\n"
        "5 · 帮助\\n"
        "6 · 播报"
    )
'''

OLD_HELP = '''def help_text():
    return (
        "你好，我是 Echo-Mate Personal Assistant。\\n\\n"
        "【记忆管理】\\n"
        "记住：Echo-Mate 已完成 VPS 部署\\n"
        "查记忆：VPS 部署\\n"
        "列出记忆\\n"
        "删除记忆 1\\n\\n"
        "【待办管理】\\n"
        "添加待办 2026-10-18 15:00 检查 VPS\\n"
        "我的待办\\n"
        "完成待办 1\\n"
        "取消待办 1\\n\\n"
        "待办时间采用北京时间（Asia/Shanghai）。\\n"
        "到期后机器人会通过飞书发送提醒。\\n\\n"
        "【主动播报】\\n"
        "播报设置\\n"
        "静音 23:00 08:00\\n"
        "关闭静音\\n"
        "关闭待办播报 / 开启待办播报\\n"
        "关闭监控播报 / 开启监控播报\\n"
        "播报记录\\n\\n"
        "桌宠停在桌宠页面且空闲时才会开口播报。"
    )
'''

NEW_HELP = '''def help_text():
    return (
        "Echo 能做什么\\n\\n"
        "添加待办 2026-10-25 15:00 检查 VPS\\n"
        "记住：xxx　查记忆 xxx\\n"
        "静音 23:00 08:00\\n\\n"
        "发 6 看播报，发 1 看状态"
    )
'''

OLD_STATUS = '''    uptime = timedelta(
        seconds=max(0, int(time.time() - psutil.boot_time()))
    )
    uptime_text = (
        f"{uptime.days}天"
        f"{uptime.seconds // 3600}小时"
        f"{(uptime.seconds % 3600) // 60}分钟"
    )

    services = [
        ("echomate-server.service", "Echo-Mate Server"),
        ("echomate-feishu-bot.service", "飞书机器人"),
        ("echomate-monitor.service", "VPS 监控"),
    ]

    lines = [
        "🖥 VPS 实时状态报告",
        f"主机：{os.uname().nodename}",
        f"报告时间（北京时间）：{current:%Y-%m-%d %H:%M:%S}",
        f"系统最近启动：{boot:%Y-%m-%d %H:%M:%S}",
        f"运行时间：{uptime_text}",
        "",
        f"CPU：{cpu:.1f}%（{psutil.cpu_count()} 个逻辑核）",
        (
            f"内存：总计 {mem.total / 1024**3:.2f} GiB，"
            f"可用 {mem.available / 1024**3:.2f} GiB，"
            f"使用率 {mem.percent:.1f}%"
        ),
        (
            f"Swap：已使用 {swap.used / 1024**3:.2f} / "
            f"{swap.total / 1024**3:.2f} GiB"
        ),
        (
            f"磁盘 /：剩余 {disk.free / 1024**3:.2f} / "
            f"{disk.total / 1024**3:.2f} GiB，"
            f"使用率 {disk.percent:.1f}%"
        ),
        "",
        "关键服务状态：",
    ]

    for unit, label in services:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            state = result.stdout.strip() or "unknown"
        except (OSError, subprocess.TimeoutExpired):
            state = "检查失败"

        lines.append(f"{label}：{state}")

    return "\\n".join(lines)
'''

NEW_STATUS = '''    uptime = timedelta(
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

    return "\\n".join(lines)
'''

print("[1/4] 友好时间")
patch(BOT, [(OLD_DUE, NEW_DUE)])

print("[2/4] 菜单")
patch(BOT, [(OLD_MENU, NEW_MENU)])

print("[3/4] 帮助")
patch(BOT, [(OLD_HELP, NEW_HELP)])

print("[4/4] VPS 状态")
patch(BOT, [(OLD_STATUS, NEW_STATUS)])

print("p1 done")
