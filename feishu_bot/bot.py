"""Stable compatibility entry point for the Feishu application.

The implementation lives under :mod:`apps.feishu`. This module keeps the
legacy import path and command name used by systemd and existing tests.
"""

import subprocess
import sys
from pathlib import Path


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
if str(DEPLOY_ROOT) not in sys.path:
    sys.path.insert(0, str(DEPLOY_ROOT))

import lark_oapi as lark

from echomate_core import actions as assistant_actions
from echomate_core import assistant as assistant_core
from echomate_core import memory as memory_store
from echomate_core import notify as notify_store
from echomate_core import store as echomate_store
from echomate_core import tools as assistant_tools
from echomate_core.db import connect as sqlite_connect
from echomate_core.db import open_connection
from echomate_core.observer import MemoryObserver
from feishu_bot import agent_runtime, pc_control_service, pc_control_store, pc_pages
from feishu_bot.pc_routing import PC_PAGES

from apps.feishu.ai import *
from apps.feishu.ai import (
    _chat_with_tools,
    _compose_or_fallback,
    _execute_planned_actions,
    _normalize_memory,
    _run_tool,
    _tool_add_todo,
    _tool_get_weather,
    _tool_list_todos,
    _tool_propose_memory,
    _tool_search_memories,
    _tool_web_search,
)
from apps.feishu.app import main
from apps.feishu.constants import *
from apps.feishu.features.pc import *
from apps.feishu.features.pc import _handle_pc_context, _pc_event_key, _pc_job_ack
from apps.feishu.features.todo import *
from apps.feishu.features.todo import (
    _add_todo,
    _handle_todo_action,
    _parse_natural_todo,
)
from apps.feishu.handlers import on_message
from apps.feishu.messaging import send_card, send_page_card, send_text
from apps.feishu.navigation import *
from apps.feishu.navigation import (
    _enter,
    _handle_pagination,
    _navigate_back,
    _open_page_index,
)
from apps.feishu.page_state import *
from apps.feishu.page_state import (
    _pc_control_enabled,
    _read_setting,
    _write_setting,
    get_page,
    get_page_state,
    set_page,
)
from apps.feishu.pages import _build_pc_page, _render_page_state
from apps.feishu.router import *
from apps.feishu.router import _handle_page_number, _toggle, handle_command
from apps.feishu.runtime import *
from apps.feishu.schema import init_chat_table, init_db
from apps.feishu.time_utils import *
from apps.feishu.views.content import *
from apps.feishu.views.content import (
    _build_chat_memory_page,
    _build_memory_page,
    _build_records_page,
    _build_todo_history_page,
    _build_todo_page,
    _memory_candidate_display_index,
    _record_total,
)
from apps.feishu.views.formatting import *
from apps.feishu.views.formatting import (
    _clamp_page,
    _format_bytes,
    _kind_label,
    _normalize_hhmm,
    _page_count,
    _pager_lines,
    _pager_state,
    _unit_dot,
    _unit_state_text,
)
from apps.feishu.worker import reminder_loop


if __name__ == "__main__":
    main()
