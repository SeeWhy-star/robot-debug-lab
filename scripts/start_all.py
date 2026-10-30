"""One foreground launcher, with ownership-aware cleanup of its child services."""
import os
import signal
import socket
import subprocess
import threading
import time

import httpx
from robot_debug_lab import config


def main():
    port = int(os.environ.get("RDL_PORT", "8765"))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            raise SystemExit(f"端口 {port} 已被使用。若调试台已启动，直接打开 http://127.0.0.1:{port}")
    except OSError:
        pass
    stopped = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stopped.set())
    children, logs = [], []

    def spawn(script, log_name):
        log = (config.RUNTIME / log_name).open("a")
        logs.append(log)
        child = subprocess.Popen(["bash", str(config.ROOT / "scripts" / script)],
                                 cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        children.append(child)
        return child

    def wait_ready(url, child=None, headers=None, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until and not stopped.is_set():
            if child is not None and child.poll() is not None:
                raise RuntimeError("服务启动失败，请查看 .runtime/ 下的启动日志")
            try:
                with httpx.Client(timeout=2) as client:
                    response = client.get(url, headers=headers)
                    if response.is_success:
                        return
            except httpx.HTTPError:
                pass
            stopped.wait(0.4)
        raise RuntimeError("服务未就绪或启动已取消")

    try:
        if config.AGENT_BACKEND == "openfang":
            headers = {"Authorization": "Bearer " + config.OPENFANG_API_KEY} if config.OPENFANG_API_KEY else {}
            try:
                httpx.get(config.OPENFANG_URL + "/api/agents", headers=headers, timeout=3).raise_for_status()
            except httpx.ConnectError:
                if config.OPENFANG_URL != "http://127.0.0.1:4200":
                    raise RuntimeError("请先启动自定义地址的 OpenFang 服务") from None
                child = spawn("start_openfang.sh", "openfang-service.log")
                wait_ready(config.OPENFANG_URL + "/api/agents", child, headers)
            except httpx.HTTPError:
                raise RuntimeError("OpenFang 不可用，请检查地址与认证配置") from None
        child = spawn("start.sh", "lab-service.log")
        startup_timeout = 120 if config.SIM_BACKEND == "nav2" else 30
        wait_ready(f"http://127.0.0.1:{port}/api/state", child, timeout=startup_timeout)
        print(f"已启动：http://127.0.0.1:{port}  （Ctrl+C 停止）", flush=True)
        print("日志：.runtime/lab-service.log、.runtime/openfang-service.log", flush=True)
        while not stopped.wait(0.5):
            if any(child.poll() is not None for child in children):
                raise RuntimeError("子服务意外退出，请查看启动日志")
    finally:
        # Only stop children created here; a reused OpenFang belongs to its owner.
        for child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGINT)
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    child.wait(timeout=10)
        for log in logs:
            log.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
