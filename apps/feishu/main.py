"""Stable Feishu entry point while the legacy bot is split incrementally."""

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FEISHU_ROOT = REPO_ROOT / "feishu_bot"
for path in (FEISHU_ROOT, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from feishu_bot.bot import main


if __name__ == "__main__":
    main()
