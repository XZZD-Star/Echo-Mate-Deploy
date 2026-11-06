"""Low-level Feishu message transport."""

import json

import lark_oapi as lark


def send_text(api_client, receive_id, text, receive_id_type="chat_id"):
    request = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = api_client.im.v1.message.create(request)
    if response.success():
        print("[飞书] 消息发送成功", flush=True)
        return True
    print(
        f"[飞书] 消息发送失败：code={response.code}, "
        f"msg={response.msg}, log_id={response.get_log_id()}",
        flush=True,
    )
    return False


def send_card(api_client, receive_id, card, receive_id_type="chat_id"):
    request = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = api_client.im.v1.message.create(request)
    if response.success():
        print("[飞书] 卡片发送成功", flush=True)
        return True
    print(
        f"[飞书] 卡片发送失败：code={response.code}, "
        f"msg={response.msg}, log_id={response.get_log_id()}",
        flush=True,
    )
    return False


__all__ = ["send_card", "send_text"]
