"""Runtime configuration, page constants and time helpers."""

from datetime import datetime, timedelta, timezone


import os
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import lark_oapi as lark
from dotenv import dotenv_values, load_dotenv
from openai import OpenAI

from echomate_core.credentials import credential
from echomate_core.observer import MemoryObserver


DEPLOY_ROOT = Path(__file__).resolve().parents[2]
FEISHU_DIR = DEPLOY_ROOT / "feishu_bot"
load_dotenv(FEISHU_DIR / ".env", override=False)

APP_ID = credential("lark_app_id")
APP_SECRET = credential("lark_app_secret")
OWNER_OPEN_ID = credential("lark_owner_open_id")

DB_PATH = Path(
    os.environ.get("ECHOMATE_DB_PATH", "/var/lib/echomate/assistant.db")
)
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
PC_DEVICE_ID = os.environ.get("ECHOMATE_PC_DEVICE_ID", "windows-main")
PC_PROFILE_ID = os.environ.get("ECHOMATE_PC_PROFILE_ID", "default")
CURRENT_CHAT_ID = ContextVar("CURRENT_CHAT_ID", default="")

if not APP_ID or not APP_SECRET:
    raise SystemExit("错误：App ID 或 App Secret 未配置")
if not OWNER_OPEN_ID.startswith("ou_"):
    raise SystemExit("错误：LARK_OWNER_OPEN_ID 未正确配置")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
os.chmod(DB_PATH.parent, 0o700)

api_client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .build()
)

SERVER_ENV_PATH = Path(
    os.environ.get(
        "ECHOMATE_SERVER_ENV",
        str(
            DEPLOY_ROOT
            / "app"
            / "Demo"
            / "AIChat_demo"
            / "Server"
            / ".env"
        ),
    )
)
server_cfg = dotenv_values(SERVER_ENV_PATH)

QWEN_API_KEY = credential("dashscope_api_key")
QWEN_MODEL = (server_cfg.get("CHAT_MODEL") or "qwen-max").strip()
MEMORY_MODEL = (server_cfg.get("MEMORY_MODEL") or "qwen-plus").strip()
TAVILY_API_KEY = credential("tavily_api_key")
TAVILY_URL = "https://api.tavily.com/search"
SEARCH_CACHE_TTL = 600

ws_endpoint = (
    server_cfg.get("DASHSCOPE_BASE_WEBSOCKET_API_URL") or ""
).strip()
ws_host = urlparse(ws_endpoint).hostname
if ws_host and ws_host.endswith(".maas.aliyuncs.com"):
    QWEN_BASE_URL = f"https://{ws_host}/compatible-mode/v1"
else:
    QWEN_BASE_URL = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

qwen_client = None
if QWEN_API_KEY:
    qwen_client = OpenAI(
        api_key=QWEN_API_KEY,
        base_url=QWEN_BASE_URL,
        timeout=35.0,
        max_retries=0,
    )

memory_observer = (
    MemoryObserver(qwen_client, MEMORY_MODEL)
    if qwen_client is not None
    else None
)




PAGES = {
    "menu", "status", "status_detail", "todo", "todo_new",
    "todo_history",
    "memory_hub", "memory", "chat_memory", "help", "notify", "quiet",
    "records",
    "memory_clear_confirm",
    "pc", "pc_single", "pc_fixed", "pc_fixed_new",
    "pc_history", "pc_history_detail",
}

PAGE_PARENT = {
    "status_detail": "status",
    "todo_new": "todo",
    "todo_history": "todo",
    "quiet": "notify",
    "records": "notify",
    "memory": "memory_hub",
    "chat_memory": "memory_hub",
    "memory_clear_confirm": "memory",
    "pc_single": "pc",
    "pc_fixed": "pc",
    "pc_fixed_new": "pc_fixed",
    "pc_history": "pc",
    "pc_history_detail": "pc_history",
}

