import json
import re
import time
from datetime import datetime, timezone

import requests

from echomate_core import actions as assistant_actions
from echomate_core import store as echomate_store
from echomate_core import tools as assistant_tools
from echomate_core.db import open_connection
from feishu_bot import agent_runtime

from .ai_constants import (
    CHAT_TOOLS,
    MAX_TOOL_ROUNDS,
    MEMORY_CONTEXT_LIMIT,
    PLACE_STOPWORDS,
    PROVINCES,
    READ_ONLY_TOOLS,
    WEATHER_KEYWORDS,
    WEATHER_SUFFIXES,
    WMO_TEXT,
)
from .runtime import (
    CURRENT_CHAT_ID,
    DB_PATH,
    LOCAL_TZ,
    OWNER_OPEN_ID,
    QWEN_MODEL,
    SEARCH_CACHE_TTL,
    TAVILY_API_KEY,
    TAVILY_URL,
    memory_observer,
    qwen_client,
)
from .time_utils import format_due_friendly, now_utc

def _normalize_memory(text):
    """用于去重的归一化：去空白、去标点、统一小写。"""
    return re.sub(r"[\s，。！？、,.!?：:；;]+", "", text or "").lower()


def load_memory_context(limit=MEMORY_CONTEXT_LIMIT):
    """读取最近记忆，去重后返回列表。"""
    conn = open_connection(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT content FROM memories
            WHERE owner_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (OWNER_OPEN_ID, limit * 3),
        ).fetchall()
    finally:
        conn.close()

    seen = set()
    items = []

    for (content,) in rows:
        content = (content or "").strip()
        if not content:
            continue

        key = _normalize_memory(content)
        if not key or key in seen:
            continue

        seen.add(key)
        items.append(content)

        if len(items) >= limit:
            break

    return items


def memory_system_block(items):
    """把记忆整理成 system prompt 片段。"""
    if not items:
        return ""

    lines = "\n".join(f"- {item}" for item in items)
    return (
        "\n\n以下是用户主动让你记住的信息，回答时可以直接使用：\n"
        "<memory>\n"
        f"{lines}\n"
        "</memory>\n"
        "使用规则：\n"
        "1. 问题涉及用户个人属性（居住地、生日、职业、偏好、称呼等）时，"
        "必须使用上面的记忆，不要反问用户。\n"
        "2. 记忆里没有的信息，直接说不知道，不要编造。\n"
        "3. 记忆和问题确实无关时才可以忽略。"
    )


def _is_weather_question(text):
    return any(word in (text or "") for word in WEATHER_KEYWORDS)


def _extract_place(text):
    """从问句里抽地名；时间副词和疑问词不算地名。"""
    raw = (text or "").strip()

    cleaned = raw
    for word in (
        *PLACE_STOPWORDS,
        "这边", "那边", "会", "下", "有", "的", "吗", "呢", "啊",
        "？", "?", "，", ",", "。", ".", "！", "!",
    ):
        cleaned = cleaned.replace(word, " ")

    for token in re.split(r"\s+", cleaned):
        token = token.strip()
        if len(token) < 2:
            continue

        # 剩余 token 仍是时间/程度词时不算地名。
        if any(stop in token for stop in PLACE_STOPWORDS):
            continue

        return token

    return None


def _memory_places():
    """从记忆里抽居住地候选，按记忆新旧排序。"""
    candidates = []

    for item in load_memory_context():
        if not any(
            word in item for word in ("居住", "住在", "家住", "所在地", "城市")
        ):
            continue

        match = re.search(
            r"(?:居住地|住在|家住|所在地|城市)(?:是|在|为|：|:)?\s*"
            r"([\u4e00-\u9fa5]{2,20})",
            item,
        )
        if match:
            candidates.append(match.group(1))

    return candidates


