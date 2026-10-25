#!/usr/bin/env python3
"""向真实飞书发送一条新菜单，确认渲染效果。"""

import json
import sys
import types

import lark_oapi as lark
from dotenv import load_dotenv
import os

load_dotenv("/root/echomate-deploy/feishu_bot/.env")

PATH = "/root/echomate-deploy/feishu_bot/bot.py"
src = open(PATH, encoding="utf-8").read()
cut = src.index("# ==================== 飞书消息发送 ====================")

m = types.ModuleType("bot_head")
m.__file__ = PATH
exec(compile(src[:cut], PATH, "exec"), m.__dict__)

text = m.menu_text()
print("--- 即将发送 ---")
print(text)
print("---------------")

client = (
    lark.Client.builder()
    .app_id(os.environ["LARK_APP_ID"])
    .app_secret(os.environ["LARK_APP_SECRET"])
    .build()
)

request = (
    lark.im.v1.CreateMessageRequest.builder()
    .receive_id_type("chat_id")
    .request_body(
        lark.im.v1.CreateMessageRequestBody.builder()
        .receive_id("oc_5b3d6c4b7569f760854044c0c5b7ba0d")
        .msg_type("text")
        .content(json.dumps({"text": text}, ensure_ascii=False))
        .build()
    )
    .build()
)

response = client.im.v1.message.create(request)
print("success =", response.success(), "code =", response.code)
