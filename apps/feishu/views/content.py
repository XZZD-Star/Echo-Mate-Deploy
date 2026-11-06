"""Compatibility facade for Feishu page views."""

from .memory import *
from .memory import (
    _build_chat_memory_page,
    _build_memory_page,
    _memory_candidate_display_index,
    _memory_rows,
)
from .notify import *
from .notify import _build_records_page, _quiet_state, _record_total
from .page import build_page_card, render_page
from .status import *
from .status import _boot_text, _service_states, _unit_state, _uptime_text
from .todo import *
from .todo import (
    _build_todo_history_page,
    _build_todo_page,
    _todo_rows,
    _todo_window,
)