def _place_candidates(place):
    """生成地名候选，覆盖「江西南昌」「南昌市」「南昌市西湖区」等写法。"""
    candidates = []

    def add(value):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    add(place)

    # 去掉行政后缀
    trimmed = place
    for suffix in WEATHER_SUFFIXES:
        if trimmed.endswith(suffix) and len(trimmed) > len(suffix):
            trimmed = trimmed[: -len(suffix)]
            add(trimmed)
            break

    # 去掉省级前缀：「江西南昌」→「南昌」
    for province in PROVINCES:
        if trimmed.startswith(province) and len(trimmed) > len(province):
            add(trimmed[len(province):])
            break

    # 「南昌市西湖区」这类：再截到区县
    for suffix in ("区", "县"):
        if trimmed.endswith(suffix):
            core = trimmed[: -len(suffix)]
            for province in PROVINCES:
                if core.startswith(province):
                    core = core[len(province):]
                    break
            add(core)
            break

    return candidates


def _geocode(place):
    """把地名换成经纬度，按候选列表逐个尝试。"""
    for candidate in _place_candidates(place):
        try:
            response = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={
                    "name": candidate,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
                timeout=8,
            )
            data = response.json()
        except Exception:
            continue

        results = data.get("results") or []
        if results:
            top = results[0]
            return {
                "name": top.get("name", candidate),
                "admin": top.get("admin1", ""),
                "lat": top.get("latitude"),
                "lon": top.get("longitude"),
            }

    return None


def _fetch_weather(place_info, days=1):
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place_info["lat"],
                "longitude": place_info["lon"],
                "current": "temperature_2m,relative_humidity_2m,weather_code",
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "weather_code,precipitation_probability_max"
                ),
                "timezone": "Asia/Shanghai",
                "forecast_days": days,
            },
            timeout=8,
        )
        return response.json()
    except Exception:
        return None


def weather_reply(text):
    """回答天气问题；无法回答时返回 None，交给普通聊天。"""
    if not _is_weather_question(text):
        return None

    asked_place = _extract_place(text)

    place_info = None
    asked_label = None

    # 先试问句里的地名；查不到不报错，回退记忆。
    if asked_place:
        place_info = _geocode(asked_place)
        if place_info:
            asked_label = asked_place

    if not place_info:
        memory_places = _memory_places()
        if memory_places:
            place_info = _geocode(memory_places[0])

    if not place_info:
        # 问句指了地名但没查到，且记忆也没有 → 这才提示。
        if asked_place:
            return (
                f"😅 没找到「{asked_place}」这个地方\n\n"
                "发「南昌天气」试试，或者发\n"
                "记住 我现在居住地是城市名\n"
            )

        return (
            "🌤 想知道哪里的天气？\n\n"
            "发「南昌天气」这样就行\n"
            "也可以先存一条居住地：\n"
            "记住 我现在居住地是江西南昌"
        )

    data = _fetch_weather(place_info)
    if not data:
        return "😅 天气服务暂时连不上，稍后再试"

    current = data.get("current") or {}
    daily = data.get("daily") or {}

    temp = current.get("temperature_2m")
    humidity = current.get("relative_humidity_2m")
    code = current.get("weather_code")

    if temp is None:
        return "😅 天气数据不完整，稍后再试"

    condition = WMO_TEXT.get(code, "未知")

    label = place_info["name"]
    admin = place_info.get("admin") or ""
    if (
        admin
        and admin not in label
        and label not in admin
    ):
        label = f"{admin}{label}"

    # 问的是「这几天/最近」时，带未来三天预报。
    wants_range = any(
        word in (text or "")
        for word in ("这几天", "这段时间", "最近", "未来")
    )

    if wants_range and len(daily.get("temperature_2m_max") or []) >= 3:
        highs = daily["temperature_2m_max"]
        lows = daily["temperature_2m_min"]
        codes = daily.get("weather_code") or []
        dates = daily.get("time") or []

        lines = [
            f"🌤 {label}未来三天",
            "",
        ]

        day_names = ["今天", "明天", "后天"]
        for index in range(3):
            day = day_names[index] if index < 3 else dates[index]
            day_condition = WMO_TEXT.get(
                codes[index] if index < len(codes) else -1, ""
            )
            lines.append(
                f"{day} {lows[index]:.0f}–{highs[index]:.0f}°C"
                f"{' · ' + day_condition if day_condition else ''}"
            )

        return "\n".join(lines)

    lines = [
        f"🌤 {label}天气",
        "",
        f"现在 {temp:.0f}°C · {condition}",
    ]

    if humidity is not None:
        lines[-1] += f" · 湿度 {humidity:.0f}%"

    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    if highs and lows:
        lines.append(f"今天 {lows[0]:.0f}–{highs[0]:.0f}°C")

    rain = daily.get("precipitation_probability_max") or []
    if rain and rain[0] is not None:
        lines.append(f"降水概率 {rain[0]:.0f}%")

    return "\n".join(lines)


