#!/usr/bin/env python3
"""修地名提取与清空记忆二次确认。"""

from pathlib import Path

BOT = Path("/root/echomate-deploy/feishu_bot/bot.py")


def patch(path, pairs):
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        if new in text:
            print("  skip")
            continue
        if text.count(old) != 1:
            raise SystemExit(
                f"匹配失败：命中 {text.count(old)} 次\n---\n{old[:300]}\n---"
            )
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"  patched: {path.name}")


# ---------- 1. 地名归一化 ----------

OLD_GEOCODE = '''def _geocode(place):
    """把地名换成经纬度，逐级去掉行政后缀重试。"""
    attempts = [place]

    trimmed = place
    for suffix in WEATHER_SUFFIXES:
        if trimmed.endswith(suffix) and len(trimmed) > len(suffix):
            trimmed = trimmed[: -len(suffix)]
            attempts.append(trimmed)
            break

    # 「江西南昌」这类：取最后 2 字再试。
    if len(place) > 2 and place not in attempts:
        attempts.append(place[-2:])

    for candidate in attempts:
'''

NEW_GEOCODE = '''PROVINCES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林",
    "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西",
    "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏", "新疆",
    "香港", "澳门",
)


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
'''

OLD_GEO_LOOP = '''        results = data.get("results") or []
        if results:
            top = results[0]
            return {
                "name": top.get("name", candidate),
                "admin": top.get("admin1", ""),
                "lat": top.get("latitude"),
                "lon": top.get("longitude"),
            }

    return None
'''

NEW_GEO_LOOP = '''        results = data.get("results") or []
        if results:
            top = results[0]
            return {
                "name": top.get("name", candidate),
                "admin": top.get("admin1", ""),
                "lat": top.get("latitude"),
                "lon": top.get("longitude"),
            }

    return None
'''


# ---------- 2. 清空记忆二次确认 ----------

OLD_CLEAR = '''    # 删除全部，需要带确认词避免误触。
    if text in {"清空记忆", "删除全部记忆"}:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not total:
            return "📚 本来就没有记忆\\n\\n0 返回"

        conn.execute(
            "DELETE FROM memories WHERE owner_id = ?", (OWNER_OPEN_ID,)
        )
        return (
            f"✅ 清空了 {total} 条记忆\\n\\n"
            "以后想存新的，发：记住：内容\\n\\n"
            "0 返回"
        )
'''

NEW_CLEAR = '''    # 清空全部记忆需要两步确认，避免误触。
    if text in {"清空记忆", "删除全部记忆"}:
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not total:
            return "📚 本来就没有记忆\\n\\n0 返回"

        set_page(chat_id, "memory_clear_confirm", conn=conn)
        return (
            f"⚠️ 要清空全部 {total} 条记忆？\\n\\n"
            "删除后无法恢复。\\n"
            "确认发：确认清空记忆\\n"
            "取消发：0\\n\\n"
            "0 取消"
        )

    if text == "确认清空记忆":
        total = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE owner_id = ?",
            (OWNER_OPEN_ID,),
        ).fetchone()[0]

        if not total:
            set_page(chat_id, "memory", conn=conn)
            return "📚 本来就没有记忆\\n\\n0 返回"

        conn.execute(
            "DELETE FROM memories WHERE owner_id = ?", (OWNER_OPEN_ID,)
        )
        set_page(chat_id, "memory", conn=conn)
        return (
            f"✅ 清空了 {total} 条记忆\\n\\n"
            "以后想存新的，发：记住：内容\\n\\n"
            "0 返回"
        )
'''


# ---------- 3. 新增确认页 ----------

OLD_PAGES = '''PAGES = {
    "menu", "status", "status_detail", "todo", "todo_new",
    "memory", "help", "notify", "quiet", "records",
}
'''

NEW_PAGES = '''PAGES = {
    "menu", "status", "status_detail", "todo", "todo_new",
    "memory", "help", "notify", "quiet", "records",
    "memory_clear_confirm",
}
'''

OLD_PARENT = '''PAGE_PARENT = {
    "status_detail": "status",
    "quiet": "notify",
    "records": "notify",
}
'''

NEW_PARENT = '''PAGE_PARENT = {
    "status_detail": "status",
    "quiet": "notify",
    "records": "notify",
    "memory_clear_confirm": "memory",
}
'''

OLD_RENDER = '''    if page == "records":
        return render_records()
    return menu_text()
'''

NEW_RENDER = '''    if page == "records":
        return render_records()
    if page == "memory_clear_confirm":
        return (
            "⚠️ 清空全部记忆？\\n\\n"
            "删除后无法恢复。\\n"
            "确认发：确认清空记忆\\n"
            "取消发：0"
        )
    return menu_text()
'''


print("[1/4] 地名候选")
patch(BOT, [(OLD_GEOCODE, NEW_GEOCODE)])

print("[2/4] 清空二次确认")
patch(BOT, [(OLD_CLEAR, NEW_CLEAR)])

print("[3/4] 页面注册")
patch(BOT, [(OLD_PAGES, NEW_PAGES), (OLD_PARENT, NEW_PARENT)])

print("[4/4] 确认页渲染")
patch(BOT, [(OLD_RENDER, NEW_RENDER)])

print("done")
