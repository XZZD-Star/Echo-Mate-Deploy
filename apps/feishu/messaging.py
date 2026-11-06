"""Business-level Feishu messaging helpers."""

from . import runtime
from .transport import send_card as _send_card
from .transport import send_text as _send_text
from .views.content import build_page_card


def send_text(receive_id, text, receive_id_type="chat_id"):
    return _send_text(
        runtime.api_client, receive_id, text, receive_id_type
    )


def send_card(receive_id, card, receive_id_type="chat_id"):
    return _send_card(
        runtime.api_client, receive_id, card, receive_id_type
    )


def send_page_card(chat_id, page, text):
    if send_card(chat_id, build_page_card(page, text)):
        return True
    return send_text(chat_id, text)


__all__ = ["send_card", "send_page_card", "send_text"]
