import threading

import lark_oapi as lark

from echomate_core import notify as notify_store
from echomate_core import store as echomate_store
from echomate_core.schema import init_core_schema
from feishu_bot import pc_control_service, pc_control_store

from .handlers import on_message
from .messaging import send_text
from .runtime import APP_ID, APP_SECRET, DB_PATH
from .schema import init_chat_table, init_db
from .worker import reminder_loop

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
