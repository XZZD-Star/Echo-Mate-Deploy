#!/usr/bin/env python3
"""第 2 批：播报设置、播报记录、类型名、静音与开关。"""

import sys

sys.path.insert(0, "/root/echomate-deploy/ux")
from lib_text import BOT, patch

OLD_SETTINGS = '''    lines = [
        "🔊 主动播报设置",
        f"静音时段：{notify_store.quiet_text()}",
        f"待办播报：{'开启' if todo_on else '关闭'}",
        f"监控播报：{'开启' if monitor_on else '关闭'}",
        f"待播通知：{notify_store.pending_count()} 条",
        "",
        "发送以下命令可修改：",
        "静音 23:00 08:00",
        "关闭静音",
        "关闭待办播报 / 开启待办播报",
        "关闭监控播报 / 开启监控播报",
        "播报记录",
    ]
    return "\\n".join(lines)
'''

NEW_SETTINGS = '''    quiet = notify_store.quiet_text().replace("（北京时间）", "")
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

    return "\\n".join(lines)
'''

OLD_RECORDS = '''    status_label = {
        "pending": "待播",
        "claimed": "播放中",
        "sent": "已播",
        "failed": "失败",
        "expired": "已过期",
    }

    lines = [f"📢 最近 {len(rows)} 条播报记录："]

    for row in rows:
        sent = (row["sent_at_utc"] or "").replace("T", " ")[:16]
        acked = "已确认" if row["acked_at_utc"] else "未确认"
        status = status_label.get(row["status"], row["status"])

        if row["status"] == "sent":
            tail = acked
        elif row["last_error"]:
            tail = f"错误：{row['last_error'][:40]}"
        else:
            tail = ""

        lines.append(
            f"\\n#{row['id']}｜{_kind_label(row['kind'])}｜{status}"
            f"｜{sent}{('｜' + tail) if tail else ''}"
            f"\\n{row['text'][:60]}"
        )

    return "\\n".join(lines)
'''

NEW_RECORDS = '''    status_label = {
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
            f"\\n#{row['id']} · {_kind_label(row['kind'])} · {mark}"
            f"\\n{row['text'][:50]}"
        )

    return "\\n".join(lines)
'''

OLD_KIND = '''def _kind_label(kind):
    return {
        "todo": "待办",
        "alert": "监控告警",
        "recovery": "监控恢复",
        "manual": "手动测试",
    }.get(kind, kind)
'''

NEW_KIND = '''def _kind_label(kind):
    return {
        "todo": "待办",
        "alert": "告警",
        "recovery": "恢复",
        "manual": "测试",
    }.get(kind, kind)
'''

OLD_QUIET_ERR = '''        if start is None or end is None:
            return "时间格式不正确，请使用 HH:MM，例如：静音 23:00 08:00"

        if start == end:
            return "开始和结束时间不能相同。"
'''

NEW_QUIET_ERR = '''        if start is None or end is None:
            return "😅 时间写成 23:00 08:00 这样"

        if start == end:
            return "😅 开始和结束不能一样"
'''

OLD_QUIET_OK = '''        return (
            f"🔇 已设置静音时段：{start} - {end}（北京时间）\\n"
            "该时段内桌宠不会主动开口，飞书通知不受影响。\\n"
            "服务端最多 60 秒内生效，无需重启。"
        )
'''

NEW_QUIET_OK = '''        return (
            f"🔇 静音 {start}–{end}\\n\\n"
            "这段桌宠闭嘴，飞书照常"
        )
'''

OLD_QUIET_OFF = '''        return "🔔 已关闭静音时段，桌宠全天可播报。"
'''

NEW_QUIET_OFF = '''        return "🔔 静音关掉了"
'''

OLD_SWITCH = '''        state_text = "开启" if enabled == "1" else "关闭"
        return f"✅ 已{state_text}{target}类主动播报。"
'''

NEW_SWITCH = '''        state_text = "开了" if enabled == "1" else "关了"
        return f"✅ {target}播报{state_text}"
'''

print("[1/4] 播报设置")
patch(BOT, [(OLD_SETTINGS, NEW_SETTINGS)])

print("[2/4] 播报记录")
patch(BOT, [(OLD_RECORDS, NEW_RECORDS)])

print("[3/4] 类型名")
patch(BOT, [(OLD_KIND, NEW_KIND)])

print("[4/4] 静音与开关")
patch(
    BOT,
    [
        (OLD_QUIET_ERR, NEW_QUIET_ERR),
        (OLD_QUIET_OK, NEW_QUIET_OK),
        (OLD_QUIET_OFF, NEW_QUIET_OFF),
        (OLD_SWITCH, NEW_SWITCH),
    ],
)

print("p2 done")
