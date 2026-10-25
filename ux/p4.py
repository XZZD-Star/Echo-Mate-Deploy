#!/usr/bin/env python3
"""第 4 批：监控告警/恢复/开机报告/启动菜单。"""

from pathlib import Path

MONITOR = Path("/root/echomate-deploy/monitor/monitor.py")


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


# ---------- 1. 告警与持续告警 ----------
OLD_ALERT = '''                if state["active"]:
                    heading = "⚠️ VPS 异常仍在持续"
                else:
                    heading = "🚨 VPS 异常告警"

                message = (
                    f"{heading}\\n"
                    f"监控项：{title}\\n"
                    f"详情：{details}\\n"
                    f"时间：{current.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
                )
'''

NEW_ALERT = '''                short_title = title.split("：", 1)[-1]
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
                    f"{heading}\\n\\n"
                    f"{details}\\n"
                    f"{current.astimezone().strftime('%H:%M')}"
                )
'''


# ---------- 2. 恢复 ----------
OLD_RECOVERY = '''            message = (
                f"✅ VPS 异常已恢复\\n"
                f"监控项：{title}\\n"
                f"当前状态：{details}\\n"
                f"时间：{current.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
            )
'''

NEW_RECOVERY = '''            short_title = title.split("：", 1)[-1]
            friendly = {
                "echomate-server.service": "语音服务",
                "echomate-feishu-bot.service": "飞书机器人",
                "echomate-monitor.service": "监控服务",
            }.get(short_title, short_title)

            message = (
                f"✅ {friendly}回来了\\n\\n"
                f"{current.astimezone().strftime('%H:%M')}"
            )
'''


# ---------- 3. 开机报告 ----------
OLD_BOOT = '''    service_lines = []
    for unit, label in [
        ("echomate-server.service", "Echo-Mate Server"),
        ("echomate-feishu-bot.service", "飞书机器人"),
        ("echomate-monitor.service", "VPS 监控"),
    ]:
        active = service_is_active(unit)
        service_lines.append(
            f"{label}：{'运行正常' if active else '异常/未运行'}"
        )

    # 首次启用时发送基线报告；以后只在新的系统启动周期报告。
    heading = (
        "📊 VPS 监控已启用，当前状态基线"
        if row is None
        else "🟢 VPS 已启动，本次开机状态报告"
    )

    report = (
        f"{heading}\\n"
        f"主机：{os.uname().nodename}\\n"
        f"报告时间（北京时间）：{current:%Y-%m-%d %H:%M:%S}\\n"
        f"系统启动时间：{boot:%Y-%m-%d %H:%M:%S}\\n"
        f"运行时间：{uptime_text}\\n\\n"
        f"CPU：{cpu:.1f}%（{psutil.cpu_count()} 个逻辑核）\\n"
        f"内存：总计 {mem.total / 1024**3:.2f} GiB，"
        f"可用 {mem.available / 1024**3:.2f} GiB，"
        f"使用率 {mem.percent:.1f}%\\n"
        f"Swap：已使用 {swap.used / 1024**3:.2f} / "
        f"{swap.total / 1024**3:.2f} GiB\\n"
        f"磁盘 /：剩余 {disk.free / 1024**3:.2f} / "
        f"{disk.total / 1024**3:.2f} GiB，"
        f"使用率 {disk.percent:.1f}%\\n\\n"
        "关键服务状态：\\n" + "\\n".join(service_lines)
    )
'''

NEW_BOOT = '''    services = [
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
        f"{heading}\\n\\n"
        f"CPU {cpu:.0f}% · 内存 {mem.available / 1024**2:.0f}M"
        f" · 磁盘 {disk.free / 1024**3:.0f}G\\n"
        f"运行 {uptime_text} · {len(services) - len(down)}/{len(services)} 服务在线"
    )

    if down:
        report += "\\n掉线：" + "、".join(down)
'''


# ---------- 4. 启动菜单 ----------
OLD_MENU = '''    menu = (
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

NEW_MENU = '''    menu = (
        "Echo\\n\\n"
        "1 · 状态\\n"
        "2 · 待办\\n"
        "3 · 新待办\\n"
        "4 · 记忆\\n"
        "5 · 帮助\\n"
        "6 · 播报"
    )
'''

print("[1/4] 告警")
patch(MONITOR, [(OLD_ALERT, NEW_ALERT)])

print("[2/4] 恢复")
patch(MONITOR, [(OLD_RECOVERY, NEW_RECOVERY)])

print("[3/4] 开机报告")
patch(MONITOR, [(OLD_BOOT, NEW_BOOT)])

print("[4/4] 启动菜单")
patch(MONITOR, [(OLD_MENU, NEW_MENU)])

print("p4 done")
