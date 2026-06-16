#!/bin/zsh
set -euo pipefail

export PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/pycache"
export PYTHONDONTWRITEBYTECODE=1

cd "$(dirname "$0")"
exec /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -B main.py
