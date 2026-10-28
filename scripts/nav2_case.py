#!/usr/bin/env python3
"""Run with .venv/bin/python scripts/nav2_case.py reproduce|investigate|all."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robot_debug_lab.source_case import RUNTIME, reproduce, investigate


def main():
    parser = argparse.ArgumentParser(description="Pinned Nav2 component diagnosis and repair experiment")
    parser.add_argument("command", choices=["reproduce", "investigate", "all"])
    parser.add_argument("--attempts", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    # Prevent concurrent builds/evidence replacement for this case.
    import fcntl
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command in {"reproduce", "all"}:
            reproduce()
        if args.command in {"investigate", "all"}:
            result = investigate(args.attempts)
            print("Final:", result["status"], "— .runtime/nav2-5710/investigation.json")
            return 0 if result["status"] == "component_verified" else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(1)
