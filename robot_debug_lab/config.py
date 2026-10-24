import os
from pathlib import Path
from urllib.parse import urlsplit
import ipaddress

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
RUNTIME.mkdir(exist_ok=True)
for line in (
    (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []
):
    if line.strip() and not line.lstrip().startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def validate_model_url(value):
    """Do not send credentials over plaintext remote connections."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port  # Validate malformed ports before accepting the URL.
        loopback = host == "localhost"
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (
            not host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
    except ValueError:
        raise ValueError(
            "Model API URL must use HTTPS, or HTTP on loopback, without embedded credentials, query or fragment"
        ) from None
    return value.rstrip("/")


BASE_URL = validate_model_url(
    os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
)
MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
API_KEY = os.environ.get("LLM_API_KEY", "")
ROBOTS = ("robot_1", "robot_2")


def robot_id(value):
    if value not in ROBOTS:
        raise ValueError("Unknown robot")
    return value
