"""Pure formatting helpers used by Feishu pages."""


def page_count(total, page_size):
    return max(1, (max(0, total) + page_size - 1) // page_size)


def clamp_page(page_index, total, page_size):
    index = max(1, int(page_index or 1))
    return min(index, page_count(total, page_size))


def pager_lines(page_index, total, page_size, extra=None):
    pages = page_count(total, page_size)
    lines = []
    if extra:
        lines.append(extra)
    lines.extend([
        f"第 {page_index}/{pages} 页 · 共 {total} 条",
        "8 上一页 · 9 下一页 · 0 返回",
    ])
    return lines


def pager_state(page_index, total, page_size, item_ids):
    return {
        "page_index": page_index,
        "page_size": page_size,
        "total": total,
        "item_ids": item_ids,
    }


def unit_state_text(state):
    if state == "active":
        return "正常"
    if state in {"inactive", "failed", "unknown"}:
        return "异常"
    return state


def unit_dot(state):
    return "🟢" if state == "active" else "🔴"


def format_bytes(value):
    gib = value / 1024**3
    if gib >= 1:
        return f"{gib:.1f} GB".replace(".0 GB", " GB")
    return f"{value / 1024**2:.0f} MB"


def normalize_hhmm(value):
    """Normalize 9:05 to 09:05 and reject invalid clock values."""
    try:
        hour, minute = value.split(":", 1)
        hour = int(hour)
        minute = int(minute)
    except (TypeError, ValueError):
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    return f"{hour:02d}:{minute:02d}"


def kind_label(kind):
    return {
        "todo": "待办",
        "alert": "告警",
        "recovery": "恢复",
        "manual": "测试",
    }.get(kind, kind)
