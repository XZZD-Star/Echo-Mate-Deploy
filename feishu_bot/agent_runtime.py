"""Compatibility import for the shared Echo-Mate assistant core."""

import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[1] / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from assistant_core import *  # noqa: F401,F403
