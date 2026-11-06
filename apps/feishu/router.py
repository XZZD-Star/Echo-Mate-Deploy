"""Command and page routing for the Feishu bot."""

import re
from datetime import datetime

from echomate_core import notify as notify_store
from echomate_core import store as echomate_store
from feishu_bot.pc_routing import PC_PAGES

from .config import LOCAL_TZ, OWNER_OPEN_ID, PAGE_UNKNOWN_HINT
from .features import _add_todo, _handle_pc_context, _handle_todo_action, _parse_natural_todo
from .navigation import _enter, _handle_pagination, _navigate_back
from .storage import _pc_control_enabled, get_page, get_page_state, set_page
from .views import (
    _memory_candidate_display_index,
    normalize_hhmm as _normalize_hhmm,
    render_status,
    render_status_detail,
    render_todo_new,
)


def _toggle(kind_key, target):
    current = notify_store.get_setting(kind_key, "1")
    new_value = "0" if current == "1" else "1"
    notify_store.set_setting(kind_key, new_value)
    state_text = "关闭" if new_value == "0" else "开启"
    return f"✅ {target}播报{state_text}\n\n4 播报记录 · 0 返回"


def _handle_page_number(conn, chat_id, page, text):
    """页面内的数字指令。返回 None 表示不归本页处理。"""
    if page == "menu":
        target = {
            "1": "status",
            "2": "todo",
            "3": "todo_new",
            "4": "memory_hub",
            "5": "help",
            "6": "notify",
            "7": "pc",
        }.get(text)
        if target:
            if target == "pc" and not _pc_control_enabled(conn):
                return "🖥 本机 Codex 暂时关闭"
            return _enter(conn, chat_id, target)

    elif page == "status":
        if text == "1":
            return render_status()
        if text == "2":
            return _enter(conn, chat_id, "status_detail")

    elif page == "status_detail":
        if text == "1":
            return render_status_detail()

    elif page == "todo":
        if text == "3":
            return _enter(conn, chat_id, "todo_new")
        if text == "4":
            return _enter(conn, chat_id, "todo_history")

    elif page == "notify":
        if text == "1":
            return _enter(conn, chat_id, "quiet")
        if text == "2":
            return _toggle("notify_todo_enabled", "待办")
        if text == "3":
            return _toggle("notify_monitor_enabled", "监控")
        if text == "4":
            return _enter(conn, chat_id, "records")

    elif page == "memory_hub":
        if text == "1":
            return _enter(conn, chat_id, "memory")
        if text == "2":
            return _enter(conn, chat_id, "chat_memory")

    return None


