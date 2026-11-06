import tempfile
import uuid
from pathlib import Path

from echomate_core.db import connect as sqlite_connect

import bot
from echomate_core import store as echomate_store


with tempfile.TemporaryDirectory() as temp_dir:
    db_path = Path(temp_dir) / "assistant.db"
    bot.DB_PATH = db_path
    echomate_store.DB_PATH = db_path
    bot.pc_control_store.DB_PATH = db_path

    bot.init_db()
    bot.init_chat_table()
    echomate_store.init_schema()
    bot.pc_control_store.init_schema()
    with sqlite_connect(db_path, write=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
            """
        )

    owner = bot.OWNER_OPEN_ID
    chat_id = f"routing-test-{uuid.uuid4().hex}"

    with sqlite_connect(db_path, write=True) as conn:
        reply = bot.handle_command(conn, "7", chat_id, event_id="route-1")
        assert "本机 Codex" in reply, repr(reply)
        assert bot.get_page(chat_id, conn=conn) == "pc"

        reply = bot.handle_command(
            conn, "今天天气怎么样", chat_id, event_id="route-2"
        )
        assert "这里没有这个编号" in reply
        assert bot.get_page(chat_id, conn=conn) == "pc"

        reply = bot.handle_command(
            conn, "菜单", chat_id, event_id="route-menu"
        )
        assert "Echo-Mate" in reply
        assert bot.get_page(chat_id, conn=conn) == "menu"

        reply = bot.handle_command(
            conn, "7", chat_id, event_id="route-reenter"
        )
        assert "本机 Codex" in reply
        assert bot.get_page(chat_id, conn=conn) == "pc"

        reply = bot.handle_command(conn, "1", chat_id, event_id="route-3")
        assert "单次任务" in reply
        assert bot.get_page(chat_id, conn=conn) == "pc_single"

        reply = bot.handle_command(
            conn, "打印 hello", chat_id, event_id="route-4"
        )
        assert "单次任务" in reply
        assert bot.get_page(chat_id, conn=conn) == "pc_single"
        assert bot.pc_control_store.list_history(owner, chat_id) == []

        bot.handle_command(conn, "0", chat_id, event_id="route-5")
        assert bot.get_page(chat_id, conn=conn) == "pc"

        reply = bot.handle_command(conn, "2", chat_id, event_id="route-6")
        assert "固定会话" in reply
        assert bot.get_page(chat_id, conn=conn) == "pc_fixed"

        reply = bot.handle_command(
            conn, "普通文本", chat_id, event_id="route-7"
        )
        assert "恢复" in reply or "新建固定会话" in reply
        assert bot.get_page(chat_id, conn=conn) == "pc_fixed"

        bot.handle_command(conn, "0", chat_id, event_id="route-8")
        assert bot.get_page(chat_id, conn=conn) == "pc"
        bot.handle_command(conn, "0", chat_id, event_id="route-9")
        assert bot.get_page(chat_id, conn=conn) == "menu"

        session_id = bot.pc_control_store.create_session(
            owner,
            chat_id,
            bot.PC_DEVICE_ID,
            bot.PC_PROFILE_ID,
            "routing fixed session",
            conn=conn,
        )
        bot.pc_control_store.activate_session(
            owner, chat_id, session_id, conn=conn
        )
        bot._enter(conn, chat_id, "pc_fixed", mode="reset")
        assert (
            bot.pc_control_store.get_active_session(
                owner, chat_id, conn=conn
            )["id"]
            == session_id
        )
        bot.handle_command(conn, "0", chat_id, event_id="route-10")
        assert (
            bot.pc_control_store.get_session(
                session_id, conn=conn
            )["lifecycle"]
            == "paused"
        )

        bot.set_page(chat_id, "todo_new", conn=conn)
        reply = bot.handle_command(
            conn, "普通聊天文本", chat_id, event_id="route-11"
        )
        assert "新建待办" in reply
        assert "本机 Codex" not in reply

    print("bot_page_routing=ok")
