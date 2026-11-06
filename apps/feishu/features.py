"""Feishu todo, PC control and background reminder features."""

import re
import time
from datetime import datetime, timedelta, timezone

from echomate_core import store as echomate_store
from feishu_bot import pc_control_service, pc_control_store, pc_pages

from .config import (
    LOCAL_TZ,
    OWNER_OPEN_ID,
    PAGE_UNKNOWN_HINT,
    PC_DEVICE_ID,
    PC_PROFILE_ID,
    format_due_friendly,
)
from .navigation import _enter, _navigate_back, _render_page_state
from .storage import (
    _pc_control_enabled,
    get_page_state,
    set_page,
    state_nav_stack,
)
from .transport import send_text


def _add_todo(conn, chat_id, date_text, time_text, title):
    """创建待办，供严格格式与自然输入共用。"""
    title = (title or "").strip()

    if not title:
        return "😅 待办得写点内容\n\n直接按格式重发 · 0 返回"

    if len(title) > 200:
        return "😅 标题太长，200 字以内\n\n直接按格式重发 · 0 返回"

    try:
        due_local = datetime.strptime(
            f"{date_text} {time_text}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=LOCAL_TZ)
    except ValueError:
        return "😅 日期写成 2026-10-26 09:30 这样\n\n直接按格式重发 · 0 返回"

    if due_local <= datetime.now(LOCAL_TZ):
        return (
            "😅 这个时间不能早于当前时间\n\n"
            f"现在是 {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}\n"
            "请填写之后的时间\n\n"
            "直接按格式重发 · 0 返回"
        )

    due_utc = due_local.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )

    todo_id, result = echomate_store.create_todo(
        OWNER_OPEN_ID,
        title,
        due_utc,
        source="feishu",
        chat_id=chat_id,
        notify_deskbot=True,
        notify_feishu=True,
        conn=conn,
    )

    if result == "duplicate":
        return_message = (
            "ℹ️ 这条待办已经存在\n\n"
            f"{title} · {format_due_friendly(due_utc)}"
        )
        return return_message + "\n\n" + _enter(
            conn, chat_id, "todo", mode="replace"
        )

    if result != "created":
        return "😅 待办没保存成功，稍后再试\n\n直接按格式重发 · 0 返回"

    return_message = (
        "✅ 已记下\n\n"
        f"{title} · {format_due_friendly(due_utc)}\n"
        "到点我会提醒你"
    )
    return return_message + "\n\n" + _enter(
        conn, chat_id, "todo", mode="replace"
    )


