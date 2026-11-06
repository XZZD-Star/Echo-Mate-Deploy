import re

from ..constants import PAGE_CARD_CONFIG, PAGE_CARD_DROP_FIRST
from .memory import (
    _build_chat_memory_page,
    _build_memory_page,
    render_memory_hub,
)
from .notify import _build_records_page, render_notify, render_quiet
from .status import menu_text, render_help, render_status, render_status_detail
from .todo import (
    _build_todo_history_page,
    _build_todo_page,
    render_todo_new,
)

def build_page_card(page, text):
    title, template = PAGE_CARD_CONFIG.get(page, ("Echo-Mate", "blue"))
    lines = str(text or "").strip().splitlines()

    if lines and lines[0].strip() in PAGE_CARD_DROP_FIRST:
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]

    footer = "回复编号继续 · 0 返回"
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if line and ("0 返回" in line or "0 取消" in line):
            footer = line
            del lines[index]
            break
    while lines and not lines[-1].strip():
        lines.pop()

    body = "\n".join(lines).strip()
    if page == "menu":
        body = re.sub(
            r"^(\d+) · (.*)$",
            r"**\1** · \2",
            body,
            flags=re.MULTILINE,
        )
    elif page == "status":
        status_lines = body.splitlines()
        if status_lines:
            status_lines[0] = f"**{status_lines[0]}**"
        for index, line in enumerate(status_lines):
            if line.startswith("核心服务 "):
                status_lines[index] = f"**{line}**"
        body = "\n".join(status_lines)
    if page in {"status", "status_detail"}:
        if "🔴" in body or "⚠️" in body:
            template = "orange"
        elif "🟢" in body or "✅" in body:
            template = "green"

    elements = []
    if body:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": body},
        })
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": footer,
            }],
        },
    ])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def render_page(conn, chat_id, page, page_index=1):
    if page == "menu":
        return menu_text()
    if page == "status":
        return render_status()
    if page == "status_detail":
        return render_status_detail()
    if page == "todo":
        return _build_todo_page(conn, page_index)[0]
    if page == "todo_new":
        return render_todo_new()
    if page == "todo_history":
        return _build_todo_history_page(conn, page_index)[0]
    if page == "memory_hub":
        return render_memory_hub(conn)
    if page == "memory":
        return _build_memory_page(conn, page_index)[0]
    if page == "chat_memory":
        return _build_chat_memory_page(conn, page_index)[0]
    if page == "help":
        return render_help()
    if page == "notify":
        return render_notify()
    if page == "quiet":
        return render_quiet()
    if page == "records":
        return _build_records_page(page_index)[0]
    if page == "memory_clear_confirm":
        return (
            "⚠️ 清空全部记忆？\n\n"
            "删除后无法恢复。\n"
            "确认发：确认清空记忆\n"
            "取消发：0"
        )
    return menu_text()
