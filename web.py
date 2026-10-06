import hashlib
import hmac
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import bot
import hermes_local
import store

templates = Jinja2Templates(directory=str(store.ROOT / "templates"))
SESSION_COOKIE = "hb_session"
FORM_COOKIE = "hb_form"
SESSION_MAX_AGE = 60 * 60 * 12
BASE = "/agen"
PASSWORD_MIN = 10
PASSWORD_MAX = 128
failures = {}


@asynccontextmanager
async def lifespan(_app):
    bot.start_background()
    yield


site = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
site.mount("/static", StaticFiles(directory=str(store.ROOT / "static")), name="static")
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount(BASE, site)


def redirect(path):
    return RedirectResponse(BASE + path, status_code=303)


@site.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    content_type = response.headers.get("content-type", "")
    if request.url.path.startswith("/api") or response.media_type == "text/html" or content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


def serializer():
    return URLSafeTimedSerializer(store.ensure_secrets(), salt="hb-session")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + salt.hex() + "$" + digest.hex()


def verify_password(password, stored):
    try:
        scheme, salt_hex, digest_hex = str(stored or "").split("$", 2)
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def has_password():
    return bool(store.get_config().get("password_hash"))


def client_key(request):
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded and forwarded != "127.0.0.1":
        return forwarded
    if request.client:
        return request.client.host or "local"
    return "local"


def locked_out(key):
    now = time.time()
    recent = [stamp for stamp in failures.get(key, []) if now - stamp < 900]
    failures[key] = recent
    return len(recent) >= 8


def mark_failure(key):
    failures.setdefault(key, []).append(time.time())


def clear_failures(key):
    failures.pop(key, None)


def read_session(request):
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not data.get("csrf"):
        return None
    return data


def request_is_https(request):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host == "gitgitmiko.my.id" or host.endswith(".gitgitmiko.my.id"):
        return True
    if request.client and request.client.host not in ("127.0.0.1", "::1"):
        return False
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    visitor = request.headers.get("cf-visitor") or ""
    return "https" in visitor


def write_session(response, request):
    token = secrets.token_urlsafe(24)
    response.set_cookie(
        SESSION_COOKIE,
        serializer().dumps({"csrf": token}),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=request_is_https(request),
        path=BASE,
    )
    return token


def render_login(request, mode, error, status_code=200):
    token = secrets.token_urlsafe(24)
    response = templates.TemplateResponse(
        "login.html",
        {"request": request, "mode": mode, "error": error, "form_csrf": token, "base": BASE},
        status_code=status_code,
    )
    response.set_cookie(
        FORM_COOKIE,
        token,
        max_age=1800,
        httponly=True,
        samesite="strict",
        secure=request_is_https(request),
        path=BASE,
    )
    return response


def form_token_ok(request, submitted):
    saved = request.cookies.get(FORM_COOKIE) or ""
    given = submitted or ""
    if not saved or not given:
        return False
    return hmac.compare_digest(saved, given)


def require_session(request):
    return read_session(request)


def require_csrf(request, session):
    given = request.headers.get("x-csrf-token") or ""
    expected = session.get("csrf") or ""
    if not given or not expected or not hmac.compare_digest(given, expected):
        return JSONResponse({"error": "sesi tidak valid, muat ulang halaman"}, status_code=403)
    return None


def check_password_rules(password, confirm):
    if password != confirm:
        raise ValueError("ulangi kata sandi tidak sama")
    if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
        raise ValueError("kata sandi minimal 10 karakter")


def store_password(password):
    digest = hash_password(password)
    with store.lock:
        saved = store.load_json(store.CONFIG_PATH, store.default_config)
        cfg = store.default_config()
        if isinstance(saved, dict):
            cfg.update(saved)
        had_repos = isinstance(saved, dict) and "repos" in saved
        cfg["password_hash"] = digest
        if not had_repos:
            cfg.pop("repos", None)
        store.save_json(store.CONFIG_PATH, cfg)


def guard(request):
    session = require_session(request)
    if not session:
        return None, JSONResponse({"error": "masuk dulu"}, status_code=401)
    denied = require_csrf(request, session)
    if denied:
        return None, denied
    return session, None


async def read_json(request):
    try:
        incoming = await request.json()
    except Exception:
        return None
    if not isinstance(incoming, dict):
        return None
    return incoming


@site.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not has_password():
        return redirect("/setup")
    session = require_session(request)
    if not session:
        return redirect("/login")
    return templates.TemplateResponse("app.html", {"request": request, "csrf": session["csrf"], "base": BASE})


