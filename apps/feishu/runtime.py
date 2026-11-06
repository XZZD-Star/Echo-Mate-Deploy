"""Runtime configuration and shared clients for the Feishu entry point."""

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
