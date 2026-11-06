"""Compatibility import for the shared Echo-Mate assistant core."""

import sys

from echomate_core import assistant as _target

sys.modules[__name__] = _target
