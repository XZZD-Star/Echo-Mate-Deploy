#!/usr/bin/env python3
"""第 3 批：待办、记忆、待办到期提醒、AI 错误。"""

import sys

sys.path.insert(0, "/root/echomate-deploy/ux")
from lib_text import BOT, patch

OLD_TODO_HINT = '''    if text in {"添加待办", "新建待办"}:
        return (
            "请按以下格式发送：\\n"
            "添加待办 YYYY-MM-DD HH:MM 事项内容\\n"
            "例如：添加待办 2026-10-20 15:00 检查项目"
        )
'''

NEW_TODO_HINT = '''    if text in {"添加待办", "新建待办"}:
        return (
            "新待办这样发\\n\\n"
            "添加待办 2026-10-25 15:00 检查 VPS"
        )
'''

OLD_TODO_EMPTY = '''        if not title:
            return "待办标题不能为空。"

        if len(title) > 200:
            return "待办标题最多 200 个字符。"
'''

NEW_TODO_EMPTY = '''        if not title:
            return "😅 待办得写点内容"

        if len(title) > 200:
            return "😅 标题太长，200 字以内"
'''

OLD_TODO_DATE_ERR = '''        except ValueError:
            return (
                "日期或时间格式不正确。\\n"
                "正确示例：添加待办 2026-10-18 15:00 检查 VPS"
            )

        if due_local <= datetime.now(LOCAL_TZ):
            return "截止时间必须晚于当前时间，请重新设置。"
'''

NEW_TODO_DATE_ERR = '''        except ValueError:
            return "😅 日期写成 2026-10-25 15:00 这样"

        if due_local <= datetime.now(LOCAL_TZ):
            return "😅 这个时间已经过去了"
'''

OLD_TODO_OK = '''        return (
            f"✅ 待办创建成功，编号 #{cursor.lastrowid}\\n"
            f"事项：{title}\\n"
            f"时间：{due_local.strftime('%Y-%m-%d %H:%M')}\\n"
            "到期后将通过飞书提醒你。"
        )
'''

NEW_TODO_OK = '''        return (
            f"✅ 记下了 #{cursor.lastrowid}\\n\\n"
            f"{title} · {format_due_friendly(due_utc)}\\n"
            "到点提醒你"
        )
'''

OLD_TODO_NONE = '''        if not rows:
            return "目前没有未完成的待办。"

        lines = ["📋 未完成的待办（最多显示 20 条）："]

        for todo_id, title, due_utc in rows:
            lines.append(
                f"\\n#{todo_id}｜{format_due(due_utc)}｜{title}"
            )

        return "\\n".join(lines)
'''

NEW_TODO_NONE = '''        if not rows:
            return "📋 空空的，没有待办"

        lines = [f"📋 {len(rows)} 件事"]

        for todo_id, title, due_utc in rows:
            lines.append(
                f"\\n#{todo_id} · {format_due_friendly(due_utc)}"
                f" · {title}"
            )

        return "\\n".join(lines)
'''

OLD_TODO_DONE = '''        if cursor.rowcount:
            if action == "完成待办":
                return f"✅ 待办 #{todo_id} 已标记为完成。"
            return f"✅ 待办 #{todo_id} 已取消。"

        return (
            f"没有找到编号为 #{todo_id} 的未完成待办。"
            "它可能不存在，或已经完成、取消。"
        )
'''

NEW_TODO_DONE = '''        if cursor.rowcount:
            if action == "完成待办":
                return f"✅ #{todo_id} 完成"
            return f"✅ #{todo_id} 取消"

        return f"😅 没有 #{todo_id} 这个待办"
'''

OLD_MEM_EMPTY = '''        if not content:
            return "记忆内容不能为空。"

        if len(content) > 2000:
            return "单条记忆最多 2000 个字符，请缩短后重试。"
'''

NEW_MEM_EMPTY = '''        if not content:
            return "😅 要记的内容是空的"

        if len(content) > 2000:
            return "😅 太长了，2000 字以内"
'''

OLD_MEM_OK = '''        return (
            f"✅ 记忆已保存，编号 #{cursor.lastrowid}。\\n"
            f"内容：{content[:150]}"
        )
'''

NEW_MEM_OK = '''        return (
            f"✅ 记下了 #{cursor.lastrowid}\\n\\n"
            f"{content[:150]}"
        )
'''