def _parse_natural_todo(text, allow_time_only=False):
    """解析自然输入，返回 (日期, 时间, 标题)、("past", None, None, None) 或 None。

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
                return "past", None, None, None
            return (
                target.strftime("%Y-%m-%d"),
                f"{int(hour):02d}:{minute}",
                title.strip(),
            )

    return None


def _handle_todo_action(conn, text, chat_id):
    match = re.match(
        r"^(完成|取消)\s*(今天|明天)待办\s*#?(\d+)\s*$",
        text,
    )
    if not match:
        return None

    action, day, index_text = match.groups()
    state = get_page_state(chat_id, conn=conn)
    if state.get("page") != "todo":
        return "😅 请先进入待办页再操作\n\n发送：待办"

    today_key = datetime.now(LOCAL_TZ).date().isoformat()
    if state.get("day_key") != today_key:
        return (
            "😅 待办页已经跨天，已重新加载\n\n"
            + _enter(conn, chat_id, "todo", mode="replace")
        )

    id_key = "today_ids" if day == "今天" else "tomorrow_ids"
    todo_ids = state.get(id_key) or []
    index = int(index_text)
    if index < 1 or index > len(todo_ids):
        return (
            f"😅 没有找到{day}待办{index}\n\n"
            + _enter(conn, chat_id, "todo", mode="replace")
        )

    todo_id = int(todo_ids[index - 1])
    status = "completed" if action == "完成" else "cancelled"
    cursor = conn.execute(
        """
        UPDATE todos SET status = ?
        WHERE id = ? AND owner_id = ? AND status = 'pending'
        """,
        (status, todo_id, OWNER_OPEN_ID),
    )
    if not cursor.rowcount:
        return (
            f"😅 {day}待办{index} 已经变化\n\n"
            + _enter(conn, chat_id, "todo", mode="replace")
        )

    marker = "✅ 已完成" if action == "完成" else "🧹 已取消"
    return f"{marker}：{day}待办{index}\n\n" + _enter(
        conn, chat_id, "todo"
    )


def _pc_event_key(event_id):
    return event_id or f"pc-direct-{time.time_ns()}"


def _pc_job_ack(suffix=""):
    line = "✅ 已提交到本机 Codex\n正在等待电脑执行"
    if suffix:
        return f"{line}\n{suffix}"
    return line


def _handle_pc_context(conn, chat_id, page, text, event_id=None):
    if text == "0":
        return _navigate_back(conn, chat_id)

    if not _pc_control_enabled(conn):
        return "🖥 本机 Codex 暂时关闭\n\n0 返回"

    if len(text) > 20000:
        return "😅 任务太长了，20000 字以内"

    online, health = pc_control_service.health_snapshot()
    event_key = _pc_event_key(event_id)

    if page == "pc":
        target = {
            "1": "pc_single",
            "2": "pc_fixed",
            "3": "pc_history",
        }.get(text)
        if target:
            return _enter(conn, chat_id, target)
        return PAGE_UNKNOWN_HINT["pc"]

    if page == "pc_single":
        if not online:
            return pc_pages.render_pc_single(online, health)
        existing = pc_control_store.get_job_by_event(
            event_key, conn=conn
        )
        if existing is None:
            active_job = pc_control_store.get_active_job_for_device(
                PC_DEVICE_ID, conn=conn
            )
            if active_job is not None:
                return (
                    "🖥 电脑正在执行另一个任务\n\n"
                    "本机同一时间只执行一个任务。"
                )
            existing = pc_control_store.create_job(
                OWNER_OPEN_ID,
                chat_id,
                None,
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                "single",
                text,
                event_key,
                conn=conn,
            )
        home = _enter(conn, chat_id, "pc", mode="replace")
        return f"{_pc_job_ack()}\n\n{home}"

    if page == "pc_fixed_new":
        if not online:
            return pc_pages.render_pc_fixed_new(online, health)
        existing = pc_control_store.get_job_by_event(
            event_key, conn=conn
        )
        if existing is None:
            active_job = pc_control_store.get_active_job_for_device(
                PC_DEVICE_ID, conn=conn
            )
            if active_job is not None:
                return (
                    "🖥 电脑正在执行另一个任务\n\n"
                    "请等待完成或先中断当前任务。"
                )
            session_id = pc_control_store.create_session(
                OWNER_OPEN_ID,
                chat_id,
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                pc_pages.task_title(text),
                conn=conn,
            )
            pc_control_store.activate_session(
                OWNER_OPEN_ID, chat_id, session_id, conn=conn
            )
            existing = pc_control_store.create_job(
                OWNER_OPEN_ID,
                chat_id,
                session_id,
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                "fixed",
                text,
                event_key,
                conn=conn,
            )
        fixed_page = _enter(
            conn, chat_id, "pc_fixed", mode="replace"
        )
        return f"{_pc_job_ack()}\n\n{fixed_page}"

    if page == "pc_fixed":
        active = pc_control_store.get_active_session(
            OWNER_OPEN_ID, chat_id, conn=conn
        )
        if active is None:
            recoverable = pc_control_store.list_recoverable_sessions(
                OWNER_OPEN_ID, chat_id, conn=conn
            )
            if text in {"1", "2"}:
                index = int(text) - 1
                if index < len(recoverable):
                    pc_control_store.activate_session(
                        OWNER_OPEN_ID,
                        chat_id,
                        recoverable[index]["id"],
                        conn=conn,
                    )
                    return _enter(
                        conn, chat_id, "pc_fixed", mode="replace"
                    )
            if text == "3":
                return _enter(conn, chat_id, "pc_fixed_new")
            return (
                "🖥 请选择要恢复的会话，或发送 3 新建固定会话\n\n"
                "0 返回"
            )

        active_job = pc_control_store.get_active_job_for_device(
            PC_DEVICE_ID, conn=conn
        )

        if text == "1":
            return pc_pages.render_pc_job_status(active_job)

        if text == "2":
            if active_job is None:
                return "🖥 当前没有执行中的任务\n\n继续发送任务即可"
            requested = pc_control_store.request_job_cancel(
                active_job["id"],
                conn=conn,
            )
            if requested:
                return (
                    "🧹 已请求中断当前任务\n"
                    "后台会确认本机执行状态。"
                )
            return "🖥 这个任务已经结束，不需要中断"

        if text == "3":
            if active_job is not None:
                return "🖥 当前任务还在执行，先发送 2 中断"
            pc_control_store.pause_active_session(
                OWNER_OPEN_ID, chat_id, conn=conn
            )
            return _enter(conn, chat_id, "pc_fixed_new")

        if active_job is not None:
            state = pc_pages.JOB_LABELS.get(
                active_job["status"], active_job["status"]
            )
            return (
                f"🖥 当前任务仍在{state}\n\n"
                "发送 1 查看状态，或 2 中断"
            )

        if not online:
            return pc_pages.render_pc_fixed(
                active,
                pc_control_store.list_recoverable_sessions(
                    OWNER_OPEN_ID, chat_id, conn=conn
                ),
                active_job,
                online,
                health,
                pc_control_store.count_session_turns(
                    active["id"], conn=conn
                ),
            )

        existing = pc_control_store.get_job_by_event(
            event_key, conn=conn
        )
        if existing is None:
            existing = pc_control_store.create_job(
                OWNER_OPEN_ID,
                chat_id,
                active["id"],
                PC_DEVICE_ID,
                PC_PROFILE_ID,
                "fixed",
                text,
                event_key,
                conn=conn,
            )
        fixed_page = _render_page_state(
            conn, chat_id, "pc_fixed"
        )[0]
        return f"{_pc_job_ack()}\n\n{fixed_page}"

    if page == "pc_history":
        state = get_page_state(chat_id, conn=conn)
        job_ids = state.get("history_job_ids") or []
        if text.isdigit():
            index = int(text) - 1
            if 0 <= index < len(job_ids):
                job = pc_control_store.get_history_job(
                    job_ids[index],
                    OWNER_OPEN_ID,
                    chat_id,
                    conn=conn,
                )
                stack = state_nav_stack(state) + [
                    "pc_history_detail"
                ]
                set_page(
                    chat_id,
                    "pc_history_detail",
                    state={
                        "history_job_id": job_ids[index],
                        "nav_stack": stack,
                    },
                    conn=conn,
                )
                return pc_pages.render_pc_history_detail(job)
        return PAGE_UNKNOWN_HINT["pc_history"]

    if page == "pc_history_detail":
        return PAGE_UNKNOWN_HINT["pc_history_detail"]

    return PAGE_UNKNOWN_HINT.get(page, "🖥 本机 Codex\n\n0 返回")


def reminder_loop():
    print("[待办] 飞书提醒投递线程已启动，检查间隔 15 秒", flush=True)

    while True:
        try:
            delivery = echomate_store.claim_delivery("feishu")

            if delivery:
                todo_id = delivery["todo_id"]
                title = delivery["title"]
                chat_id = delivery["chat_id"]
                delivery_id = delivery["delivery_id"]

                if not chat_id:
                    # A deskbot-created todo has no Feishu chat target.
                    # Keep the shared record, but this channel cannot deliver it.
                    echomate_store.mark_delivery(
                        delivery_id, "skipped", "no Feishu chat_id"
                    )
                    echomate_store.finalize_todo_if_delivered(todo_id)
                else:
                    due_display = format_due_friendly(delivery["due_at_utc"])
                    content = (
                        f"⏰ {title}\n\n"
                        f"{due_display}\n\n"
                        "完成后打开待办清单操作"
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
                        echomate_store.mark_delivery(delivery_id, "sent")
                        echomate_store.finalize_todo_if_delivered(todo_id)
                    else:
                        echomate_store.mark_delivery(
                            delivery_id, "failed", "Feishu send failed"
                        )

            time.sleep(15)

        except Exception as exc:
            print(
                f"[待办] 调度异常：{type(exc).__name__}",
                flush=True,
            )
            time.sleep(5)
