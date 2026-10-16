#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
if [[ ! -x .venv/bin/python ]]; then
  echo 'FAIL: Python environment missing; run uv sync --all-packages --all-groups.' >&2
  exit 1
fi
exec .venv/bin/python scripts/verify_phase0.py "$@"
