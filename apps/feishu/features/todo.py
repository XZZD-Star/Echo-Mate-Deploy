import re
from datetime import datetime, timedelta, timezone

from echomate_core import store as echomate_store

from ..navigation import _enter
from ..page_state import get_page_state
from ..runtime import LOCAL_TZ, OWNER_OPEN_ID
from ..time_utils import format_due_friendly

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
