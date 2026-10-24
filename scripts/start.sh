#!/bin/bash
set -eo pipefail
source "$(dirname "$0")/env.bash"
cd "$RDL_ROOT"
mkdir -p .runtime
exec 9>.runtime/server.lock
flock -n 9 || { echo 'Robot Debug Lab is already running'; exit 1; }
exec .venv/bin/python -m uvicorn robot_debug_lab.app:app --timeout-graceful-shutdown 3 --host 127.0.0.1 --port "${RDL_PORT:-8765}"
