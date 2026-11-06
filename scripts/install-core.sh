#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE_PATH="${ECHOMATE_CORE_PATH:-$ROOT_DIR/app/Demo/echomate_core}"
FEISHU_VENV="${ECHOMATE_FEISHU_VENV:-$ROOT_DIR/feishu-venv}"
SERVER_VENV="${ECHOMATE_SERVER_VENV:-$ROOT_DIR/venv}"

if [[ ! -f "$CORE_PATH/pyproject.toml" ]]; then
    echo "echomate_core package not found: $CORE_PATH" >&2
    exit 1
fi

for venv in "$FEISHU_VENV" "$SERVER_VENV"; do
    if [[ ! -x "$venv/bin/python" ]]; then
        echo "skip missing virtualenv: $venv"
        continue
    fi
    "$venv/bin/python" -m pip install --disable-pip-version-check -e "$CORE_PATH"
done
