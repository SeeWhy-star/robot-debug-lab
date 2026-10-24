"""Supervise a child without depending on the spawning worker thread lifetime."""

import os, signal, subprocess, sys, time

parent = int(os.environ["RDL_PARENT_PID"])
child = subprocess.Popen(sys.argv[1:])


def stop(signum, frame):
    if child.poll() is None:
        child.send_signal(signum)
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
    raise SystemExit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
while child.poll() is None:
    if os.getppid() != parent:
        stop(signal.SIGTERM, None)
    time.sleep(0.2)
sys.exit(child.returncode)