def handle_command(conn, text, chat_id, event_id=None):
    text = text.strip()

    # Global escape commands must work from every page, including PC capture.
    if text in {"菜单", "功能"}:
        return _enter(conn, chat_id, "menu", mode="reset")
    if text == "0":
        return _navigate_back(conn, chat_id)

    page = get_page(chat_id, conn=conn)

    paged = _handle_pagination(conn, chat_id, text)
    if paged is not None:
        return paged

    if page in PC_PAGES:
        return _handle_pc_context(
            conn, chat_id, page, text, event_id=event_id
        )

    todo_action = _handle_todo_action(conn, text, chat_id)
    if todo_action is not None:
        return todo_action

    match = re.match(r"^(确认|取消)候选\s*(\d+)\s*$", text)
    if match:
        action, index_text = match.groups()
        display_index = int(index_text)
        candidates = echomate_store.list_memory_candidates(
            OWNER_OPEN_ID, 10, conn=conn
        )
        if 1 <= display_index <= len(candidates):
            candidate_id = candidates[display_index - 1]["id"]
            decision = "accept" if action == "确认" else "reject"
            if echomate_store.decide_memory_candidate(
                OWNER_OPEN_ID, candidate_id, decision, conn=conn
            ):
                result = "写入" if action == "确认" else "取消"
                return f"✅ 候选记忆已{result}"
        return "没有找到这条候选记忆\n\n回复：记忆候选"

    match = re.match(r"^确认记忆\s*#?(\d+)\s*$", text)
    if match:
        candidate_id = int(match.group(1))
        if echomate_store.decide_memory_candidate(
            OWNER_OPEN_ID, candidate_id, "accept", conn=conn
        ):
            return "✅ 记忆已确认写入"
        return "没有找到这条候选记忆"

    match = re.match(r"^取消记忆\s*#?(\d+)\s*$", text)
    if match:
        candidate_id = int(match.group(1))
        if echomate_store.decide_memory_candidate(
            OWNER_OPEN_ID, candidate_id, "reject", conn=conn
        ):
            return "✅ 已取消候选记忆"
        return "没有找到这条候选记忆"

    if text in {"记忆候选", "待确认记忆"}:
        candidates = echomate_store.list_memory_candidates(
            OWNER_OPEN_ID, 10, conn=conn
        )
        if not candidates:
            return "📚 没有待确认的候选记忆"
        lines = [f"📝 待确认候选 {len(candidates)} 条", ""]
        for sequence, item in enumerate(candidates, 1):
            lines.append(f"{sequence}. {item['content'][:60]}")
        lines.extend([
            "",
            "确认：确认候选 1",
            "取消：取消候选 1",
        ])
        return "\n".join(lines)

    # -------- 页面内数字 --------

    if text.isdigit():
        handled = None
        if text in {"1", "2", "3", "4", "5", "6", "7"}:
            handled = _handle_page_number(conn, chat_id, page, text)
        if handled is not None:
            return handled
        if page != "menu":
            return PAGE_UNKNOWN_HINT.get(
                page,
                "😅 本页没有这个数字操作\n0 返回",
            )
        return "😅 主菜单没有这个编号\n请输入 1 到 7"

    # -------- 直接进入页面的文字命令 --------

    if text in {
        "检查 VPS", "检查VPS", "VPS 状态", "VPS状态",
        "服务器状态", "状态",
    }:
        return _enter(conn, chat_id, "status", mode="reset")

    if text in {"状态详情", "详情"}:
        return _enter(conn, chat_id, "status_detail", mode="reset")

    if text == "帮助":
        return _enter(conn, chat_id, "help", mode="reset")

    if text in {"播报设置", "语音播报设置"}:
        return _enter(conn, chat_id, "notify", mode="reset")

    if text in {"播报记录", "语音播报记录"}:
        return _enter(conn, chat_id, "records", mode="reset")

    if text in {"我的待办", "待办列表", "查看待办", "待办"}:
        return _enter(conn, chat_id, "todo", mode="reset")

    if text in {"历史待办", "待办历史"}:
        return _enter(conn, chat_id, "todo_history", mode="reset")

    if text in {"记忆", "记忆中心"}:
        return _enter(conn, chat_id, "memory_hub", mode="reset")

    if text in {
        "列出记忆", "我的记忆", "记忆列表",
        "长期记忆", "长期偏好",
    }:
        return _enter(conn, chat_id, "memory", mode="reset")

    if text in {"聊天记忆", "聊天记忆列表"}:
        return _enter(conn, chat_id, "chat_memory", mode="reset")

    match = re.match(r"^删除聊天记忆\s*(\d+)\s*$", text)
    if match:
        display_index = int(match.group(1))
        state = get_page_state(chat_id, conn=conn)
        ids = state.get("chat_memory_ids") or []
        if not (1 <= display_index <= len(ids)):
            return "没有找到这条聊天记忆\n\n聊天记忆"
        if not echomate_store.delete_chat_memory(
            OWNER_OPEN_ID, ids[display_index - 1], conn=conn
        ):
            return "这条聊天记忆已经不存在\n\n聊天记忆"
        return (
            "🗑 已删除聊天记忆\n\n"
            + _enter(conn, chat_id, "chat_memory", mode="replace")
        )

    if text in {"添加待办", "新建待办"}:
        return _enter(conn, chat_id, "todo_new", mode="reset")

    if text in {"本机 Codex", "本机Codex", "电脑控制", "本机电脑"}:
        if not _pc_control_enabled(conn):
            return "🖥 本机 Codex 暂时关闭"
        return _enter(conn, chat_id, "pc", mode="reset")

    # -------- 静音设置 --------

    match = re.match(
        r"^静音\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})\s*$",
        text,
    )

    if match:
        start_text, end_text = match.groups()
        start = _normalize_hhmm(start_text)
        end = _normalize_hhmm(end_text)

        if start is None or end is None:
            return "😅 时间写成 23:00 08:00 这样\n\n0 返回"

        if start == end:
            return "😅 开始和结束不能一样\n\n0 返回"

        notify_store.set_setting("notify_quiet_start", start)
        notify_store.set_setting("notify_quiet_end", end)

        return (
            f"🔇 静音 {start}–{end}\n\n"
            "这段桌宠闭嘴，飞书照常\n\n"
            "0 返回"
        )

    if text in {"关闭静音", "取消静音"}:
        notify_store.set_setting("notify_quiet_start", "")
        notify_store.set_setting("notify_quiet_end", "")
        return "🔔 静音关掉了\n\n0 返回"

    match = re.match(r"^(关闭|开启)(待办|监控)播报$", text)

    if match:
        action, target = match.groups()
        key = (
            "notify_todo_enabled"
            if target == "待办"
            else "notify_monitor_enabled"
        )
        notify_store.set_setting(key, "1" if action == "开启" else "0")
        state_text = "开启" if action == "开启" else "关闭"
        return f"✅ {target}播报{state_text}\n\n0 返回"

    # -------- 创建待办 --------

    if text.startswith(("添加待办", "新建待办")):
        # A valid explicit command works anywhere. Invalid text falls through
        # to the current page or to the AI router instead of being intercepted.
        prefix_match = re.match(
            r"^(?:添加待办|新建待办)\s+"
            r"(\d{4}-\d{2}-\d{2})\s+"
            r"(\d{1,2})\s*[:：.．]\s*(\d{2})\s+(.+)$",
            text,
            re.DOTALL,
        )
        if prefix_match:
            date_text, hour, minute, title = prefix_match.groups()
            return _add_todo(
                conn, chat_id, date_text,
                f"{int(hour):02d}:{minute}", title,
            )

    match = re.match(
        r"^添加待办\s+(\d{4}-\d{2}-\d{2})\s+"
        r"(\d{2}:\d{2})\s+(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        date_text, time_text, title = match.groups()
        return _add_todo(conn, chat_id, date_text, time_text, title)

    # -------- 完成或取消待办 --------

    match = re.match(
        r"^(完成待办|取消待办)\s*[:：#]?\s*(\d+)\s*$",
        text,
    )

    if match:
        action, todo_id_text = match.groups()
        todo_id = int(todo_id_text)
        new_status = "completed" if action == "完成待办" else "cancelled"

        cursor = conn.execute(
            """
            UPDATE todos
            SET status = ?
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (new_status, todo_id, OWNER_OPEN_ID),
        )

        if not cursor.rowcount:
            return "没有找到这条待办\n\n发送：待办"

        if action == "取消待办":
            return (
                "🧹 待办已取消\n\n"
                + _enter(conn, chat_id, "todo", mode="replace")
            )

        return "✅ 待办已完成\n\n" + _enter(
            conn, chat_id, "todo", mode="replace"
        )

    # -------- 保存记忆 --------

    match = re.match(
        r"^(?:记住|保存记忆)(?:\s*[:：]\s*|\s+)(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        content = match.group(1).strip()

        if not content:
            return "😅 要记的内容是空的\n\n4 看记忆 · 0 返回"

        if len(content) > 2000:
            return "😅 太长了，2000 字以内\n\n4 看记忆 · 0 返回"

        candidate_id, result = echomate_store.enqueue_memory_candidate(
            OWNER_OPEN_ID, content, source="feishu", conn=conn
        )
        if result == "created":
            display_index = _memory_candidate_display_index(
                conn, candidate_id
            )
            return (
                "📝 已生成记忆候选\n\n"
                f"{content[:150]}\n\n"
                f"确认：确认候选 {display_index}\n"
                f"取消：取消候选 {display_index}"
            )
        if result == "duplicate":
            return "ℹ️ 已有相同候选记忆\n\n回复：记忆候选"
        if result == "too_long":
            return "😅 太长了，2000 字以内"
        return "😅 要记的内容是空的"

    # -------- 搜索记忆 --------

    match = re.match(
        r"^(?:查记忆|搜索记忆)\s*[:：]?\s*(.+)$",
        text,
        re.DOTALL,
    )

    if match:
        keyword = match.group(1).strip()

        rows = conn.execute(
            """
            SELECT id, content
            FROM memories
            WHERE owner_id = ? AND content LIKE ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (OWNER_OPEN_ID, f"%{keyword}%"),
        ).fetchall()

        if not rows:
            return (
                f"😅 没找到「{keyword}」\n\n"
                "1 全部 · 0 返回"
            )

        lines = [f"🔎 找到 {len(rows)} 条", ""]
        for sequence, (_, content) in enumerate(rows, 1):
            lines.append(f"{sequence}. {content[:120]}")
        lines.extend(["", "4 看记忆 · 0 返回"])
        return "\n".join(lines)

    # -------- 删除记忆 --------

    match = re.match(r"^删除第\s*(\d+)\s*条(?:记忆)?$", text)
    if match:
        display_index = int(match.group(1))
        state = get_page_state(chat_id, conn=conn)
        memory_ids = state.get("memory_ids") or []
        if display_index < 1 or display_index > len(memory_ids):
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        memory_id = int(memory_ids[display_index - 1])
        row = conn.execute(
            "SELECT content FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, OWNER_OPEN_ID),
        ).fetchone()
        if row is None:
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        content = row[0]
        if not echomate_store.delete_memory(
            OWNER_OPEN_ID, memory_id, conn=conn
        ):
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        return (
            "✅ 已删除这条记忆\n\n"
            f"{content[:40]}\n\n"
            "4 看记忆 · 0 返回"
        )

    match = re.match(r"^删除记忆\s*[:：#]?\s*(\d+)\s*$", text)

    if match:
        memory_id = int(match.group(1))
        row = conn.execute(
            "SELECT content FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, OWNER_OPEN_ID),
        ).fetchone()

        if not row:
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"

        if not echomate_store.delete_memory(
            OWNER_OPEN_ID, memory_id, conn=conn
        ):
            return "没有找到这条记忆\n\n4 看记忆 · 0 返回"
        preview = row[0][:40]
        return (
            "✅ 已删除这条记忆\n\n"
            f"{preview}\n\n"
            "4 看记忆 · 0 返回"
        )

    # 清空全部记忆需要两步确认，避免误触。
    if text in {"清空记忆", "删除全部记忆"}:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not total:
            return "📚 本来就没有记忆\n\n0 返回"

        set_page(chat_id, "memory_clear_confirm", conn=conn)
        return (
            f"⚠️ 要清空全部 {total} 条记忆？\n\n"
            "删除后无法恢复。\n"
            "确认发：确认清空记忆\n"
            "取消发：0\n\n"
            "0 取消"
        )

    if text == "确认清空记忆":
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not total:
            set_page(chat_id, "memory", conn=conn)
            return "📚 本来就没有记忆\n\n0 返回"

        removed = echomate_store.clear_memories(
            OWNER_OPEN_ID, conn=conn
        )
        set_page(chat_id, "memory", conn=conn)
        return (
            f"✅ 清空了 {removed} 条记忆\n\n"
            "以后想存新的，发：记住 内容\n\n"
            "0 返回"
        )

    # -------- 页面上下文：PC 页面已在入口处分流 --------

    if page == "todo_new":
        parsed = _parse_natural_todo(text, allow_time_only=True)
        if parsed and parsed[0] == "past":
            return (
                "😅 这个时间不能早于当前时间\n\n"
                f"现在是 {datetime.now(LOCAL_TZ):%Y-%m-%d %H:%M}\n"
                "请填写之后的时间\n\n"
                "直接按格式重发 · 0 返回"
            )
        if parsed is not None:
            date_text, time_text, title = parsed
            return _add_todo(conn, chat_id, date_text, time_text, title)
        return render_todo_new() + "\n\n😅 这次没解析成功，请按上面的格式重发。"

    # Outside todo_new, free text belongs to the AI tool router.
    return None
