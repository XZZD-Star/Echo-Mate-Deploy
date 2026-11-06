import json
import re
from datetime import datetime, timezone

from echomate_core import actions as assistant_actions
from echomate_core.db import open_connection
from feishu_bot import agent_runtime

from .ai_constants import (
    MAX_TOOL_ROUNDS,
    MEMORY_CONTEXT_LIMIT,
    READ_ONLY_TOOLS,
)
from .features.tools import _run_tool
from .runtime import (
    DB_PATH,
    LOCAL_TZ,
    OWNER_OPEN_ID,
    QWEN_MODEL,
    memory_observer,
    qwen_client,
)
from .time_utils import now_utc

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
