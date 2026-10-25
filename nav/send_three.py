#!/usr/bin/env python3
"""向真实飞书依次发送主菜单、状态页、播报页，供验收。"""

import json
import os
import sqlite3
import sys
import time
import types

import lark_oapi as lark
from dotenv import load_dotenv

load_dotenv("/root/echomate-deploy/feishu_bot/.env")

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
DB = "/var/lib/echomate/assistant.db"
CHAT = "oc_5b3d6c4b7569f760854044c0c5b7ba0d"

src = open(PATH, encoding="utf-8").read()
cut = src.index("# ==================== 飞书消息发送 ====================")
m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)

client = (
    lark.Client.builder()
    .app_id(os.environ["LARK_APP_ID"])
    .app_secret(os.environ["LARK_APP_SECRET"])
    .build()
)


def send(text):
    request = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(CHAT)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = client.im.v1.message.create(request)
    print("sent:", response.success(), response.code)


def cmd(text):
    conn = sqlite3.connect(DB, timeout=15)
    try:
        result = m.handle_command(conn, text, CHAT)
        conn.commit()
        return result
    finally:
        conn.close()


cmd("菜单")
for text in [cmd("菜单"), cmd("1"), cmd("0"), cmd("6"), cmd("0")]:
    if text:
        send(text)
        time.sleep(1.2)

print("done")
