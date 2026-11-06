from datetime import datetime, timezone

from .runtime import LOCAL_TZ

def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_due(utc_text):
    dt = datetime.fromisoformat(utc_text).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def format_due_friendly(utc_text):
    """今天/明天/后天 + HH:MM，更远的用 MM-DD HH:MM。"""
    dt = datetime.fromisoformat(utc_text).astimezone(LOCAL_TZ)
    delta = (dt.date() - datetime.now(LOCAL_TZ).date()).days

    if delta == 0:
        day = "今天"
    elif delta == 1:
        day = "明天"
    elif delta == 2:
        day = "后天"
    else:
        return dt.strftime("%m-%d %H:%M")

    return f"{day} {dt:%H:%M}"
