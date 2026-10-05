import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import store

try:
    WIB = ZoneInfo("Asia/Jakarta")
except Exception:
    WIB = timezone(timedelta(hours=7))
job_lock = threading.Lock()
SECRET_TEXT = re.compile(
    r"(crsr_|cursor_|gho_|github_pat_)[A-Za-z0-9_\-]{6,}|[0-9]{6,12}:[A-Za-z0-9_\-]{20,}"
)


def now_wib():
    return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")


def redact(text):
    return SECRET_TEXT.sub("[rahasia]", str(text or ""))


def send_telegram(token, chat_id, text):
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": redact(text)[:4000]}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "Telegram menolak pesan")


def telegram_updates(token, offset, timeout=25):
    query = urllib.parse.urlencode(
        {"timeout": timeout, "offset": offset, "allowed_updates": json.dumps(["message"])}
    )
    url = "https://api.telegram.org/bot" + token + "/getUpdates?" + query
    with urllib.request.urlopen(url, timeout=35) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "Telegram tidak menjawab")
    return payload.get("result") or []


def repo_lines(repos):
    if not repos:
        return "Belum ada repo terdaftar di dasbor."
    lines = ["Repo yang terdaftar:"]
    for index, repo in enumerate(repos, start=1):
        where = repo.get("local_path") or "hanya di GitHub"
        lines.append("%s. %s (%s) · %s" % (index, repo.get("full_name"), repo.get("branch") or "main", where))
    lines.append("Pilih dengan /pilih nomor, lalu kirim perintah coding.")
    return "\n".join(lines)


def find_repo(repos, token):
    text = str(token or "").strip().lower()
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(repos):
            return repos[index]
        return None
    for repo in repos:
        names = {str(repo.get("name") or "").lower(), str(repo.get("full_name") or "").lower(), str(repo.get("id") or "").lower()}
        if text in names:
            return repo
    return None


def help_text():
    return "\n".join(
        [
            "Perintah dasbor Gitgitmiko:",
            "/repo — daftar repo yang terdaftar di web",
            "/pilih 1 — pilih repo sebelum coding",
            "/batal — lepaskan repo yang dipilih",
            "/status — pekerjaan yang sedang berjalan",
            "Setelah repo dipilih, kirim perintah biasa untuk memperbaiki bug, menambah fitur, lalu commit dan push.",
        ]
    )


def token_count(usage, name):
    if usage is None:
        return 0
    if isinstance(usage, dict):
        return int(usage.get(name) or 0)
    return int(getattr(usage, name, 0) or 0)


def record_run(repo, mode, model, status, summary, usage, charged_cents):
    entry = {
        "time": now_wib(),
        "repo": repo.get("full_name") or "",
        "mode": mode,
        "model": model,
        "status": status,
        "summary": redact(summary)[:500],
        "input_tokens": token_count(usage, "input_tokens"),
        "output_tokens": token_count(usage, "output_tokens"),
        "cache_read_tokens": token_count(usage, "cache_read_tokens"),
        "cache_write_tokens": token_count(usage, "cache_write_tokens"),
        "total_tokens": token_count(usage, "total_tokens"),
        "charged_cents": charged_cents,
    }

    def remember(state):
        runs = list(state.get("runs") or [])
        runs.insert(0, entry)
        state["runs"] = runs[:200]

    store.update_state(remember)


