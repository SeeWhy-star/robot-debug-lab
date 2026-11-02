#!/bin/bash
set -eo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/stop_all.py "$@"
