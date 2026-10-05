import json
import os
import re
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
ALLOW_PATH = ROOT / "restart.allow"
DENIED_SERVICES = {
    "ssh",
    "sshd",
    "mysql",
    "mariadb",
    "postgresql",
    "apache2",
    "httpd",
    "cloudflared",
    "cron",
}
lock = threading.Lock()
GITHUB_REPO = re.compile(
    r"^(?:https://github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def default_repos():
    return [
        {
            "id": "dashboard_agent_cursor",
            "name": "dashboard_agent_cursor",
            "full_name": "gitgitmiko/dashboard_agent_cursor",
            "url": "https://github.com/gitgitmiko/dashboard_agent_cursor",
            "branch": "main",
            "local_path": "/home/gitgitmiko/harga-hbar",
            "service": "harga-hbar",
        }
    ]


def default_config():
    return {
        "cursor_api_key": "",
        "telegram_token": "",
        "telegram_chat_id": "",
        "github_token": "",
        "model_mode": "auto",
        "custom_model": "composer-2.5",
    }


def default_state():
    return {
        "runs": [],
        "selected_repo_id": "",
        "telegram_offset": 0,
        "job": None,
    }


def load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback()


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    path.chmod(0o600)


def ensure_secrets():
    import secrets as _secrets

    with lock:
        saved = load_json(CONFIG_PATH, default_config)
        if isinstance(saved, dict) and saved.get("session_secret"):
            return saved.get("session_secret")
        current = default_config()
        if isinstance(saved, dict):
            current.update(saved)
        secret = _secrets.token_urlsafe(32)
        current["session_secret"] = secret
        if not isinstance(saved, dict) or "repos" not in saved:
            current.pop("repos", None)
        save_json(CONFIG_PATH, current)
        return secret


def get_config():
    with lock:
        saved = load_json(CONFIG_PATH, default_config)
        cfg = default_config()
        has_repos = isinstance(saved, dict) and "repos" in saved
        if isinstance(saved, dict):
            cfg.update(saved)
        if not has_repos:
            cfg["repos"] = default_repos()
        if cfg.get("model_mode") not in ("auto", "custom"):
            cfg["model_mode"] = "auto"
        cfg["repos"] = [item for item in (cfg.get("repos") or []) if isinstance(item, dict)]
        return cfg


def has_state_key(key):
    try:
        saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return isinstance(saved, dict) and key in saved


def get_state():
    with lock:
        state = default_state()
        saved = load_json(STATE_PATH, default_state)
        if isinstance(saved, dict):
            state.update(saved)
        if not isinstance(state.get("runs"), list):
            state["runs"] = []
        return state


def update_state(mutator):
    with lock:
        state = default_state()
        saved = load_json(STATE_PATH, default_state)
        if isinstance(saved, dict):
            state.update(saved)
        if not isinstance(state.get("runs"), list):
            state["runs"] = []
        mutator(state)
        save_json(STATE_PATH, state)
        return state


def hint(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) < 8:
        return "tersimpan"
    return "…" + text[-4:]


def parse_github(value):
    match = GITHUB_REPO.match(str(value or "").strip())
    if not match:
        raise ValueError("tulis repo seperti gitgitmiko/dashboard_agent_cursor")
    owner, name = match.group(1), match.group(2)
    return "https://github.com/%s/%s" % (owner, name), "%s/%s" % (owner, name), name


def clean_branch(value):
    branch = str(value or "main").strip() or "main"
    if len(branch) > 80 or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
        raise ValueError("nama branch tidak valid")
    return branch


def clean_path(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if "\n" in text or "\r" in text or not text.startswith("/") or ".." in text.split("/"):
        raise ValueError("path lokal harus path absolut tanpa ..")
    return text


def clean_service(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if text in DENIED_SERVICES or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", text):
        raise ValueError("nama layanan systemd tidak diizinkan")
    return text


def write_allowlist(repos):
    names = []
    for repo in repos:
        service = str(repo.get("service") or "").strip()
        if service and service not in names and service not in DENIED_SERVICES:
            names.append(service)
    ALLOW_PATH.write_text("\n".join(names) + ("\n" if names else ""), encoding="utf-8")
    ALLOW_PATH.chmod(0o600)


def sync_allowlist():
    write_allowlist(get_config().get("repos") or [])


def last_commit(repo):
    path = str(repo.get("local_path") or "").strip()
    if not path or not Path(path, ".git").is_dir():
        return ""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-C", path, "log", "-1", "--pretty=format:%h %s"],
            capture_output=True,
            timeout=5,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    text = " ".join((completed.stdout or b"").decode("utf-8", "replace").split())
    if len(text) > 96:
        return text[:93].rstrip() + "…"
    return text


def public_view():
    cfg = get_config()
    state = get_state()
    runs = []
    for item in (state.get("runs") or [])[:40]:
        if not isinstance(item, dict):
            continue
        runs.append(
            {
                "time": item.get("time") or "",
                "repo": item.get("repo") or "",
                "mode": "auto" if item.get("mode") == "auto" else "custom",
                "model": item.get("model") or "",
                "total_tokens": int(item.get("total_tokens") or 0),
                "charged_cents": item.get("charged_cents"),
                "status": item.get("status") or "",
                "summary": item.get("summary") or "",
            }
        )
    usage = {"auto": {"tokens": 0, "runs": 0, "cents": 0}, "custom": {"tokens": 0, "runs": 0, "cents": 0}}
    for item in state.get("runs") or []:
        if not isinstance(item, dict):
            continue
        bucket = usage["auto" if item.get("mode") == "auto" else "custom"]
        bucket["runs"] += 1
        bucket["tokens"] += int(item.get("total_tokens") or 0)
        bucket["cents"] += float(item.get("charged_cents") or 0)
    repos = []
    for repo in cfg.get("repos") or []:
        repos.append(
            {
                "id": repo.get("id") or "",
                "name": repo.get("name") or "",
                "full_name": repo.get("full_name") or "",
                "url": repo.get("url") or "",
                "branch": repo.get("branch") or "main",
                "service": repo.get("service") or "",
                "last_commit": last_commit(repo),
            }
        )
    job = state.get("job") if isinstance(state.get("job"), dict) else None
    return {
        "usage": usage,
        "runs": runs,
        "repos": repos,
        "selected_repo_id": state.get("selected_repo_id") or "",
        "selected_model": state.get("selected_model") or "",
        "job": job,
        "model_mode": cfg.get("model_mode") or "auto",
        "custom_model": cfg.get("custom_model") or "composer-2.5",
        "chat_id": cfg.get("telegram_chat_id") or "",
        "cursor_ready": bool(str(cfg.get("cursor_api_key") or "").strip()),
        "telegram_ready": bool(str(cfg.get("telegram_token") or "").strip() and str(cfg.get("telegram_chat_id") or "").strip()),
        "github_ready": bool(str(cfg.get("github_token") or "").strip()),
        "cursor_hint": hint(cfg.get("cursor_api_key")),
        "telegram_hint": hint(cfg.get("telegram_token")),
        "github_hint": hint(cfg.get("github_token")),
    }


def save_settings(incoming):
    chat = str(incoming.get("telegram_chat_id") or "").strip()
    if chat and not re.fullmatch(r"-?\d{1,20}", chat):
        raise ValueError("chat ID harus angka")
    with lock:
        saved = load_json(CONFIG_PATH, default_config)
        cfg = default_config()
        if isinstance(saved, dict):
            cfg.update(saved)
        had_repos = isinstance(saved, dict) and "repos" in saved
        for field in ("cursor_api_key", "telegram_token", "github_token"):
            value = str(incoming.get(field) or "").strip()
            if value:
                cfg[field] = value
        if chat:
            cfg["telegram_chat_id"] = chat
        if not had_repos:
            cfg.pop("repos", None)
        save_json(CONFIG_PATH, cfg)
    return "Pengaturan disimpan"


def automatic_path(name):
    if name in ("harga-hbar", ".", "..") or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name or ""):
        raise ValueError("nama repo itu tidak bisa dipakai")
    return "/home/gitgitmiko/" + name


def draft_repo(incoming):
    url, full_name, name = parse_github(incoming.get("url"))
    branch = clean_branch(incoming.get("branch"))
    service = clean_service(incoming.get("service"))
    local_path = automatic_path(name)
    if any(item.get("full_name") == full_name for item in get_config().get("repos") or []):
        raise ValueError("repo itu sudah terdaftar")
    return {
        "name": name,
        "full_name": full_name,
        "url": url,
        "branch": branch,
        "local_path": local_path,
        "service": service,
    }


def add_repo(incoming):
    import secrets as _secrets

    drafted = draft_repo(incoming)
    with lock:
        saved = load_json(CONFIG_PATH, default_config)
        cfg = default_config()
        had_repos = isinstance(saved, dict) and "repos" in saved
        if isinstance(saved, dict):
            cfg.update(saved)
        repos = list(cfg.get("repos") or []) if had_repos else default_repos()
        if any(item.get("full_name") == drafted["full_name"] for item in repos):
            raise ValueError("repo itu sudah terdaftar")
        repos.append(
            {
                "id": _secrets.token_hex(4),
                "name": drafted["name"],
                "full_name": drafted["full_name"],
                "url": drafted["url"],
                "branch": drafted["branch"],
                "local_path": drafted["local_path"],
                "service": drafted["service"],
            }
        )
        cfg["repos"] = repos
        save_json(CONFIG_PATH, cfg)
        write_allowlist(repos)
    return "Repo ditambahkan dan kodenya sudah ditarik"


def remove_repo(repo_id):
    with lock:
        saved = load_json(CONFIG_PATH, default_config)
        cfg = default_config()
        had_repos = isinstance(saved, dict) and "repos" in saved
        if isinstance(saved, dict):
            cfg.update(saved)
        repos = list(cfg.get("repos") or []) if had_repos else default_repos()
        kept = [item for item in repos if item.get("id") != repo_id]
        if len(kept) == len(repos):
            raise ValueError("repo tidak ditemukan")
        cfg["repos"] = kept
        save_json(CONFIG_PATH, cfg)
        write_allowlist(kept)
        state = default_state()
        saved_state = load_json(STATE_PATH, default_state)
        if isinstance(saved_state, dict):
            state.update(saved_state)
        if state.get("selected_repo_id") == repo_id:
            state["selected_repo_id"] = ""
            save_json(STATE_PATH, state)
    return "Repo dihapus"