def pull_repo(repo, github_token):
    path = repo.get("local_path") or ""
    if not path:
        return ""
    folder = Path(path)
    if not (folder / ".git").is_dir():
        return "Path lokal belum berupa repo git, jadi kode di STB tidak ditarik."
    branch = repo.get("branch") or "main"
    command = ["git", "-C", str(folder), "pull", "--ff-only", "origin", branch]
    if github_token:
        basic = base64.b64encode(("x-access-token:" + github_token).encode()).decode()
        command = [
            "git",
            "-c",
            "http.extraheader=AUTHORIZATION: basic " + basic,
            "-C",
            str(folder),
            "pull",
            "--ff-only",
            "origin",
            branch,
        ]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(command, capture_output=True, env=env, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "Tarik kode gagal. " + redact(exc)
    output = redact(((completed.stdout or b"") + (completed.stderr or b"")).decode("utf-8", "replace"))
    if github_token:
        output = output.replace(github_token, "[rahasia]")
    if completed.returncode != 0:
        return "Tarik kode gagal. " + output[-400:]
    return "Kode di STB sudah ditarik."


def restart_service(service):
    if not service:
        return ""
    try:
        completed = subprocess.run(
            ["sudo", "-n", "/usr/local/sbin/gg-restart", service],
            capture_output=True,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "Layanan belum dijalankan ulang. " + redact(exc)
    if completed.returncode != 0:
        return "Layanan %s belum dijalankan ulang." % service
    return "Layanan %s dijalankan ulang." % service


def run_agent(repo, prompt, mode, model, api_key):
    from cursor_sdk import Agent, CloudAgentOptions, CloudRepository

    instructions = (
        "Kerjakan permintaan pemilik repo ini.\n"
        "Commit perubahan lalu push ke branch yang sedang dipakai.\n"
        "Jangan mengubah atau menampilkan config.json, state.json, token, atau kunci API.\n\n"
        "Permintaan:\n%s" % prompt
    )
    with Agent.create(
        model=model,
        api_key=api_key,
        cloud=CloudAgentOptions(
            repos=[CloudRepository(url=repo["url"], starting_ref=repo.get("branch") or "main")],
            work_on_current_branch=True,
            auto_create_pr=False,
            skip_reviewer_request=True,
        ),
    ) as agent:
        run = agent.send(instructions)
        result = run.wait()
        usage = getattr(result, "usage", None)
        charged = None
        try:
            billed = agent.get_usage()
            if billed is not None and getattr(billed, "cost", None) is not None:
                charged = getattr(billed.cost, "charged_cents", None)
            if usage is None and getattr(billed, "usage", None) is not None:
                usage = billed.usage
        except Exception:
            charged = None
        status = getattr(result, "status", "") or ""
        text = getattr(result, "result", "") or ""
        return status, text, usage, charged


def work(repo, prompt):
    cfg = store.get_config()
    api_key = str(cfg.get("cursor_api_key") or "").strip()
    mode = "auto" if cfg.get("model_mode") != "custom" else "custom"
    model = "auto" if mode == "auto" else (cfg.get("custom_model") or "composer-2.5")
    token = str(cfg.get("telegram_token") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    status = "error"
    summary = ""
    usage = None
    charged = None
    try:
        if not api_key:
            raise RuntimeError("API key Cursor belum diisi di Pengaturan")
        status, summary, usage, charged = run_agent(repo, prompt, mode, model, api_key)
    except Exception as exc:
        status = "error"
        summary = str(exc)
    finally:
        record_run(repo, mode, model, status or "error", summary or status, usage, charged)
        store.update_state(lambda state: state.update({"job": None}))
    follow = ""
    if status not in ("error", "cancelled", "canceled", ""):
        follow = pull_repo(repo, str(cfg.get("github_token") or "").strip())
    message = "Selesai di %s (%s).\n%s" % (repo.get("full_name"), status or "selesai", redact(summary)[:2500])
    if follow:
        message += "\n\n" + follow
    will_restart = status not in ("error", "cancelled", "canceled", "") and follow.startswith("Kode di STB") and repo.get("service")
    if will_restart:
        message += "\nLayanan %s akan dijalankan ulang." % repo.get("service")
    if token and chat_id:
        try:
            send_telegram(token, chat_id, message)
        except Exception:
            pass
    if will_restart:
        restart_service(repo.get("service") or "")


def handle_message(text):
    cfg = store.get_config()
    repos = cfg.get("repos") or []
    state = store.get_state()
    cleaned = text.strip()
    command = cleaned.split()[0].lower() if cleaned.startswith("/") else ""
    if command in ("/start", "/help", "/bantuan"):
        return help_text()
    if command in ("/repo", "/repos", "/daftar"):
        return repo_lines(repos)
    if command == "/batal":
        store.update_state(lambda state: state.update({"selected_repo_id": ""}))
        return "Repo yang dipilih dilepas. Kirim /repo untuk melihat daftar."
    if command == "/status":
        job = state.get("job") if isinstance(state.get("job"), dict) else None
        if not job:
            return "Tidak ada pekerjaan yang sedang berjalan."
        return "Sedang mengerjakan %s sejak %s." % (job.get("repo") or "repo", job.get("since") or "")
    if command == "/pilih":
        parts = cleaned.split(maxsplit=1)
        if len(parts) < 2:
            return "Contoh: /pilih 1"
        repo = find_repo(repos, parts[1])
        if not repo:
            return "Repo itu tidak ada di daftar.\n" + repo_lines(repos)
        store.update_state(lambda state: state.update({"selected_repo_id": repo.get("id") or ""}))
        return "Repo dipilih: %s. Kirim perintah coding sekarang." % repo.get("full_name")
    if command.startswith("/"):
        return "Perintah tidak dikenal.\n" + help_text()
    selected = next((repo for repo in repos if repo.get("id") == state.get("selected_repo_id")), None)
    if not selected:
        return "Pilih repo dulu.\n" + repo_lines(repos)
    if len(cleaned) > 4000:
        return "Perintah terlalu panjang. Maksimal 4000 karakter."
    claimed = {"ok": False}

    def claim(current):
        if isinstance(current.get("job"), dict):
            return
        current["job"] = {"repo": selected.get("full_name"), "since": now_wib(), "prompt": cleaned[:160]}
        claimed["ok"] = True

    with job_lock:
        if isinstance(store.get_state().get("job"), dict):
            return "Masih mengerjakan %s. Tunggu sampai selesai." % store.get_state()["job"].get("repo")
        store.update_state(claim)
    if not claimed["ok"]:
        return "Masih ada pekerjaan yang berjalan. Tunggu sampai selesai."
    threading.Thread(target=work, args=(selected, cleaned), daemon=True).start()
    return "Menggarap %s. Hasilnya dikirim ke sini setelah agen selesai." % selected.get("full_name")


def skip_old_messages(token):
    if store.has_state_key("telegram_offset"):
        return
    offset = 0
    while True:
        updates = telegram_updates(token, offset, timeout=0)
        if not updates:
            break
        offset = max(int(update.get("update_id") or 0) for update in updates) + 1
    store.update_state(lambda state, offset=offset: state.update({"telegram_offset": offset}))


def poll_loop():
    skipped = False
    while True:
        cfg = store.get_config()
        token = str(cfg.get("telegram_token") or "").strip()
        chat_id = str(cfg.get("telegram_chat_id") or "").strip()
        if not token or not chat_id:
            time.sleep(5)
            continue
        if not skipped:
            try:
                skip_old_messages(token)
            except Exception:
                time.sleep(5)
                continue
            skipped = True
        offset = int(store.get_state().get("telegram_offset") or 0)
        try:
            updates = telegram_updates(token, offset)
        except Exception:
            time.sleep(5)
            continue
        for update in updates:
            update_id = int(update.get("update_id") or 0)
            offset = max(offset, update_id + 1)
            store.update_state(lambda state, offset=offset: state.update({"telegram_offset": offset}))
            message = update.get("message") or {}
            sender = str((message.get("chat") or {}).get("id") or "")
            text = message.get("text") or ""
            if sender == chat_id and text.strip():
                try:
                    reply = handle_message(text)
                    send_telegram(token, chat_id, reply)
                except Exception as exc:
                    try:
                        send_telegram(token, chat_id, "Perintah gagal. " + redact(exc))
                    except Exception:
                        pass


def send_test_message():
    cfg = store.get_config()
    token = str(cfg.get("telegram_token") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        raise ValueError("isi token bot dan chat ID, lalu simpan")
    send_telegram(token, chat_id, "Percobaan dari dasbor Gitgitmiko. Koneksi Telegram berhasil.")
    return "Pesan percobaan terkirim"


def start_background():
    store.ensure_secrets()
    if not store.CONFIG_PATH.exists():
        store.save_json(store.CONFIG_PATH, {})
    store.sync_allowlist()
    threading.Thread(target=poll_loop, daemon=True, name="telegram").start()
