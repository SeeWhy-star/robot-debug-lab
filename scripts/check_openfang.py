"""Read-only daemon check; does not create agents or call a model."""
import sys
import httpx
from robot_debug_lab import config


def main():
    headers = {"Authorization": "Bearer " + config.OPENFANG_API_KEY} if config.OPENFANG_API_KEY else {}
    try:
        with httpx.Client(base_url=config.OPENFANG_URL, headers=headers, timeout=5) as client:
            client.get("/api/health").raise_for_status()
            response = client.get("/api/agents")
            response.raise_for_status()
            if not isinstance(response.json(), list):
                raise ValueError("Unexpected response")
    except (httpx.HTTPError, ValueError):
        print("OpenFang 连接检查失败：检查 daemon 地址、启动状态和 OPENFANG_API_KEY。")
        return 1
    print("OpenFang 健康与认证检查通过；尚未验证模型调用和 ROS 闭环。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
