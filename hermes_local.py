import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path("/home/gitgitmiko")
HERMES = HOME / ".hermes"
ENV_PATH = HERMES / ".env"
CONFIG_PATH = HERMES / "config.yaml"
BIN = HOME / ".local" / "bin" / "hermes"
MODEL = "openrouter/free"
SECRET_TEXT = re.compile(
    r"(crsr_|cursor_|gho_|github_pat_|sk-or-)[A-Za-z0-9_\-]{6,}|[0-9]{6,12}:[A-Za-z0-9_\-]{20,}"
)
try:
    WIB = ZoneInfo("Asia/Jakarta")
except Exception:
    WIB = timezone(timedelta(hours=7))


def _hermes_db():
    override = os.environ.get("GITGITMIKO_HERMES_DB", "").strip()
    if override:
        return Path(override)
    return HERMES / "state.db"


def _wib(stamp):
    try:
        return datetime.fromtimestamp(float(stamp), WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _clip(text):
    clean = " ".join(SECRET_TEXT.sub("[rahasia]", str(text or "")).split())
    if len(clean) > 96:
        return clean[:93].rstrip() + "…"
    return clean


def _hermes_status(ended_at, last_role):
    if ended_at:
        return "selesai"
    role = str(last_role or "")
    if role == "assistant":
        return "selesai"
    if role:
        return "berjalan"
    return "terbuka"


def recent_runs(limit=200):
    path = _hermes_db()
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    except (OSError, sqlite3.Error):
        return []
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT s.title, s.model, s.started_at, s.last_activity_at, s.ended_at,
                   COALESCE(s.input_tokens, 0) AS input_tokens,
                   COALESCE(s.output_tokens, 0) AS output_tokens,
                   COALESCE(s.reasoning_tokens, 0) AS reasoning_tokens,
                   (
                     SELECT m.role FROM messages m
                     WHERE m.session_id = s.id
                       AND m.role IN ('user', 'assistant', 'tool')
                     ORDER BY m.id DESC
                     LIMIT 1
                   ) AS last_role
            FROM sessions s
            WHERE COALESCE(s.hidden, 0) = 0
              AND COALESCE(s.message_count, 0) > 0
            ORDER BY COALESCE(s.last_activity_at, s.started_at) DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    runs = []
    for row in rows:
        stamp = row["last_activity_at"] or row["started_at"]
        tokens = int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0) + int(row["reasoning_tokens"] or 0)
        runs.append(
            {
                "time": _wib(stamp),
                "repo": _clip(row["title"]) or "Hermes",
                "source": "hermes",
                "mode": "hermes",
                "model": row["model"] or MODEL,
                "total_tokens": tokens,
                "charged_cents": None,
                "raw_cost_cents": None,
                "status": _hermes_status(row["ended_at"], row["last_role"]),
                "summary": "",
            }
        )
    return runs


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
