import time

from feishu_bot import pc_control_service, pc_control_store, pc_pages

from ..constants import PAGE_UNKNOWN_HINT
from ..navigation import _enter, _navigate_back
from ..page_state import (
    _pc_control_enabled,
    get_page_state,
    set_page,
    state_nav_stack,
)
from ..pages import _render_page_state
from ..runtime import OWNER_OPEN_ID, PC_DEVICE_ID, PC_PROFILE_ID

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
