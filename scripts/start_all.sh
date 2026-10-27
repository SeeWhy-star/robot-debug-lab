#!/bin/bash
set -eo pipefail
source "$(dirname "$0")/env.bash"
cd "$RDL_ROOT"
mkdir -p .runtime
exec 9>.runtime/all.lock
flock -n 9 || { echo 'Robot Debug Lab launcher is already running'; exit 1; }
exec .venv/bin/python scripts/start_all.py
