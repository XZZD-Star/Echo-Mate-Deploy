#!/usr/bin/env python3
"""修天气三问题：时间副词误当地名、失败不回退记忆、去导航尾。"""

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


# ---------- 1. 地名停用词 ----------

OLD_EXTRACT = '''def _extract_place(text):
    """从问句里抽地名，抽不到返回 None。"""
    raw = (text or "").strip()

    # 去掉常见疑问词和天气词，剩下的当候选地名。
    cleaned = raw
    for word in (
        "今天", "明天", "后天", "现在", "当前", "这边", "那边",
        "天气", "气温", "温度", "怎么样", "如何", "多少", "几度",
        "会", "下", "有", "的", "吗", "呢", "啊", "？", "?", "，", ",",
    ):
        cleaned = cleaned.replace(word, " ")

    for token in re.split(r"\\s+", cleaned):
        token = token.strip()
        if len(token) >= 2:
            return token

    return None
'''

NEW_EXTRACT = '''PLACE_STOPWORDS = (
    "这段时间", "这几天", "最近", "现在", "当前", "今天", "明天",
    "后天", "未来", "之后", "这边", "那边", "那里", "这里",
    "怎么样", "如何", "多少", "几度", "天气", "气温", "温度",
)


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

    for token in re.split(r"\\s+", cleaned):
        token = token.strip()
        if len(token) < 2:
            continue

        # 剩余 token 仍是时间/程度词时不算地名。
        if any(stop in token for stop in PLACE_STOPWORDS):
            continue

        return token

    return None
'''


# ---------- 2. weather_reply 重写：回退记忆、去导航 ----------

OLD_WEATHER = '''    place = _extract_place(text)

    if not place:
        places = _memory_places()
        if places:
            place = places[0]

    if not place:
        return (
            "🌤 想知道哪里的天气？\\n\\n"
            "发「南昌天气」这样就行\\n"
            "或者先存一条：记住：我现在居住地是江西南昌\\n\\n"
            "0 返回"
        )

    place_info = _geocode(place)
    if not place_info:
        return (
            f"😅 没找到「{place}」这个地方\\n\\n"
            "换个写法试试，比如只发城市名\\n\\n"
            "0 返回"
        )

    data = _fetch_weather(place_info)
    if not data:
        return "😅 天气服务暂时连不上，稍后再试\\n\\n0 返回"

    current = data.get("current") or {}
    daily = data.get("daily") or {}

    temp = current.get("temperature_2m")
    humidity = current.get("relative_humidity_2m")
    code = current.get("weather_code")

    if temp is None:
        return "😅 天气数据不完整，稍后再试\\n\\n0 返回"

    condition = WMO_TEXT.get(code, "未知")

    label = place_info["name"]
    if place_info.get("admin") and place_info["admin"] != label:
        label = f"{place_info['admin']}{label}"

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

    lines.append("")
    lines.append("0 返回")

    return "\\n".join(lines)
'''

NEW_WEATHER = '''    asked_place = _extract_place(text)

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
                f"😅 没找到「{asked_place}」这个地方\\n\\n"
                "发「南昌天气」试试，或者发\\n"
                "记住：我现在居住地是城市名\\n"
            )

        return (
            "🌤 想知道哪里的天气？\\n\\n"
            "发「南昌天气」这样就行\\n"
            "也可以先存一条居住地：\\n"
            "记住：我现在居住地是江西南昌"
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
    if place_info.get("admin") and place_info["admin"] != label:
        label = f"{place_info['admin']}{label}"

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

        return "\\n".join(lines)

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

    return "\\n".join(lines)
'''


# ---------- 3. 提示语里已有的 0 返回也去掉 ----------

OLD_FETCH_FAIL = '''    if not data:
        return "😅 天气服务暂时连不上，稍后再试\\n\\n0 返回"
'''

# （新版 weather_reply 已不含该行，此对为空操作兜底）


# ---------- 应用 ----------

print("[1/3] 地名停用词")
patch(BOT, [(OLD_EXTRACT, NEW_EXTRACT)])

print("[2/3] weather_reply 重写")
patch(BOT, [(OLD_WEATHER, NEW_WEATHER)])

print("done")
