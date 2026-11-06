import json
import re
import time
from datetime import datetime, timezone

import requests

from echomate_core import actions as assistant_actions
from echomate_core import store as echomate_store
from echomate_core import tools as assistant_tools
from echomate_core.db import open_connection

from ..runtime import (
    CURRENT_CHAT_ID,
    DB_PATH,
    LOCAL_TZ,
    OWNER_OPEN_ID,
    SEARCH_CACHE_TTL,
    TAVILY_API_KEY,
    TAVILY_URL,
)
from ..time_utils import format_due_friendly

def _tool_list_todos(args):
    conn = open_connection(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT id, title, due_at_utc
            FROM todos
            WHERE owner_id = ? AND status = 'pending'
            ORDER BY due_at_utc ASC
            LIMIT 20
            """,
            (OWNER_OPEN_ID,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "ok": True,
        "count": len(rows),
        "items": [
            {
                "id": todo_id,
                "title": title,
                "due": format_due_friendly(due_utc),
            }
            for todo_id, title, due_utc in rows
        ],
    }


def _tool_search_memories(args):
    keyword = (args.get("keyword") or "").strip()

    conn = open_connection(DB_PATH)
    try:
        if keyword:
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE owner_id = ? AND content LIKE ?
                ORDER BY id DESC LIMIT 10
                """,
                (OWNER_OPEN_ID, f"%{keyword}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, content FROM memories
                WHERE owner_id = ?
                ORDER BY id DESC LIMIT 10
                """,
                (OWNER_OPEN_ID,),
            ).fetchall()
    finally:
        conn.close()

    return {
        "ok": True,
        "count": len(rows),
        "items": [
            {"id": memory_id, "content": content}
            for memory_id, content in rows
        ],
    }


def _tool_add_todo(args):
    title = (
        args.get("title")
        or args.get("content")
        or args.get("description")
        or ""
    ).strip()
    due_text = (args.get("due_at") or "").strip()
    due = assistant_actions._parse_local_datetime(due_text)
    if not title:
        return {"ok": False, "error": "empty", "due_at": due_text}
    if due is None:
        return {"ok": False, "error": "bad_due", "due_at": due_text}

    now_local = datetime.now(LOCAL_TZ)
    readable_due = due.strftime("%Y-%m-%d %H:%M")
    if due <= now_local:
        return {
            "ok": False,
            "error": "past_due",
            "due_text": readable_due,
            "current_time": now_local.strftime("%Y-%m-%d %H:%M"),
        }

    todo_id, result = echomate_store.create_todo(
        OWNER_OPEN_ID,
        title,
        due.astimezone(timezone.utc).isoformat(timespec="seconds"),
        source="feishu",
        chat_id=CURRENT_CHAT_ID.get(),
        notify_deskbot=True,
        notify_feishu=True,
    )
    if result not in ("created", "duplicate"):
        response = {
            "ok": False,
            "error": result,
            "due_text": readable_due,
        }
        if result == "past_due":
            response["current_time"] = datetime.now(LOCAL_TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
        return response
    return {
        "ok": True,
        "todo_id": todo_id,
        "duplicate": result == "duplicate",
        "title": title,
        "due_text": due.strftime("%Y-%m-%d %H:%M"),
    }


def _tool_propose_memory(args):
    content = (args.get("content") or "").strip()
    category = (args.get("category") or "").strip() or None
    candidate_id, result = echomate_store.enqueue_memory_candidate(
        OWNER_OPEN_ID,
        content,
        source="feishu_ai",
        category=category,
    )
    return {
        "ok": result in ("created", "duplicate"),
        "candidate_id": candidate_id,
        "result": result,
    }


def _tool_web_search(args):
    """调用 Tavily 搜索，返回标题、链接、摘要。"""
    query = (args.get("query") or "").strip()

    if not query:
        return {"ok": False, "error": "缺少搜索关键词"}

    if not TAVILY_API_KEY:
        return {"ok": False, "error": "未配置 TAVILY_API_KEY"}

    max_results = args.get("max_results") or 5
    try:
        max_results = max(1, min(int(max_results), 8))
    except (TypeError, ValueError):
        max_results = 5

    cache_key = f"{query.lower()}|{max_results}"
    cached = _search_cache.get(cache_key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_TTL:
        return cached[1]

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "include_answer": True,
        "search_depth": "basic",
    }

    try:
        response = requests.post(TAVILY_URL, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        print(f"[搜索] 请求失败：{type(exc).__name__}", flush=True)
        return {"ok": False, "error": "搜索服务暂时不可用"}

    results = []
    for item in (data.get("results") or [])[:max_results]:
        results.append({
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "content": (item.get("content") or "")[:1200],
        })

    if not results:
        return {"ok": False, "error": f"没有搜到「{query}」的结果"}

    result = {
        "ok": True,
        "query": query,
        "answer": (data.get("answer") or "")[:800],
        "results": results,
    }

    _search_cache[cache_key] = (time.time(), result)

    # 简单限长，避免缓存无限增长。
    if len(_search_cache) > 50:
        oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
        _search_cache.pop(oldest, None)

    return result


def _run_tool(name, arguments):
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"未知工具：{name}"}

    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}

    try:
        result = handler(args)
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        print(f"[工具] {name} ok={str(ok).lower()}", flush=True)
        return result
    except Exception as exc:
        print(f"[工具] {name} 执行失败：{type(exc).__name__}", flush=True)
        return {"ok": False, "error": "工具执行失败"}


_search_cache = {}

TOOL_HANDLERS = {
    "get_weather": lambda args: assistant_tools.get_weather(
        args, OWNER_OPEN_ID
    ),
    "list_todos": lambda args: assistant_tools.list_todos(
        args, OWNER_OPEN_ID
    ),
    "add_todo": _tool_add_todo,
    "propose_memory": _tool_propose_memory,
    "search_memories": lambda args: assistant_tools.search_memories(
        args, OWNER_OPEN_ID
    ),
    "web_search": assistant_tools.web_search,
}
