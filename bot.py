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
        lines.append("%s. %s (%s)" % (index, repo.get("full_name"), repo.get("branch") or "main"))
    lines.append("Pilih dengan /pilih nomor, lalu pilih model.")
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
            "/pilih 1 — pilih repo",
            "/model — pilih ulang Auto atau custom",
            "/batal — lepaskan repo dan model",
            "/status — repo, model, dan pekerjaan yang sedang berjalan",
            "Urutannya: pilih repo, pilih model, lalu kirim perintah coding.",
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


def checkout_repo(repo):
    import shutil

    name = str(repo.get("name") or "")
    path = Path("/home/gitgitmiko") / name
    if path.as_posix() != "/home/gitgitmiko/" + name or name in ("harga-hbar", ".", ".."):
        raise ValueError("nama repo itu tidak bisa dipakai")
    token = str(store.get_config().get("github_token") or "").strip()
    branch = repo.get("branch") or "main"
    url = repo.get("url") or ""
    created = not path.exists()
    try:
        if path.exists() and (path / ".git").is_dir():
            origin = git_output(["git", "-C", str(path), "remote", "get-url", "origin"], token)
            if not same_github(origin, url):
                raise ValueError("nama repo itu sudah dipakai folder lain")
            git_output(["git", "-C", str(path), "pull", "--ff-only", "origin", branch], token)
            return
        if path.exists():
            raise ValueError("nama repo itu sudah dipakai folder lain")
        git_output(["git", "clone", "--branch", branch, "--single-branch", url, str(path)], token)
    except ValueError:
        if created and path.exists():
            shutil.rmtree(path, ignore_errors=True)
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        if created and path.exists():
            shutil.rmtree(path, ignore_errors=True)
        raise ValueError("Kode belum berhasil ditarik.") from exc


def same_github(left, right):
    def norm(value):
        text = str(value or "").strip().rstrip("/")
        if text.endswith(".git"):
            text = text[:-4]
        return text.lower()

    return norm(left) == norm(right)


def git_output(command, github_token):
    if github_token:
        basic = base64.b64encode(("x-access-token:" + github_token).encode()).decode()
        command = ["git", "-c", "http.extraheader=AUTHORIZATION: basic " + basic, *command[1:]]
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(command, capture_output=True, env=env, timeout=120)
    output = redact(((completed.stdout or b"") + (completed.stderr or b"")).decode("utf-8", "replace")).strip()
    if github_token:
        output = output.replace(github_token, "[rahasia]")
    if completed.returncode != 0:
        raise ValueError("Kode belum berhasil ditarik.")
    return output


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


CLOUD_DONE = {
    "FINISHED": "finished",
    "ERROR": "error",
    "CANCELLED": "cancelled",
    "CANCELED": "cancelled",
    "EXPIRED": "expired",
}