@site.get("/pengaturan", response_class=HTMLResponse)
def settings_page(request: Request):
    if not has_password():
        return redirect("/setup")
    session = require_session(request)
    if not session:
        return redirect("/login")
    return templates.TemplateResponse("settings.html", {"request": request, "csrf": session["csrf"], "base": BASE})


@site.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if has_password():
        return redirect("/login")
    return render_login(request, "setup", "")


@site.post("/setup")
def setup_submit(request: Request, password: str = Form(""), confirm: str = Form(""), csrf: str = Form("")):
    if has_password():
        return redirect("/login")
    if not form_token_ok(request, csrf):
        return render_login(request, "setup", "Halaman kedaluwarsa. Muat ulang, lalu coba lagi.", 400)
    try:
        check_password_rules(password, confirm)
    except ValueError as exc:
        return render_login(request, "setup", str(exc), 400)
    store_password(password)
    response = redirect("/")
    write_session(response, request)
    response.delete_cookie(FORM_COOKIE, path=BASE)
    response.delete_cookie(FORM_COOKIE, path="/")
    return response


@site.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not has_password():
        return redirect("/setup")
    if require_session(request):
        return redirect("/")
    return render_login(request, "login", "")


@site.post("/login")
def login_submit(request: Request, password: str = Form(""), csrf: str = Form("")):
    if not has_password():
        return redirect("/setup")
    key = client_key(request)
    if not form_token_ok(request, csrf):
        return render_login(request, "login", "Halaman kedaluwarsa. Muat ulang, lalu coba lagi.", 401)
    if locked_out(key):
        return render_login(request, "login", "Terlalu banyak percobaan. Tunggu 15 menit.", 429)
    if not verify_password(password, store.get_config().get("password_hash")):
        mark_failure(key)
        return render_login(request, "login", "Kata sandi salah.", 401)
    clear_failures(key)
    response = redirect("/")
    write_session(response, request)
    response.delete_cookie(FORM_COOKIE, path=BASE)
    response.delete_cookie(FORM_COOKIE, path="/")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@site.post("/logout")
def logout(request: Request):
    session = require_session(request)
    if session:
        denied = require_csrf(request, session)
        if denied:
            return denied
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path=BASE)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@site.get("/api/dashboard")
def dashboard(request: Request):
    session = require_session(request)
    if not session:
        return JSONResponse({"error": "masuk dulu"}, status_code=401)
    return store.public_view()


@site.post("/api/settings")
async def settings(request: Request):
    _session, denied = guard(request)
    if denied:
        return denied
    incoming = await read_json(request)
    if incoming is None:
        return JSONResponse({"error": "permintaan tidak valid"}, status_code=400)
    try:
        message = store.save_settings(incoming)
        message += hermes_local.sync(store.get_config())
        return {"message": message}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@site.post("/api/repos")
async def create_repo(request: Request):
    _session, denied = guard(request)
    if denied:
        return denied
    incoming = await read_json(request)
    if incoming is None:
        return JSONResponse({"error": "permintaan tidak valid"}, status_code=400)
    try:
        drafted = store.draft_repo(incoming)
        bot.checkout_repo(drafted)
        return {"message": store.add_repo(incoming)}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@site.post("/api/repos/remove")
async def delete_repo(request: Request):
    _session, denied = guard(request)
    if denied:
        return denied
    incoming = await read_json(request)
    if incoming is None:
        return JSONResponse({"error": "permintaan tidak valid"}, status_code=400)
    try:
        return {"message": store.remove_repo(str(incoming.get("id") or ""))}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@site.post("/api/test-telegram")
def test_telegram(request: Request):
    _session, denied = guard(request)
    if denied:
        return denied
    try:
        return {"message": bot.send_test_message()}
    except Exception as exc:
        return JSONResponse({"error": bot.redact(exc)}, status_code=400)


@site.post("/api/password")
async def change_password(request: Request):
    _session, denied = guard(request)
    if denied:
        return denied
    incoming = await read_json(request)
    if incoming is None:
        return JSONResponse({"error": "permintaan tidak valid"}, status_code=400)
    current = str(incoming.get("current") or "")
    new = str(incoming.get("new") or "")
    confirm = str(incoming.get("confirm") or "")
    if not verify_password(current, store.get_config().get("password_hash")):
        return JSONResponse({"error": "kata sandi sekarang salah"}, status_code=400)
    try:
        check_password_rules(new, confirm)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    store_password(new)
    return {"message": "Kata sandi diperbarui"}