PAGE_UNKNOWN_HINT = {
    "status": "这里没有这个编号\n1 刷新 · 2 详情 · 0 返回",
    "status_detail": "这里没有这个编号\n1 刷新 · 0 返回",
    "todo": (
        "这里没有这个数字操作\n"
        "完成：完成 今天待办1\n"
        "取消：取消 明天待办1\n"
        "8 上一页 · 9 下一页 · 0 返回"
    ),
    "todo_new": "这里需要直接填写待办内容\n0 返回",
    "todo_history": "8 上一页 · 9 下一页 · 0 返回",
    "memory": "8 上一页 · 9 下一页 · 0 返回",
    "chat_memory": "8 上一页 · 9 下一页 · 0 返回",
    "memory_hub": "请选择 1 或 2，或回复 0 返回",
    "help": "帮助页可直接回复 0 返回",
    "notify": "这里没有这个编号\n请选择 1 到 4，或回复 0 返回",
    "quiet": "静音页请直接发送设置命令\n0 返回",
    "records": "8 上一页 · 9 下一页 · 0 返回",
    "memory_clear_confirm": "请发送“确认清空记忆”或 0 返回",
    "pc": "这里没有这个编号\n请选择 1 到 3，或回复 0 返回",
    "pc_single": "请直接发送一条任务\n0 取消",
    "pc_fixed": "请直接发送任务，或选择页面上的编号\n0 返回",
    "pc_fixed_new": "请直接发送第一条任务\n0 取消",
    "pc_history": "1-6 查看详情 · 8 上一页 · 9 下一页 · 0 返回",
    "pc_history_detail": "任务详情页可直接回复 0 返回",
}

PAGE_CARD_CONFIG = {
    "menu": ("Echo-Mate", "blue"),
    "status": ("运行状态", "green"),
    "status_detail": ("系统详情", "turquoise"),
    "todo": ("待办清单", "orange"),
    "todo_new": ("新建待办", "orange"),
    "todo_history": ("历史待办", "grey"),
    "memory": ("长期偏好", "purple"),
    "chat_memory": ("聊天记忆", "turquoise"),
    "memory_hub": ("记忆", "purple"),
    "help": ("使用帮助", "blue"),
    "notify": ("语音播报", "turquoise"),
    "quiet": ("静音设置", "grey"),
    "records": ("播报记录", "turquoise"),
    "memory_clear_confirm": ("清空记忆", "red"),
    "pc": ("本机 Codex", "purple"),
    "pc_single": ("本机 Codex", "purple"),
    "pc_fixed": ("本机 Codex", "purple"),
    "pc_fixed_new": ("本机 Codex", "purple"),
    "pc_history": ("本机 Codex", "purple"),
    "pc_history_detail": ("本机 Codex", "purple"),
}

PAGE_CARD_DROP_FIRST = {
    "🐣 Echo-Mate",
    "🔍 状态详情",
    "🧰 Echo-Mate 系统详情",
    "➕ 新建待办",
    "📜 历史待办",
    "💡 帮助",
    "🔊 播报设置",
    "🌙 静音设置",
    "📢 播报记录",
    "🧠 记忆",
    "💬 聊天记忆",
    "⚠️ 清空全部记忆？",
}

SERVICE_UNITS = [
    ("echomate-server.service", "语音服务", "echomate-server"),
    ("echomate-feishu-bot.service", "飞书机器人", "echomate-feishu-bot"),
    ("echomate-monitor.service", "资源监控", "echomate-monitor"),
    (
        "echomate-ssh-session-watch.service",
        "SSH 通知",
        "echomate-ssh-session-watch",
    ),
    ("echomate-flowview.service", "Flow 查看", "echomate-flowview"),
]
BACKUP_UNIT = (
    "echomate-db-backup.timer",
    "数据库备份",
    "echomate-db-backup",
)

PAGE_TTL_SECONDS = 86400
PAGED_PAGE_SIZES = {
    "todo": 8,
    "todo_history": 8,
    "memory": 5,
    "chat_memory": 8,
    "records": 8,
    "pc_history": 6,
}


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
