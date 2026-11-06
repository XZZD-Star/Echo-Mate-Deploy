"""Feishu application handlers and startup."""

import json
import re
import threading

import lark_oapi as lark

from echomate_core import memory as memory_store
from echomate_core import notify as notify_store
from echomate_core import store as echomate_store
from echomate_core.db import connect as sqlite_connect
from echomate_core.schema import init_core_schema
from feishu_bot import pc_control_service, pc_control_store

from .ai import generate_ai_reply
from .config import (
    APP_ID,
    APP_SECRET,
    CURRENT_CHAT_ID,
    DB_PATH,
    OWNER_OPEN_ID,
    now_utc,
)
from .features import reminder_loop
from .router import handle_command
from .storage import get_page, init_chat_table, init_db
from .transport import send_page_card, send_text


def on_message(data: lark.im.v1.P2ImMessageReceiveV1):
    header = getattr(data, "header", None)
    event = getattr(data, "event", None)

    if event is None:
        return

    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)

    if message is None or sender is None:
        return

    # 私人记忆和待办仅在单聊中处理。
    if getattr(message, "chat_type", "") != "p2p":
        return

    if getattr(sender, "sender_type", "") != "user":
        return

    sender_id = getattr(sender, "sender_id", None)
    open_id = getattr(sender_id, "open_id", "")

    if open_id != OWNER_OPEN_ID:
        print("[安全] 已忽略非授权用户消息", flush=True)
        return

    if getattr(message, "message_type", "") != "text":
        return

    try:
        payload = json.loads(message.content or "{}")
        user_text = str(payload.get("text", "")).strip()
    except (json.JSONDecodeError, TypeError):
        return

    if not user_text:
        return

    event_id = getattr(header, "event_id", None)
    event_id = event_id or getattr(message, "message_id", None)

    if not event_id:
        print("[飞书] 事件缺少唯一 ID，已忽略", flush=True)
        return

    CURRENT_CHAT_ID.set(message.chat_id)
    reply = None
    reply_page = None

    try:
        with sqlite_connect(DB_PATH, write=True) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO processed_events
                    (event_id, processed_at)
                VALUES (?, ?)
                """,
                (event_id, now_utc()),
            )

            if cursor.rowcount == 0:
                print("[飞书] 重复事件已忽略", flush=True)
                return

        with sqlite_connect(DB_PATH, write=True) as conn:
            reply = handle_command(
                conn,
                user_text,
                message.chat_id,
                event_id=event_id,
            )
            if reply is not None:
                reply_page = get_page(message.chat_id, conn=conn)

    except Exception as exc:
        print(
            f"[数据库] 处理失败：{type(exc).__name__}",
            flush=True,
        )
        reply = "😅 这条消息保存失败，请稍后重试。\n\n不会重复创建，直接重发即可。"
    if reply is None:
        match = re.match(
            r"^(?:记住|保存记忆)(?:\s*[:：]\s*|\s+)(.+)$",
            user_text,
            re.DOTALL,
        )
        if match:
            candidate_id, result = memory_store.enqueue_candidate(
                OWNER_OPEN_ID, match.group(1).strip()
            )
            if result == "created":
                reply = (
                    "📝 已生成记忆候选\n"
                    "回复：记忆候选\n"
                    "查看并确认"
                )
            elif result == "duplicate":
                reply = "已有相同候选记忆\n回复：记忆候选"
            elif result == "too_long":
                reply = "😅 太长了，2000 字以内"
            else:
                reply = "😅 要记的内容是空的"

    if reply is None:
        reply = generate_ai_reply(
            message.chat_id,
            user_text,
            turn_id=event_id,
        )

    if reply:
        try:
            if reply_page:
                send_page_card(message.chat_id, reply_page, reply)
            else:
                send_text(message.chat_id, reply)
        except Exception as exc:
            print(
                f"[飞书] 回复异常：{type(exc).__name__}",
                flush=True,
            )


def main():
    echomate_store.DB_PATH = DB_PATH
    notify_store.DB_PATH = DB_PATH
    init_db()
    init_chat_table()
    init_core_schema()
    pc_control_store.init_schema()
    pc_control_store.pause_all_active_sessions()

    threading.Thread(
        target=reminder_loop,
        name="todo-reminder",
        daemon=True,
    ).start()

    pc_control_service.start_worker(send_text)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
    )

    print("[记忆助手] SQLite 初始化完成", flush=True)
    print("[记忆助手] 记忆和待办仅允许授权账号私聊操作", flush=True)
    print(f"[记忆助手] 数据库位置：{DB_PATH}", flush=True)
    print("[飞书] 正在连接开放平台……", flush=True)

    client.start()


if __name__ == "__main__":
    main()