def _tool_get_weather(args):
    place = (args.get("place") or "").strip()
    days = args.get("days") or 3

    try:
        days = max(1, min(int(days), 7))
    except (TypeError, ValueError):
        days = 3

    try:
        offset = max(0, min(int(args.get("day_offset") or 0), 6))
    except (TypeError, ValueError):
        offset = 0

    # 从今天开始取，保证 offset 之后的日期都在结果里。
    fetch_days = min(7, offset + days)

    place_info = None

    if place:
        place_info = _geocode(place)

    if not place_info:
        places = _memory_places()
        if places:
            place_info = _geocode(places[0])
            if place_info and not place:
                place = places[0]

    if not place_info:
        return {
            "ok": False,
            "error": (
                f"找不到地点「{place}」"
                if place
                else "没有地点，也没有记忆里的居住地"
            ),
        }

    data = _fetch_weather(place_info, days=fetch_days)
    if not data:
        return {"ok": False, "error": "天气服务暂时不可用"}

    current = data.get("current") or {}
    daily = data.get("daily") or {}

    label = place_info["name"]
    admin = place_info.get("admin") or ""
    if admin and admin not in label and label not in admin:
        label = f"{admin}{label}"

    forecast = []
    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    rains = daily.get("precipitation_probability_max") or []

    for index in range(offset, min(offset + days, len(dates))):
        forecast.append({
            "date": dates[index],
            "high_c": round(highs[index], 1) if index < len(highs) else None,
            "low_c": round(lows[index], 1) if index < len(lows) else None,
            "condition": WMO_TEXT.get(
                codes[index] if index < len(codes) else -1, "未知"
            ),
            "rain_chance": (
                round(rains[index]) if index < len(rains)
                and rains[index] is not None else None
            ),
        })

    return {
        "ok": True,
        "place": label,
        "now": {
            "temp_c": (
                round(current["temperature_2m"], 1)
                if current.get("temperature_2m") is not None else None
            ),
            "condition": WMO_TEXT.get(current.get("weather_code"), "未知"),
            "humidity": (
                round(current["relative_humidity_2m"])
                if current.get("relative_humidity_2m") is not None else None
            ),
        },
        "forecast": forecast,
    }


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


