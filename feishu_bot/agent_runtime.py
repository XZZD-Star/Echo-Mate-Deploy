"""Grounded write-action planning and response composition."""

import json
import re
from collections import Counter
from datetime import datetime, timedelta


WRITE_TOOLS = frozenset({"add_todo", "propose_memory"})

ACTION_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_action_plan",
        "description": (
            "提交本轮需要执行的数据库写操作。普通聊天、查询和咨询必须返回空 actions。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "enum": sorted(WRITE_TOOLS),
                            },
                            "arguments": {
                                "type": "object",
                                "description": "传给对应写工具的参数。",
                            },
                        },
                        "required": ["name", "arguments"],
                    },
                },
                "clarification": {
                    "type": "string",
                    "description": "时间或内容不完整时，需要向用户确认的问题。",
                },
            },
            "required": ["actions"],
        },
    },
}

GROUNDED_REPLY_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_grounded_reply",
        "description": "提交基于真实工具执行结果的用户回复。",
        "parameters": {
            "type": "object",
            "properties": {
                "reply": {"type": "string"},
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "enum": sorted(WRITE_TOOLS),
                            },
                            "ok": {"type": "boolean"},
                            "object_id": {
                                "type": "integer",
                            },
                        },
                        "required": ["tool", "ok"],
                    },
                },
            },
            "required": ["reply", "claims"],
        },
    },
}

_COMPLETION_PATTERNS = (
    re.compile(
        r"(?:已经|已|帮你|为你).{0,12}"
        r"(?:设置|创建|添加|保存|记录|安排|记住|取消|删除|完成)"
    ),
    re.compile(
        r"(?:设置|创建|添加|保存|安排|取消|删除|记住)"
        r"(?:好|成功|完成)?(?:了|啦)"
    ),
    re.compile(r"(?:会|将会|到时|到点).{0,20}(?:提醒|通知)你"),
)
_CLOCK_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[:：.．](\d{2})(?!\d)")
_CHINESE_CLOCK_PATTERN = re.compile(r"(\d{1,2})点(?:(\d{1,2})分?)?")
_OBJECT_ID_PATTERN = re.compile(r"#(\d+)")
_EXPLICIT_REMINDER_PATTERN = re.compile(
    r"^\s*(?:(今天|明天|后天)\s*)?"
    r"(?P<hour>\d{1,2})"
    r"(?:(?:[:：.．])(?P<minute>\d{0,2})"
    r"|点(?P<chinese_minute>\d{1,2})?分?)"
    r"\s*(?:的时候|时)?\s*"
    r"(?:提醒我|提醒|通知我|叫我)\s*"
    r"(?P<title>.+?)\s*$"
)


class AgentProtocolError(RuntimeError):
    """Raised when a structured agent response violates its contract."""


