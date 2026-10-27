"""Start the project-local OpenFang daemon using existing model credentials."""
import json
import os
import shutil
from pathlib import Path
from robot_debug_lab import config


def main():
    binary = config.RUNTIME / "openfang-bin/openfang"
    if not binary.is_file():
        found = shutil.which("openfang")
        if not found:
            raise SystemExit("Install OpenFang first; see docs/openfang.md")
        binary = Path(found)
    if not config.API_KEY:
        raise SystemExit("Configure LLM_API_KEY in the local .env first")
    provider = os.environ.get("RDL_OPENFANG_PROVIDER", "deepseek")
    home = config.RUNTIME / "openfang"
    home.mkdir(mode=0o700, exist_ok=True)
    path = home / "config.toml"
    # Managed project-local config; never modifies ~/.openfang or stores the key.
    quote = json.dumps
    content = (
        'api_listen = "127.0.0.1:4200"\nnetwork_enabled = false\nusage_footer = "off"\n'
        '[default_model]\n'
        f'provider = {quote(provider)}\nmodel = {quote(config.MODEL)}\n'
        'api_key_env = "RDL_OPENFANG_MODEL_KEY"\n'
        f'base_url = {quote(config.BASE_URL)}\n'
    )
    path.write_text(content)
    path.chmod(0o600)
    env = os.environ.copy()
    env["OPENFANG_HOME"] = str(home)
    env["RDL_OPENFANG_MODEL_KEY"] = config.API_KEY
    os.execve(str(binary), [str(binary), "--config", str(path), "start"], env)


if __name__ == "__main__":
    main()
