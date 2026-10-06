import os
import subprocess
import time
from pathlib import Path

HOME = Path("/home/gitgitmiko")
HERMES = HOME / ".hermes"
ENV_PATH = HERMES / ".env"
CONFIG_PATH = HERMES / "config.yaml"
BIN = HOME / ".local" / "bin" / "hermes"
MODEL = "openrouter/free"


def sync(cfg):
    if not HERMES.is_dir() or not BIN.is_file():
        return ""
    token = str(cfg.get("hermes_telegram_token") or "").strip()
    key = str(cfg.get("hermes_api_key") or "").strip()
    chat = str(cfg.get("telegram_chat_id") or "").strip()
    current = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if token:
        current = _upsert(current, "TELEGRAM_BOT_TOKEN", token)
    if key:
        current = _upsert(current, "OPENROUTER_API_KEY", key)
    if chat:
        current = _upsert(current, "TELEGRAM_ALLOWED_USERS", chat)
    ENV_PATH.write_text(current, encoding="utf-8")
    ENV_PATH.chmod(0o600)
    _ensure_model()
    if token and key:
        _restart()
        return " Hermes di STB sudah dijalankan."
    return ""


def _upsert(text, key, value):
    lines = []
    found = False
    for line in text.splitlines():
        if line.startswith(key + "="):
            lines.append(key + "=" + value)
            found = True
        else:
            lines.append(line)
    if not found:
        lines.append(key + "=" + value)
    return "\n".join(lines).rstrip() + "\n"


def _ensure_model():
    if not CONFIG_PATH.exists():
        return
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("model:")), None)
    if start is None:
        return
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index] and not lines[index].startswith((" ", "#", "\t")):
            end = index
            break
    changed = False
    for index in range(start + 1, end):
        stripped = lines[index].lstrip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("default:"):
            replacement = '  default: "%s"' % MODEL
            if lines[index] != replacement:
                lines[index] = replacement
                changed = True
        elif stripped.startswith("provider:"):
            replacement = '  provider: "openrouter"'
            if lines[index] != replacement:
                lines[index] = replacement
                changed = True
    if not any(
        not line.lstrip().startswith("#") and line.strip() == "free_only: true"
        for line in lines
    ):
        lines.extend(["auxiliary:", "  free_only: true"])
        changed = True
    if changed:
        CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _restart():
    subprocess.run(
        ["pkill", "-f", r"sys\.exit\(main\(\)\).* gateway"],
        check=False,
    )
    time.sleep(1)
    script = HERMES / "start-gateway.sh"
    # `at` starts outside the dashboard service cgroup, so a later
    # harga-hbar restart does not send SIGTERM to the gateway.
    subprocess.run(["at", "now"], input=f"{script}\n".encode(), check=False)