def _forced_tool_arguments(client, model, messages, tools, tool_name,
                           max_tokens):
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice={
            "type": "function",
            "function": {"name": tool_name},
        },
        max_tokens=max_tokens,
    )
    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None) or []
    call = next(
        (item for item in tool_calls if item.function.name == tool_name),
        None,
    )
    if call is None:
        raise AgentProtocolError(f"模型未调用 {tool_name}")
    try:
        arguments = json.loads(call.function.arguments or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise AgentProtocolError(f"{tool_name} 参数不是有效 JSON") from exc
    if not isinstance(arguments, dict):
        raise AgentProtocolError(f"{tool_name} 参数必须为对象")
    return arguments


def sanitize_history(history):
    """Remove unverified historical action-completion claims from context."""
    result = []
    for message in history:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "assistant" and looks_like_completion_claim(content):
            continue
        result.append({"role": role, "content": content})
    return result


def parse_explicit_reminder(user_text, now):
    """Resolve a high-confidence clock-first reminder without an LLM call."""
    match = _EXPLICIT_REMINDER_PATTERN.match(user_text or "")
    if not match:
        return None

    hour = int(match.group("hour"))
    minute_text = (
        match.group("minute")
        if match.group("minute") not in (None, "")
        else match.group("chinese_minute")
    )
    minute = int(minute_text or 0)
    title = (match.group("title") or "").strip()
    if not title or hour > 23 or minute > 59:
        return None

    day_text = match.group(1)
    day_offset = {"今天": 0, "明天": 1, "后天": 2}.get(day_text, 0)
    due = (now + timedelta(days=day_offset)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    return {
        "name": "add_todo",
        "arguments": {
            "title": title,
            "due_at": due.isoformat(timespec="seconds"),
        },
    }


def looks_like_completion_claim(text):
    value = text or ""
    return any(pattern.search(value) for pattern in _COMPLETION_PATTERNS)


def normalize_tool_run(name, result):
    value = result if isinstance(result, dict) else {
        "ok": False,
        "error": "invalid tool result",
    }
    object_id = value.get("todo_id") or value.get("candidate_id")
    try:
        object_id = int(object_id) if object_id is not None else None
    except (TypeError, ValueError):
        object_id = None
    return {
        "tool": name,
        "ok": bool(value.get("ok")),
        "object_id": object_id,
        "result": value,
    }


def _normalize_action(raw_action):
    if not isinstance(raw_action, dict):
        raise AgentProtocolError("动作计划不是对象")
    name = raw_action.get("name")
    arguments = raw_action.get("arguments")
    if name not in WRITE_TOOLS or not isinstance(arguments, dict):
        raise AgentProtocolError("动作计划包含未知工具或无效参数")

    if name == "add_todo":
        title = (
            arguments.get("title")
            or arguments.get("content")
            or arguments.get("description")
            or ""
        ).strip()
        due_at = str(arguments.get("due_at") or "").strip()
        if not title or not due_at:
            raise AgentProtocolError("add_todo 缺少标题或时间")
        normalized = {"title": title, "due_at": due_at}
        for key in ("notify_deskbot", "notify_feishu"):
            if isinstance(arguments.get(key), bool):
                normalized[key] = arguments[key]
        return {"name": name, "arguments": normalized}

    content = (arguments.get("content") or "").strip()
    if not content:
        raise AgentProtocolError("propose_memory 缺少内容")
    normalized = {"content": content}
    if arguments.get("category"):
        normalized["category"] = str(arguments["category"])
    return {"name": name, "arguments": normalized}


def plan_actions(client, model, user_text, now):
    """Ask the model for a structured write-action plan."""
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Echo-Mate 的写操作路由器。只调用 submit_action_plan，"
                "不要直接回答用户。\n"
                f"当前北京时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}。\n"
                "用户明确要求创建提醒/待办时使用 add_todo；"
                "明确要求长期记住个人偏好或资料时使用 propose_memory。\n"
                "查询、闲聊、天气、看列表、解释方法时 actions 必须为空。\n"
                "时间不完整、无法判断日期或时间已经过去时，actions 为空，"
                "并在 clarification 中提出一个简短问题。\n"
                "11.24、11:24、11点24分都表示当天的时和分，"
                "不要解释成年月日；日期只接受“11月24日”"
                "或 2026-11-24 这类明确写法。\n"
                "due_at 必须使用带时区的 ISO 8601，例如 "
                "2026-10-30T11:24:00+08:00。\n"
                "示例：用户说“11.24的时候提醒我起床”，当前仍是 11 点前，"
                "则调用 add_todo，标题为起床，due_at 使用今天 11:24。"
            ),
        },
        {"role": "user", "content": user_text},
    ]
    arguments = _forced_tool_arguments(
        client,
        model,
        messages,
        [ACTION_PLAN_TOOL],
        "submit_action_plan",
        350,
    )
    raw_actions = arguments.get("actions")
    if not isinstance(raw_actions, list):
        raise AgentProtocolError("动作计划缺少 actions 数组")
    actions = []
    seen = set()
    for item in raw_actions:
        action = _normalize_action(item)
        key = (
            action["name"],
            json.dumps(
                action["arguments"],
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        if key not in seen:
            seen.add(key)
            actions.append(action)
    clarification = str(arguments.get("clarification") or "").strip()
    if len(actions) > 1:
        return {
            "actions": [],
            "clarification": (
                "我识别到不止一个写操作。请先告诉我最重要的一项，"
                "我一次只处理一个写操作。"
            ),
        }
    return {
        "actions": actions,
        "clarification": clarification if not actions else "",
    }


def _normalize_claim(raw_claim):
    if not isinstance(raw_claim, dict):
        raise AgentProtocolError("claims 中包含非对象")
    tool = raw_claim.get("tool")
    if tool not in WRITE_TOOLS or not isinstance(raw_claim.get("ok"), bool):
        raise AgentProtocolError("claim 工具或状态无效")
    object_id = raw_claim.get("object_id")
    try:
        object_id = int(object_id) if object_id is not None else None
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError("claim object_id 无效") from exc
    return tool, raw_claim["ok"], object_id


def _extract_clock_tokens(text):
    tokens = set()
    for hour, minute in _CLOCK_PATTERN.findall(text or ""):
        tokens.add((int(hour), int(minute)))
    for hour, minute in _CHINESE_CLOCK_PATTERN.findall(text or ""):
        tokens.add((int(hour), int(minute or 0)))
    return tokens


def _allowed_clock_tokens(tool_runs):
    tokens = set()
    for run in tool_runs:
        result = run["result"]
        for key in ("due_text", "due_at", "due_speech"):
            value = result.get(key)
            if isinstance(value, str):
                tokens.update(_extract_clock_tokens(value))
    return tokens


def validate_reply(reply, claims, tool_runs):
    if not isinstance(reply, str) or not reply.strip():
        raise AgentProtocolError("用户回复为空")
    if not isinstance(claims, list):
        raise AgentProtocolError("claims 不是数组")

    expected = Counter(
        (run["tool"], run["ok"], run["object_id"]) for run in tool_runs
    )
    actual = Counter(_normalize_claim(item) for item in claims)
    if expected != actual:
        raise AgentProtocolError("claims 与工具执行结果不一致")

    successful = any(
        run["ok"] and run["tool"] in WRITE_TOOLS for run in tool_runs
    )
    if looks_like_completion_claim(reply) and not successful:
        raise AgentProtocolError("回复声称操作成功，但没有成功写工具")

    allowed_clocks = _allowed_clock_tokens(tool_runs)
    mentioned_clocks = _extract_clock_tokens(reply)
    if mentioned_clocks - allowed_clocks:
        raise AgentProtocolError("回复中的时间与工具结果不一致")

    allowed_ids = {
        run["object_id"] for run in tool_runs if run["object_id"] is not None
    }
    mentioned_ids = {
        int(value) for value in _OBJECT_ID_PATTERN.findall(reply or "")
    }
    if mentioned_ids - allowed_ids:
        raise AgentProtocolError("回复中的对象编号与工具结果不一致")
    return reply.strip()


def compose_grounded_reply(client, model, user_text, tool_runs, now):
    """Let the model phrase a reply, but validate all factual claims."""
    facts = []
    for run in tool_runs:
        result = run["result"]
        item = {
            "tool": run["tool"],
            "ok": run["ok"],
            "object_id": run["object_id"],
        }
        for key in (
            "title", "due_text", "due_at", "duplicate",
            "candidate_id", "result", "error",
        ):
            if key in result:
                item[key] = result[key]
        facts.append(item)

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Echo-Mate 的回复生成器，只调用 submit_grounded_reply。\n"
                "下面 facts 是本轮唯一的业务事实来源。可以自然、多样地表达，"
                "但不得新增或修改业务 ID、时间、成功状态和失败原因。\n"
                "没有执行成功时不得说已经设置或已经保存；"
                "创建提醒只能说已创建/已安排，不能声称已经提醒、已经播放或已读。\n"
                f"当前北京时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}。\n"
                "facts=" + json.dumps(facts, ensure_ascii=False)
            ),
        },
        {"role": "user", "content": user_text},
    ]
    arguments = _forced_tool_arguments(
        client,
        model,
        messages,
        [GROUNDED_REPLY_TOOL],
        "submit_grounded_reply",
        240,
    )
    reply = arguments.get("reply")
    claims = arguments.get("claims")
    return validate_reply(reply, claims, tool_runs)