def cursor_json(api_key, path):
    request = urllib.request.Request(
        "https://api.cursor.com" + path,
        headers={"Authorization": "Bearer " + api_key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def cloud_assistant_text(api_key, agent_id):
    payload = cursor_json(api_key, "/v0/agents/" + urllib.parse.quote(agent_id) + "/conversation")
    messages = payload.get("messages") if isinstance(payload, dict) else []
    text = ""
    for message in messages or []:
        if not isinstance(message, dict) or message.get("type") != "assistant_message":
            continue
        body = message.get("text") or ""
        if isinstance(body, str) and body.strip():
            text = body.strip()
    return text


def await_cloud_agent(api_key, agent_id):
    deadline = time.time() + 25 * 60
    path = "/v0/agents/" + urllib.parse.quote(agent_id)
    while True:
        raw = ""
        try:
            detail = cursor_json(api_key, path)
            raw = str(detail.get("status") or "").upper()
        except Exception:
            raw = ""
        if raw in CLOUD_DONE:
            text = ""
            try:
                text = cloud_assistant_text(api_key, agent_id)
            except Exception:
                text = ""
            if not text:
                text = "Agen cloud berstatus %s." % CLOUD_DONE[raw]
            return CLOUD_DONE[raw], text
        if time.time() >= deadline:
            return "error", "Agen cloud belum selesai dalam 25 menit."
        time.sleep(8)


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
        agent.send(instructions)
        agent_id = str(getattr(agent, "agent_id", "") or "")
        if not agent_id:
            raise RuntimeError("Agen cloud tidak mengembalikan id")
        status, text = await_cloud_agent(api_key, agent_id)
        usage = None
        charged = None
        try:
            billed = agent.get_usage()
            if billed is not None and getattr(billed, "cost", None) is not None:
                charged = getattr(billed.cost, "charged_cents", None)
            if usage is None and getattr(billed, "usage", None) is not None:
                usage = billed.usage
        except Exception:
            charged = None
        return status, text, usage, charged


def list_custom_models(api_key):
    from cursor_sdk import Cursor

    listed = Cursor.models.list(api_key=api_key)
    if hasattr(listed, "models"):
        listed = listed.models
    skipped = {"auto", "auto-smart", "default"}
    items = []
    seen = set()
    for model in listed or []:
        model_id = str(getattr(model, "id", "") or "").strip()
        if not model_id or model_id in skipped or model_id in seen:
            continue
        seen.add(model_id)
        name = str(getattr(model, "display_name", "") or getattr(model, "name", "") or model_id).strip()
        items.append({"id": model_id, "name": name or model_id})
    return items[:40]


def clear_selection(state):
    state.update(
        {
            "selected_repo_id": "",
            "model_step": "",
            "selected_model": "",
            "model_catalog": [],
        }
    )


def ask_model_kind(repo):
    return "\n".join(
        [
            "Repo dipilih: %s." % repo.get("full_name"),
            "Pilih model:",
            "1. Auto",
            "2. Custom — daftar model di akun Cursor",
            "Kirim 1 atau 2.",
        ]
    )


def catalog_text(items):
    lines = ["Model custom di akun Cursor:"]
    for index, item in enumerate(items, start=1):
        name = item.get("name") or item.get("id")
        if name == item.get("id"):
            lines.append("%s. %s" % (index, name))
        else:
            lines.append("%s. %s (%s)" % (index, name, item.get("id")))
    lines.append("Kirim nomor model.")
    return "\n".join(lines)


def work(repo, prompt, model):
    cfg = store.get_config()
    api_key = str(cfg.get("cursor_api_key") or "").strip()
    mode = "auto" if model == "auto" else "custom"
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
    message = "Selesai di %s dengan %s (%s).\n%s" % (
        repo.get("full_name"),
        "Auto" if model == "auto" else model,
        status or "selesai",
        redact(summary)[:2500],
    )
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
        store.update_state(clear_selection)
        return "Repo dan model dilepas. Kirim /repo untuk melihat daftar."
    if command == "/status":
        return status_text(repos, state)
    if command == "/pilih":
        return choose_repo(repos, cleaned)
    if command == "/model":
        return reopen_model(repos, state)
    if command.startswith("/"):
        return "Perintah tidak dikenal.\n" + help_text()
    selected = next((repo for repo in repos if repo.get("id") == state.get("selected_repo_id")), None)
    if not selected:
        return "Pilih repo dulu.\n" + repo_lines(repos)
    step = state.get("model_step") or "kind"
    if step == "kind":
        return choose_model_kind(selected, cleaned, cfg)
    if step == "catalog":
        return choose_catalog_model(cleaned, state)
    if len(cleaned) > 4000:
        return "Perintah terlalu panjang. Maksimal 4000 karakter."
    model = state.get("selected_model") or ""
    if not model:
        return ask_model_kind(selected)
    return start_job(selected, cleaned, model)


def status_text(repos, state):
    selected = next((repo for repo in repos if repo.get("id") == state.get("selected_repo_id")), None)
    lines = ["Repo: %s." % (selected.get("full_name") if selected else "belum dipilih")]
    model = state.get("selected_model") or ""
    if state.get("model_step") == "ready" and model:
        lines.append("Model: %s." % ("Auto" if model == "auto" else model))
    else:
        lines.append("Model: belum dipilih.")
    job = state.get("job") if isinstance(state.get("job"), dict) else None
    if job:
        lines.append("Sedang mengerjakan %s sejak %s." % (job.get("repo") or "repo", job.get("since") or ""))
    else:
        lines.append("Tidak ada pekerjaan yang sedang berjalan.")
    return "\n".join(lines)


def choose_repo(repos, cleaned):
    parts = cleaned.split(maxsplit=1)
    if len(parts) < 2:
        return "Contoh: /pilih 1"
    repo = find_repo(repos, parts[1])
    if not repo:
        return "Repo itu tidak ada di daftar.\n" + repo_lines(repos)
    store.update_state(
        lambda state, repo_id=repo.get("id") or "": state.update(
            {
                "selected_repo_id": repo_id,
                "model_step": "kind",
                "selected_model": "",
                "model_catalog": [],
            }
        )
    )
    return ask_model_kind(repo)


def reopen_model(repos, state):
    selected = next((repo for repo in repos if repo.get("id") == state.get("selected_repo_id")), None)
    if not selected:
        return "Pilih repo dulu.\n" + repo_lines(repos)
    store.update_state(
        lambda current: current.update({"model_step": "kind", "selected_model": "", "model_catalog": []})
    )
    return ask_model_kind(selected)


def choose_model_kind(repo, cleaned, cfg):
    answer = cleaned.lower()
    if answer in ("1", "auto"):
        store.update_state(
            lambda state: state.update({"model_step": "ready", "selected_model": "auto", "model_catalog": []})
        )
        return "Model: Auto. Kirim perintah coding sekarang."
    if answer in ("2", "custom"):
        api_key = str(cfg.get("cursor_api_key") or "").strip()
        if not api_key:
            return "API key Cursor belum diisi di Pengaturan."
        try:
            items = list_custom_models(api_key)
        except Exception as exc:
            return "Daftar model Cursor belum bisa diambil. " + redact(exc)
        if not items:
            return "Akun Cursor ini belum punya model custom. Pilih 1 untuk Auto."
        store.update_state(
            lambda state, items=items: state.update({"model_step": "catalog", "model_catalog": items, "selected_model": ""})
        )
        return catalog_text(items)
    return ask_model_kind(repo)


def choose_catalog_model(cleaned, state):
    items = state.get("model_catalog") if isinstance(state.get("model_catalog"), list) else []
    if not cleaned.isdigit():
        return "Kirim nomor model.\n" + catalog_text(items)
    index = int(cleaned) - 1
    if index < 0 or index >= len(items):
        return "Nomor itu tidak ada.\n" + catalog_text(items)
    chosen = items[index]
    store.update_state(
        lambda current, model_id=chosen.get("id") or "": current.update(
            {"model_step": "ready", "selected_model": model_id, "model_catalog": []}
        )
    )
    return "Model: %s. Kirim perintah coding sekarang." % (chosen.get("name") or chosen.get("id"))


def start_job(selected, cleaned, model):
    claimed = {"ok": False}

    def claim(current):
        if isinstance(current.get("job"), dict):
            return
        current["job"] = {
            "repo": selected.get("full_name"),
            "model": "Auto" if model == "auto" else model,
            "since": now_wib(),
            "prompt": cleaned[:160],
        }
        claimed["ok"] = True

    with job_lock:
        if isinstance(store.get_state().get("job"), dict):
            return "Masih mengerjakan %s. Tunggu sampai selesai." % store.get_state()["job"].get("repo")
        store.update_state(claim)
    if not claimed["ok"]:
        return "Masih ada pekerjaan yang berjalan. Tunggu sampai selesai."
    threading.Thread(target=work, args=(selected, cleaned, model), daemon=True).start()
    label = "Auto" if model == "auto" else model
    return "Menggarap %s dengan %s. Hasilnya dikirim ke sini setelah agen selesai." % (selected.get("full_name"), label)


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
