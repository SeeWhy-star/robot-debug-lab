"""Stop only Robot Debug Lab processes owned by the current user."""

import argparse
import os
from pathlib import Path
import signal
import time


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
PID_FILES = {
    "launcher": RUNTIME / "all.pid",
    "server": RUNTIME / "server.pid",
    "openfang": RUNTIME / "openfang.pid",
}


def _process(pid):
    proc = Path("/proc") / str(pid)
    try:
        if proc.stat().st_uid != os.getuid():
            return None
        command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            errors="replace"
        )
        cwd = (proc / "cwd").resolve()
        return command, cwd
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


def _matches(pid, kind):
    details = _process(pid)
    if not details:
        return False
    command, cwd = details
    if cwd != ROOT:
        return False
    if kind == "launcher":
        return "scripts/start_all.py" in command
    if kind == "server":
        return "uvicorn" in command and "robot_debug_lab.app:app" in command
    if kind == "openfang":
        config_path = str(RUNTIME / "openfang/config.toml")
        return config_path in command and "openfang" in command
    if kind == "simulation":
        return str(ROOT / "simulation/launch/rdl_multi_robot.launch.py") in command
    return False


def _read_pid(kind):
    try:
        pid = int(PID_FILES[kind].read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    return pid if _matches(pid, kind) else None


def _discover(kind):
    found = set()
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if entry.name.isdigit() and _matches(int(entry.name), kind):
            found.add(int(entry.name))
    recorded = _read_pid(kind) if kind in PID_FILES else None
    if recorded:
        found.add(recorded)
    return sorted(found)


def _wait_gone(pids, timeout):
    deadline = time.monotonic() + timeout
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if _process(pid)}
        if remaining:
            time.sleep(0.2)
    return remaining


def _signal(pids, sig, *, process_group=False, dry_run=False):
    for pid in pids:
        if dry_run:
            continue
        try:
            if process_group and os.getpgid(pid) == pid:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _stop(kind, initial_signal, timeout, *, process_group=False, dry_run=False):
    pids = _discover(kind)
    if not pids:
        return []
    print(f"{kind}: " + ", ".join(map(str, pids)), flush=True)
    _signal(pids, initial_signal, process_group=process_group, dry_run=dry_run)
    if dry_run:
        return pids
    remaining = _wait_gone(pids, timeout)
    if remaining:
        _signal(remaining, signal.SIGTERM, process_group=process_group)
        remaining = _wait_gone(remaining, 5)
    if remaining:
        _signal(remaining, signal.SIGKILL, process_group=process_group)
        _wait_gone(remaining, 2)
    return pids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="只显示将停止的项目进程"
    )
    args = parser.parse_args()
    stopped = []
    stopped += _stop("launcher", signal.SIGTERM, 25, dry_run=args.dry_run)
    # A live launcher normally owns and cleans these children. These passes
    # also recover services orphaned when its terminal or tool session died.
    stopped += _stop("server", signal.SIGINT, 20, dry_run=args.dry_run)
    stopped += _stop("openfang", signal.SIGINT, 10, dry_run=args.dry_run)
    stopped += _stop(
        "simulation", signal.SIGINT, 15,
        process_group=True, dry_run=args.dry_run,
    )
    if args.dry_run:
        print("仅检查，未发送停止信号。")
        return
    for path in PID_FILES.values():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    print("已停止 Robot Debug Lab。" if stopped else "Robot Debug Lab 当前未运行。")


if __name__ == "__main__":
    main()