def fallback_action_reply(tool_runs):
    """Last-resort response when structured composition fails."""
    parts = []
    seen_errors = set()
    for run in tool_runs:
        result = run["result"]
        name = run["tool"]
        if name == "add_todo" and run["ok"]:
            due_text = result.get("due_text") or result.get("due_at") or ""
            title = result.get("title") or "事项"
            if result.get("duplicate"):
                parts.append(
                    f"这条提醒已经存在 #{run['object_id']}：{due_text} {title}。"
                )
            else:
                parts.append(
                    f"已创建提醒 #{run['object_id']}：{due_text} {title}。"
                )
        elif name == "propose_memory" and run["ok"]:
            parts.append(
                f"已生成记忆候选 #{run['object_id']}，确认后才会写入正式记忆。"
            )
        else:
            error = str(result.get("error") or "")
            due_text = str(result.get("due_text") or "")
            current_time = str(result.get("current_time") or "")
            key = (error, due_text, current_time)
            if key in seen_errors:
                continue
            seen_errors.add(key)
            if error == "past_due":
                if due_text and current_time:
                    parts.append(
                        f"这个提醒没有创建：{due_text} 已经早于"
                        f"当前北京时间 {current_time}。"
                        "请告诉我一个更晚的时间。"
                    )
                else:
                    parts.append(
                        "这个提醒没有创建：目标时间已经过去，"
                        "请告诉我一个更晚的时间。"
                    )
            elif error == "bad_due":
                parts.append(
                    "这个提醒没有创建：我没能准确识别时间。"
                    "可以试试“明天11:58提醒我吃饭”。"
                )
            elif error == "empty":
                parts.append("这个提醒没有创建：请补充具体要提醒的事项。")
            else:
                parts.append(
                    "这次没有实际执行写操作，我不会把它说成成功。"
                    "请重新发送一次。"
                )
    return "\n".join(parts) or "这次操作没有成功，请重试。"


def false_action_reply():
    return "这次没有实际完成创建或修改，我不会把它说成成功。请重新发送一次。"
