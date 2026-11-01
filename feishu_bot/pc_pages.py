"""Text pages for the Feishu local Codex control area."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


try:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=8))

JOB_LABELS = {
    "queued": "排队中",
    "dispatched": "已送达",
    "running": "执行中",
    "succeeded": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
    "expired": "已超时",
    "uncertain": "结果不确定",
}


def short_text(value, limit=80):
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def task_title(prompt, limit=24):
    text = short_text(prompt, limit)
    return text or "本机任务"


def _time_label(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone(
            LOCAL_TZ
        ).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _online_label(online, health=None):
    if not online:
        return "🔴 电脑离线"
    state = (health or {}).get("state", "idle")
    label = "执行中" if state == "busy" else "空闲"
    device = (health or {}).get("display_name") or "Windows PC"
    return f"🟢 {device} · {label}"


def render_pc_home(online, health=None):
    slots = 2
    return "\n".join([
        "🖥 本机 Codex",
        "",
        _online_label(online, health),
        f"固定会话恢复槽：{slots} 个",
        "",
        "1 · 单次任务",
        "2 · 固定会话",
        "3 · 任务历史",
        "0 · 返回",
    ])


def render_pc_single(online, health=None):
    if not online:
        body = (
            "本机 Companion 没有连上。\n"
            "请先启动本机 Companion 和 SSH 反向隧道。"
        )
    else:
        body = "发送一条任务。提交后立即回到电脑首页，不保留上下文。"
    return "\n".join([
        "🖥 单次任务",
        "",
        _online_label(online, health),
        "",
        body,
        "",
        "0 · 取消",
    ])


def render_pc_fixed(active_session, recoverable_sessions, active_job,
                    online, health=None, turn_count=0):
    if active_session is None:
        lines = [
            "🖥 固定会话",
            "",
            _online_label(online, health),
            "",
        ]
        if recoverable_sessions:
            lines.append("最近会话")
            for index, session in enumerate(recoverable_sessions[:2], 1):
                state = "活跃" if session["lifecycle"] == "active" else "可恢复"
                lines.append(
                    f"{index} · {short_text(session['title'], 24)} · {state}"
                )
            lines.append("3 · 新建固定会话")
        else:
            lines.append("还没有固定会话")
            lines.append("3 · 新建固定会话")
        lines.extend(["", "0 · 返回"])
        return "\n".join(lines)

    if active_job is not None:
        job_line = (
            f"当前任务 · "
            f"{JOB_LABELS.get(active_job['status'], active_job['status'])}"
        )
    else:
        job_line = "空闲"

    return "\n".join([
        f"🖥 固定会话 · {short_text(active_session['title'], 24)}",
        _online_label(online, health),
        f"{job_line} · {turn_count} 轮",
        "",
        "直接发送任务",
        "1 · 查看状态",
        "2 · 中断当前任务",
        "3 · 新建固定会话",
        "0 · 返回并暂停",
    ])


def render_pc_fixed_new(online, health=None):
    return "\n".join([
        "🖥 新建固定会话",
        "",
        _online_label(online, health),
        "",
        "第一条消息会创建会话并作为初始任务。",
        "之后的自由文本会继续同一个 Codex Thread。",
        "",
        "0 · 取消",
    ])


def render_pc_job_status(job):
    if job is None:
        return "🖥 当前没有执行中的任务\n\n继续发送任务即可"
    status = JOB_LABELS.get(job["status"], job["status"])
    lines = [
        "🖥 本机任务",
        "",
        f"状态：{status}",
        f"内容：{short_text(job['prompt'], 100)}",
    ]
    if job["error_code"]:
        lines.append(f"错误：{job['error_code']}")
    lines.extend(["", "发送 2 · 中断任务"])
    return "\n".join(lines)


def render_pc_history(rows, page_index=1, total=0, page_size=6):
    if not rows:
        return "📜 本机任务历史\n\n还没有任务记录\n\n0 · 返回"

    lines = ["📜 本机任务历史", ""]
    for index, row in enumerate(rows, 1):
        status = JOB_LABELS.get(row["status"], row["status"])
        lines.append(
            f"{index} · {short_text(row['prompt'], 26)}"
            f" · {status}"
        )
    pages = max(1, (total + page_size - 1) // page_size)
    lines.extend([
        "",
        "发送编号查看详情",
        f"第 {page_index}/{pages} 页 · 共 {total} 条",
        "8 上一页 · 9 下一页 · 0 · 返回",
    ])
    return "\n".join(lines)


def render_pc_history_detail(job):
    if job is None:
        return "😅 找不到这条任务记录\n\n0 · 返回"
    status = JOB_LABELS.get(job["status"], job["status"])
    created = _time_label(job["created_at_utc"])
    finished = _time_label(job["finished_at_utc"])
    lines = [
        "🖥 本机任务",
        "",
        f"状态：{status}",
        f"创建：{created}",
    ]
    if finished:
        lines.append(f"结束：{finished}")
    lines.extend([
        "",
        f"内容：{short_text(job['prompt'], 180)}",
    ])
    if job["result_summary"]:
        lines.extend([
            "",
            "结果：",
            short_text(job["result_summary"], 1200),
        ])
    if job["error_code"]:
        lines.append(f"\n错误：{job['error_code']}")
    lines.extend(["", "0 · 返回"])
    return "\n".join(lines)


def completion_message(job):
    status = JOB_LABELS.get(job["status"], job["status"])
    result = (job["result_summary"] or "").strip()
    if len(result) > 3500:
        result = result[:3499] + "…"
    title = f"🖥 本机 Codex · {status}"
    if result:
        return f"{title}\n\n{result}"
    if job["error_code"]:
        return f"{title}\n\n错误：{job['error_code']}"
    return title