OLD_MEM_SEARCH_NONE = '''        if not rows:
            return f"没有找到包含“{keyword}”的记忆。"

        lines = [f"🔎 找到 {len(rows)} 条相关记忆："]
        lines.extend(
            f"\\n#{row[0]}：{row[1][:180]}"
            for row in rows
        )
        return "\\n".join(lines)
'''

NEW_MEM_SEARCH_NONE = '''        if not rows:
            return f"😅 没有找到“{keyword}”"

        lines = [f"🔎 {len(rows)} 条"]
        lines.extend(
            f"\\n#{row[0]} · {row[1][:180]}"
            for row in rows
        )
        return "\\n".join(lines)
'''

OLD_MEM_LIST_NONE = '''        if not rows:
            return "目前还没有保存任何记忆。"

        lines = ["📚 最近的记忆（最多 10 条）："]
        lines.extend(
            f"\\n#{row[0]}：{row[1][:180]}"
            for row in rows
        )
        return "\\n".join(lines)
'''

NEW_MEM_LIST_NONE = '''        if not rows:
            return "📚 还没有记忆"

        lines = [f"📚 最近 {len(rows)} 条"]
        lines.extend(
            f"\\n#{row[0]} · {row[1][:180]}"
            for row in rows
        )
        return "\\n".join(lines)
'''

OLD_MEM_DEL = '''        if cursor.rowcount:
            return f"✅ 记忆 #{memory_id} 已删除。"

        return f"没有找到编号为 #{memory_id} 的记忆。"
'''

NEW_MEM_DEL = '''        if cursor.rowcount:
            return f"✅ #{memory_id} 删掉了"

        return f"😅 没有 #{memory_id}"
'''

OLD_REMIND = '''                due_display = format_due(due_utc)
                content = (
                    f"⏰ 待办到期提醒\\n"
                    f"编号：#{todo_id}\\n"
                    f"事项：{title}\\n"
                    f"计划时间：{due_display}\\n\\n"
                    f"完成后发送：完成待办 {todo_id}\\n"
                    f"取消任务发送：取消待办 {todo_id}"
                )
'''

NEW_REMIND = '''                due_display = format_due_friendly(due_utc)
                content = (
                    f"⏰ {title}\\n\\n"
                    f"{due_display} · #{todo_id}\\n"
                    f"完成 → 完成待办 {todo_id}"
                )
'''

OLD_AI = '''        return "AI 服务尚未配置，请检查服务端的百炼 API 配置。"
'''

NEW_AI = '''        return "😅 AI 还没配好"
'''

OLD_AI_LONG = '''        return "这条消息有些长，请控制在 4000 个字符以内。"
'''

NEW_AI_LONG = '''        return "😅 消息太长了，4000 字以内"
'''

OLD_AI_EMPTY = '''            return "AI 暂时没有生成有效回复，请换个方式提问。"
'''

NEW_AI_EMPTY = '''            return "😅 这次没想出来，换个说法试试"
'''

OLD_AI_FAIL = '''        return "抱歉，AI 服务暂时没有成功响应，请稍后再试。"
'''

NEW_AI_FAIL = '''        return "😅 AI 卡了一下，稍后再试"
'''

print("[1/4] 待办")
patch(
    BOT,
    [
        (OLD_TODO_HINT, NEW_TODO_HINT),
        (OLD_TODO_EMPTY, NEW_TODO_EMPTY),
        (OLD_TODO_DATE_ERR, NEW_TODO_DATE_ERR),
        (OLD_TODO_OK, NEW_TODO_OK),
        (OLD_TODO_NONE, NEW_TODO_NONE),
        (OLD_TODO_DONE, NEW_TODO_DONE),
    ],
)

print("[2/4] 记忆")
patch(
    BOT,
    [
        (OLD_MEM_EMPTY, NEW_MEM_EMPTY),
        (OLD_MEM_OK, NEW_MEM_OK),
        (OLD_MEM_SEARCH_NONE, NEW_MEM_SEARCH_NONE),
        (OLD_MEM_LIST_NONE, NEW_MEM_LIST_NONE),
        (OLD_MEM_DEL, NEW_MEM_DEL),
    ],
)

print("[3/4] 待办到期")
patch(BOT, [(OLD_REMIND, NEW_REMIND)])

print("[4/4] AI 错误")
patch(
    BOT,
    [
        (OLD_AI, NEW_AI),
        (OLD_AI_LONG, NEW_AI_LONG),
        (OLD_AI_EMPTY, NEW_AI_EMPTY),
        (OLD_AI_FAIL, NEW_AI_FAIL),
    ],
)

print("p3 done")
