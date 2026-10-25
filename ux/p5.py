#!/usr/bin/env python3
"""第 5 批：SSH 连接/断开通知精简为 3 行，并去掉菜单。"""

from pathlib import Path

WATCH = Path("/root/echomate-deploy/monitor/ssh_session_watch.py")


def patch(path, pairs):
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if new in text:
            print("  skip")
            continue
        if text.count(old) != 1:
            raise SystemExit(
                f"匹配失败：命中 {text.count(old)} 次\n---\n{old[:300]}\n---"
            )
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"  patched: {path.name}")


OLD_MENU = '''MENU = (
    "🤖 Echo-Mate 功能菜单\\n\\n"
    "请单独发送数字，或单独发送对应文字指令：\\n"
    "1. 检查 VPS\\n"
    "2. 我的待办\\n"
    "3. 添加待办\\n"
    "4. 列出记忆\\n"
    "5. 帮助\\n\\n"
    "只有整条消息匹配指令时才执行功能。"
)
'''

NEW_MENU = '''MENU = (
    "Echo\\n\\n"
    "1 · 状态\\n"
    "2 · 待办\\n"
    "3 · 新待办\\n"
    "4 · 记忆\\n"
    "5 · 帮助\\n"
    "6 · 播报"
)
'''

OLD_REPORT = '''    service_lines = []
    for unit, label in SERVICES:
        active = monitor.service_is_active(unit)
        service_lines.append(
            f"{label}：{'运行正常' if active else '异常/未运行'}"
        )

    report = (
        "🟢 检测到 SSH 终端连接\\n"
        f"主机：{os.uname().nodename}\\n"
        f"报告时间（北京时间）：{now:%Y-%m-%d %H:%M:%S}\\n"
        f"系统启动时间：{boot:%Y-%m-%d %H:%M:%S}\\n"
        f"运行时间：{days}天{hours}小时{minutes}分钟\\n\\n"
        f"CPU：{cpu:.1f}%（{psutil.cpu_count()} 个逻辑核）\\n"
        f"内存：总计 {mem.total / 1024**3:.2f} GiB，"
        f"可用 {mem.available / 1024**3:.2f} GiB，"
        f"使用率 {mem.percent:.1f}%\\n"
        f"Swap：已使用 {swap.used / 1024**3:.2f} / "
        f"{swap.total / 1024**3:.2f} GiB\\n"
        f"磁盘 /：剩余 {disk.free / 1024**3:.2f} / "
        f"{disk.total / 1024**3:.2f} GiB，"
        f"使用率 {disk.percent:.1f}%\\n\\n"
        "关键服务状态：\\n"
        + "\\n".join(service_lines)
    )

    return report + "\\n\\n" + MENU
'''

NEW_REPORT = '''    down = [
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
        f"{head}\\n\\n"
        f"CPU {cpu:.0f}% · 内存 {mem.available / 1024**2:.0f}M"
        f" · 磁盘 {disk.free / 1024**3:.0f}G\\n"
        f"运行 {uptime} · {len(SERVICES) - len(down)}/{len(SERVICES)} 服务在线"
    )

    if down:
        report += "\\n掉线：" + "、".join(down)

    return report
'''

OLD_DISCONNECT = '''    message = (
        "🔴 所有 SSH 终端已断开\\n"
        f"检测时间（北京时间）：{now:%Y-%m-%d %H:%M:%S}\\n"
        "当前没有检测到活动的 pts/* 终端。\\n"
        "注意：这不代表 VPS 已关机，后台服务仍可继续运行。"
    )
'''

NEW_DISCONNECT = '''    message = (
        "🔴 断开 · 后台还在跑\\n\\n"
        f"{now:%H:%M}"
    )
'''

print("[1/3] SSH 菜单")
patch(WATCH, [(OLD_MENU, NEW_MENU)])

print("[2/3] SSH 连接报告")
patch(WATCH, [(OLD_REPORT, NEW_REPORT)])

print("[3/3] SSH 断开")
patch(WATCH, [(OLD_DISCONNECT, NEW_DISCONNECT)])

print("p5 done")