def _chat_with_tools(messages, tools):
    """跑工具调用循环，返回最终回复和真实工具执行记录。"""
    working = list(messages)
    tool_runs = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = qwen_client.chat.completions.create(
            model=QWEN_MODEL,
            messages=working,
            tools=tools,
            tool_choice="auto",
            max_tokens=700,
        )

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            content = message.content
            reply = content.strip() if isinstance(content, str) else ""
            return reply, tool_runs

        # 把助手的 tool_calls 原样放回对话，再逐个回填结果。
        working.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in tool_calls
            ],
        })

        for call in tool_calls:
            result = _run_tool(
                call.function.name, call.function.arguments
            )
            tool_runs.append(
                agent_runtime.normalize_tool_run(
                    call.function.name, result
                )
            )
            payload = json.dumps(result, ensure_ascii=False)

            # 搜索结果可能很长，提示模型这是唯一事实来源。
            if call.function.name == "web_search":
                payload = (
                    "以下是本次搜索的全部事实来源，"
                    "回答只能基于这些内容：\n" + payload
                )

            working.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": payload,
            })

    # 工具轮数用完，最后要一次纯文本回答。
    final = qwen_client.chat.completions.create(
        model=QWEN_MODEL,
        messages=working
        + [{
            "role": "user",
            "content": "请基于上面的工具结果直接回答，不要再调用工具。",
        }],
        max_tokens=700,
    )
    content = final.choices[0].message.content
    reply = content.strip() if isinstance(content, str) else ""
    return reply, tool_runs


def _execute_planned_actions(actions):
    tool_runs = []
    for action in actions:
        arguments = json.dumps(
            action["arguments"], ensure_ascii=False
        )
        result = _run_tool(action["name"], arguments)
        tool_runs.append(
            agent_runtime.normalize_tool_run(action["name"], result)
        )
    return tool_runs


def _compose_or_fallback(user_text, tool_runs):
    try:
        return agent_runtime.compose_grounded_reply(
            qwen_client,
            QWEN_MODEL,
            user_text,
            tool_runs,
            datetime.now(LOCAL_TZ),
        )
    except Exception as exc:
        print(
            f"[动作回复] 校验失败，使用保底文案：{type(exc).__name__}",
            flush=True,
        )
        return agent_runtime.fallback_action_reply(tool_runs)


def generate_ai_reply(chat_id, user_text, turn_id=""):
    """使用 Qwen 回复普通聊天，并保存近期上下文。"""
    if qwen_client is None:
        return "😅 AI 还没配好"

    if len(user_text) > 4000:
        return "😅 消息太长了，4000 字以内"

    # 先读取历史，再关闭数据库连接，避免网络请求期间占用数据库。
    conn = open_connection(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT role, content
            FROM chat_messages
            WHERE owner_id = ? AND chat_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (OWNER_OPEN_ID, chat_id),
        ).fetchall()
    finally:
        conn.close()

    history = [
        {"role": role, "content": content}
        for role, content in reversed(rows)
    ]
    safe_history = agent_runtime.sanitize_history(history)
    session_id = (
        f"feishu:{datetime.now(LOCAL_TZ).date().isoformat()}:{chat_id}"
    )

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Echo-Mate，用户的个人助手。"
                "你已连接共享待办和记忆。"
                f"当前北京时间是 {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}。"
                "用简体中文自然对话，像朋友聊天，简洁但有信息量。\n"
                "普通聊天只负责回答和只读查询；写操作由前置动作层执行。\n"
                "聊天历史只用于语境，不代表业务状态；"
                "只有在当前轮工具返回 ok=true 时才能声称操作完成。\n"
                "不要套固定格式，不要每次都列清单，"
                "不要加「0 返回」这类导航提示。\n\n"
                "你可以调用工具获取实时信息：\n"
                "- get_weather：天气、气温、下雨、穿衣、未来几天趋势\n"
                "- list_todos：用户有哪些待办\n"
                "- search_memories：翻找用户过去记过的事\n"
                "- web_search：联网查最新资料（新闻、价格、版本、赛事）\n"
                "需要数据时直接调用工具，然后基于结果自然回答；"
                "能对比、能建议、能解释原因就多说一句。\n"
                "用了 web_search 时，严格按下面的规则写：\n"
                "1. 只使用工具返回的 title / content / answer 里出现过的事实，"
                "不要添加任何工具结果里没有的新闻、公司名、金额或版本号。\n"
                "2. 来源链接只能从工具结果的 url 字段原样复制，"
                "禁止自己拼 URL 或改路径。\n"
                "3. 结果里信息不足就直说「搜到的资料有限」，不要补全。\n"
                "4. 如果多个结果说法冲突，指出冲突，不要挑一个当结论。\n"
                "5. 回答末尾附 1-3 个来源，格式：标题 - URL。\n"
                "工具返回 ok=false 时如实说明，不要编造数据。\n\n"
                "问天气时，如果用户说今天/明天/后天，"
                "调用 get_weather 并传对应 day_offset。"
                + assistant_actions.memory_prompt_block(
                    OWNER_OPEN_ID,
                    query=user_text,
                    session_id=session_id,
                )
            ),
        },
        *safe_history,
        {"role": "user", "content": user_text},
    ]

    now = datetime.now(LOCAL_TZ)
    tool_runs = []
    explicit_action = agent_runtime.parse_explicit_reminder(
        user_text, now
    )
    if explicit_action is not None:
        plan = {"actions": [explicit_action], "clarification": ""}
        print("[动作计划] 使用明确时间快速路径", flush=True)
    else:
        try:
            plan = agent_runtime.plan_actions(
                qwen_client,
                QWEN_MODEL,
                user_text,
                now,
            )
            print(
                f"[动作计划] actions={len(plan['actions'])}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[动作计划] 失败，异常类型={type(exc).__name__}",
                flush=True,
            )
            plan = {"actions": [], "clarification": ""}

    if plan["actions"]:
        tool_runs = _execute_planned_actions(plan["actions"])
        if all(run["ok"] for run in tool_runs):
            reply = _compose_or_fallback(user_text, tool_runs)
        else:
            reply = agent_runtime.fallback_action_reply(tool_runs)
    elif plan["clarification"]:
        reply = plan["clarification"]
    else:
        try:
            reply, tool_runs = _chat_with_tools(
                messages, READ_ONLY_TOOLS
            )
            if not reply:
                return "😅 这次没想出来，换个说法试试"

            if agent_runtime.looks_like_completion_claim(reply):
                reply = agent_runtime.false_action_reply()

        except Exception as exc:
            # 避免将请求内容或凭证写入日志。
            print(
                f"[Qwen] 请求失败，异常类型={type(exc).__name__}",
                flush=True,
            )
            return "😅 AI 卡了一下，稍后再试"

    # 只有成功获得回答后才保存本轮聊天。
    conn = open_connection(DB_PATH)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO chat_messages
                    (owner_id, chat_id, role, content, created_at_utc)
                VALUES (?, ?, 'user', ?, ?)
                """,
                (OWNER_OPEN_ID, chat_id, user_text, now_utc()),
            )
            conn.execute(
                """
                INSERT INTO chat_messages
                    (owner_id, chat_id, role, content, created_at_utc)
                VALUES (?, ?, 'assistant', ?, ?)
                """,
                (OWNER_OPEN_ID, chat_id, reply, now_utc()),
            )

            # 每个聊天只保留最近 12 条消息。
            conn.execute(
                """
                DELETE FROM chat_messages
                WHERE owner_id = ? AND chat_id = ?
                  AND id NOT IN (
                    SELECT id
                    FROM chat_messages
                    WHERE owner_id = ? AND chat_id = ?
                    ORDER BY id DESC
                    LIMIT 12
                  )
                """,
                (
                    OWNER_OPEN_ID, chat_id,
                    OWNER_OPEN_ID, chat_id,
                ),
            )
    finally:
        conn.close()

    if (
        memory_observer is not None
        and not any(
            run.get("tool") == "propose_memory" for run in tool_runs
        )
    ):
        memory_observer.enqueue_turn(
            owner_id=OWNER_OPEN_ID,
            session_id=session_id,
            turn_id=turn_id or now_utc(),
            source="feishu",
            user_text=user_text,
            tool_runs=tool_runs,
        )

    print("[回复] 已生成", flush=True)
    return reply


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
