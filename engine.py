#!/usr/bin/env python3
"""Pemantau harga HBAR/USDT dari data publik Tokocrypto."""

import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
KLINE_INTERVALS = {"15m", "1h", "4h", "1d", "1w"}
kline_cache = {}
HOST = "127.0.0.1"
PORT = 8787
WIB = ZoneInfo("Asia/Jakarta")

SIDE_LABEL = {0: "Beli", 1: "Jual"}
STATUS_LABEL = {
    -2: "Diproses",
    0: "Baru",
    1: "Sebagian terisi",
    2: "Terisi",
    3: "Dibatalkan",
    4: "Menunggu pembatalan",
    5: "Ditolak",
    6: "Kedaluwarsa",
}
TYPE_LABEL = {
    1: "Limit",
    2: "Market",
    3: "Stop",
    4: "Stop limit",
    5: "Take profit",
    6: "Take profit limit",
    7: "Limit maker",
}

lock = threading.Lock()
latest = {
    "ok": False,
    "error": "belum ada data",
    "price": None,
    "change_percent": None,
    "high": None,
    "low": None,
    "updated_at": None,
}
account_view = {
    "ok": False,
    "error": "Data akun belum diambil",
    "updated_at": None,
    "maker_fee": "-",
    "taker_fee": "-",
    "total_idr": "…",
    "idr_note": "Menghitung kurs IDR…",
    "balances": [],
    "open_orders": [],
    "order_history": [],
    "trades": [],
}
plan_view = {
    "status": "Menghitung rencana posisi…",
    "detail": "",
}


def now_wib():
    return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")


def default_config():
    return {
        "above": None,
        "below": None,
        "telegram_token": "",
        "telegram_chat_id": "",
        "tokocrypto_api_key": "",
        "tokocrypto_api_secret": "",
        "pair": "HBAR/USDT",
        "poll_seconds": 20,
    }


def default_state():
    return {
        "above_fired": False,
        "below_fired": False,
        "last_alert": None,
        "last_alert_error": None,
    }


def load_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback()


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    path.chmod(0o600)


def get_config():
    with lock:
        cfg = default_config()
        cfg.update(load_json(CONFIG_PATH, default_config))
        return cfg


def get_state():
    with lock:
        state = default_state()
        state.update(load_json(STATE_PATH, default_state))
        return state


def parse_pair(value):
    text = str(value or "").strip().upper().replace(" ", "").replace("-", "/")
    if "_" in text and "/" not in text:
        text = text.replace("_", "/")
    if "/" not in text:
        for quote in ("USDT", "IDR", "BUSD", "USDC", "BTC", "ETH"):
            if text.endswith(quote) and len(text) > len(quote):
                text = text[: -len(quote)] + "/" + quote
                break
    if text.count("/") != 1:
        raise ValueError("tulis pair seperti BNB/USDT")
    base, quote = text.split("/")
    if not base.isalnum() or not quote.isalnum():
        raise ValueError("pair hanya boleh huruf dan angka")
    if not (2 <= len(base) <= 12 and 2 <= len(quote) <= 10):
        raise ValueError("pair tidak dikenal")
    return base + "/" + quote


def market_symbol(pair):
    base, quote = pair.split("/")
    return base + quote


def order_symbol(pair):
    base, quote = pair.split("/")
    return base + "_" + quote


def current_pair():
    cfg = get_config()
    raw = cfg.get("pair") or "HBAR/USDT"
    try:
        return parse_pair(raw)
    except ValueError:
        return "HBAR/USDT"


def ensure_pair(pair):
    url = "https://www.tokocrypto.site/api/v3/ticker/price?symbol=" + urllib.parse.quote(market_symbol(pair))
    request = urllib.request.Request(url, headers={"User-Agent": "harga-hbar/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise ValueError("pair " + pair + " tidak ada di Tokocrypto")
    if not payload.get("price"):
        raise ValueError("pair " + pair + " tidak ada di Tokocrypto")
    return pair


def clear_pair_plan(state, pair):
    state["pair"] = pair
    state["phase"] = "menunggu"
    state["plan_key"] = None
    state["buy_sent"] = False
    state["partial_sent"] = False
    state["trail_sent"] = False
    state["invalid_sent"] = False
    state["entry"] = None
    state["high_since"] = None
    state["entry_target"] = None
    state["entry_trail"] = None
    state["entry_invalidation"] = None


def parse_optional_number(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if text == "":
        return None
    number = float(text)
    if number <= 0:
        raise ValueError("harga batas harus lebih dari 0")
    return number


def fetch_ticker():
    url = "https://www.tokocrypto.site/api/v3/ticker/24hr?symbol=" + urllib.parse.quote(market_symbol(current_pair()))
    request = urllib.request.Request(url, headers={"User-Agent": "harga-hbar/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return {
        "ok": True,
        "error": None,
        "price": float(payload["lastPrice"]),
        "change_percent": float(payload["priceChangePercent"]),
        "high": float(payload["highPrice"]),
        "low": float(payload["lowPrice"]),
        "updated_at": now_wib(),
    }


def fetch_klines(interval):
    if interval not in KLINE_INTERVALS:
        raise ValueError("timeframe tidak dikenal")
    symbol = market_symbol(current_pair())
    cache_key = symbol + "|" + interval
    now = time.time()
    with lock:
        cached = kline_cache.get(cache_key)
    if cached and now - cached[0] < 20:
        return cached[1]
    url = (
        "https://www.tokocrypto.site/api/v3/klines?symbol="
        + urllib.parse.quote(symbol)
        + "&interval="
        + urllib.parse.quote(interval)
        + "&limit=120"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "harga-hbar/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    candles = []
    for row in payload:
        candles.append(
            {
                "time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            }
        )
    with lock:
        kline_cache[cache_key] = (now, candles)
    return candles


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = price * k + value * (1 - k)
    return value


def average_true_range(candles, period=14):
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["close"]
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))
    value = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        value = ((value * (period - 1)) + true_range) / period
    return value


def swing_points(candles, wing=2):
    highs = []
    lows = []
    for index in range(wing, len(candles) - wing):
        window = candles[index - wing:index + wing + 1]
        if candles[index]["high"] >= max(item["high"] for item in window):
            highs.append(candles[index])
        if candles[index]["low"] <= min(item["low"] for item in window):
            lows.append(candles[index])
    return highs, lows


def build_plan(price):
    daily = fetch_klines("1d")
    four_hour = fetch_klines("4h")
    if len(daily) < 55 or len(four_hour) < 30 or not price:
        raise RuntimeError("Candle belum cukup untuk menghitung rencana")
    closed_daily = daily[:-1]
    closed_four_hour = four_hour[:-1]
    daily_closes = [item["close"] for item in closed_daily]
    ema20 = ema(daily_closes, 20)
    ema50 = ema(daily_closes, 50)
    last_daily = daily_closes[-1]
    if ema20 and ema50 and last_daily > ema20 > ema50:
        trend = "naik"
    elif ema20 and ema50 and last_daily < ema20 < ema50:
        trend = "turun"
    else:
        trend = "menyamping"
    volatility = average_true_range(closed_four_hour, 14)
    if not volatility:
        raise RuntimeError("ATR belum bisa dihitung")
    highs, lows = swing_points(closed_four_hour, 2)
    supports = [item for item in lows if item["low"] < price]
    if not supports:
        return {
            "ready": False,
            "trend": trend,
            "status": "Belum ada support di bawah harga.",
            "detail": "Arah harian: %s." % trend,
        }
    support = max(supports, key=lambda item: item["low"])
    zone_low = support["low"]
    zone_high = min(price * 0.999, zone_low + (0.5 * volatility))
    if zone_high <= zone_low:
        zone_high = zone_low + (0.25 * volatility)
    resistances = [item["high"] for item in highs if item["high"] > price]
    if not resistances:
        recent_high = max(item["high"] for item in closed_four_hour[-30:])
        if recent_high > price:
            resistances = [recent_high]
    target = min(resistances) if resistances else None
    fib_note = "Fibonacci tidak berhimpit dengan zona beli."
    if lows and highs:
        anchor_low = lows[-1]
        later_highs = [item for item in highs if item["time"] > anchor_low["time"]]
        if later_highs:
            anchor_high = later_highs[-1]["high"]
            span = anchor_high - anchor_low["low"]
            if span > 0:
                fib_levels = (
                    anchor_high - (0.382 * span),
                    anchor_high - (0.618 * span),
                )
                if any(zone_low <= level <= zone_high for level in fib_levels):
                    fib_note = "Zona beli berhimpit dengan Fibonacci 38,2% atau 61,8%."
    four_hour_close = closed_four_hour[-1]["close"]
    return {
        "ready": True,
        "trend": trend,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "target": target,
        "trail": 1.5 * volatility,
        "invalidation": zone_low,
        "four_hour_close": four_hour_close,
        "fib_note": fib_note,
        "key": "%.5f:%.5f:%s" % (zone_low, zone_high, "-" if target is None else "%.5f" % target),
    }


def plan_detail(plan, extra):
    lines = [
        "Arah harian: %s." % plan.get("trend", "-"),
        "Zona beli: %s – %s." % (format_price(plan.get("zone_low")), format_price(plan.get("zone_high"))),
        "Batal jika candle 4 jam ditutup di bawah %s." % format_price(plan.get("invalidation")),
    ]
    if plan.get("target"):
        lines.append("Jual sebagian di %s." % format_price(plan.get("target")))
    lines.append("Jual sisa setelah harga turun %s dari puncak sejak beli." % format_price(plan.get("trail")))
    lines.append(plan.get("fib_note") or "")
    if extra:
        lines.append(extra)
    return "\n".join(line for line in lines if line)


def publish_plan(status, detail):
    with lock:
        plan_view["status"] = status
        plan_view["detail"] = detail


def notice_pair(item):
    stored = str((item or {}).get("pair") or "").strip().upper()
    if "/" in stored:
        return stored
    match = re.search(r"\b([A-Z0-9]{2,12}/[A-Z0-9]{2,12})\b", str((item or {}).get("text") or ""))
    return match.group(1) if match else ""


def remember_notice(text, error, state=None, pair=None):
    entry = {
        "time": now_wib(),
        "text": text,
        "status": "Terkirim" if error is None else "Gagal",
        "error": error,
        "pair": notice_pair({"pair": pair, "text": text}) or current_pair(),
    }

    def apply(target):
        notices = [entry]
        counts = {entry["pair"]: 1}
        for item in target.get("notices") or []:
            key = notice_pair(item) or "_"
            count = counts.get(key, 0) + 1
            if count > 40:
                continue
            counts[key] = count
            notices.append(item)
        target["notices"] = notices

    if state is not None:
        apply(state)
        return
    with lock:
        current = default_state()
        current.update(load_json(STATE_PATH, default_state))
        apply(current)
        current["last_alert"] = entry["time"]
        current["last_alert_error"] = error
        save_json(STATE_PATH, current)


def deliver_messages(messages, state):
    if not messages:
        return
    cfg = get_config()
    token = (cfg.get("telegram_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    error = None
    if not token or not chat_id:
        error = "Token Telegram atau chat ID masih kosong"
        for message in messages:
            remember_notice(message, error, state)
    else:
        for message in messages:
            one_error = None
            try:
                send_telegram(token, chat_id, message)
            except Exception as exc:
                one_error = str(exc)
                error = one_error
            remember_notice(message, one_error, state)
    state["last_alert"] = now_wib()
    state["last_alert_error"] = error


def evaluate_plan(price):
    name = current_pair()
    try:
        plan = build_plan(price)
    except Exception as exc:
        publish_plan("Rencana belum siap", str(exc))
        return
    if not plan.get("ready"):
        publish_plan(plan.get("status") or "Rencana belum siap", plan.get("detail") or "")
        return
    with lock:
        state = default_state()
        state.update(load_json(STATE_PATH, default_state))
    if state.get("pair") not in (None, "", name):
        return
    state["pair"] = name
    phase = state.get("phase") or "menunggu"
    if phase in ("menunggu", "selesai", "batal") and state.get("plan_key") != plan["key"]:
        phase = "menunggu"
        state["plan_key"] = plan["key"]
        state["buy_sent"] = False
        state["partial_sent"] = False
        state["trail_sent"] = False
        state["invalid_sent"] = False
    if phase == "selesai" and price > plan["zone_high"]:
        phase = "menunggu"
        state["buy_sent"] = False
        state["partial_sent"] = False
        state["trail_sent"] = False
        state["invalid_sent"] = False
    messages = []
    closed_below = plan["four_hour_close"] < plan["invalidation"]
    if phase in ("posisi_beli", "sebagian_terjual") and closed_below and not state.get("invalid_sent"):
        messages.append(
            name + " — rencana batal\n"
            "Candle 4 jam ditutup di %s, di bawah batas %s.\n"
            "Jangan tambah posisi beli pada zona ini.\n"
            "Ini notifikasi, bukan order."
            % (format_price(plan["four_hour_close"]), format_price(plan["invalidation"]))
        )
        state["invalid_sent"] = True
        phase = "batal"
    if phase == "menunggu" and plan["trend"] != "turun" and not closed_below:
        if plan["zone_low"] <= price <= plan["zone_high"] and not state.get("buy_sent"):
            messages.append(
                name + " — buka posisi beli\n"
                "Harga: %s\n"
                "Zona beli: %s – %s\n"
                "Arah harian: %s\n"
                "Jual sebagian di: %s\n"
                "Batal jika candle 4 jam ditutup di bawah %s.\n"
                "Ini notifikasi, bukan order."
                % (
                    format_price(price),
                    format_price(plan["zone_low"]),
                    format_price(plan["zone_high"]),
                    plan["trend"],
                    format_price(plan["target"]) if plan["target"] else "resistensi berikutnya",
                    format_price(plan["invalidation"]),
                )
            )
            state["buy_sent"] = True
            state["entry"] = price
            state["high_since"] = price
            state["entry_target"] = plan["target"]
            state["entry_trail"] = plan["trail"]
            state["entry_invalidation"] = plan["invalidation"]
            phase = "posisi_beli"
    if phase in ("posisi_beli", "sebagian_terjual"):
        high_since = max(float(state.get("high_since") or price), price)
        state["high_since"] = high_since
        target = state.get("entry_target")
        trail = float(state.get("entry_trail") or plan["trail"])
        entry = float(state.get("entry") or price)
        if phase == "posisi_beli" and target and price >= float(target) and not state.get("partial_sent"):
            messages.append(
                name + " — jual sebagian\n"
                "Harga: %s\n"
                "Resistensi: %s\n"
                "Sisanya dijual jika harga turun %s dari puncak.\n"
                "Ini notifikasi, bukan order."
                % (format_price(price), format_price(target), format_price(trail))
            )
            state["partial_sent"] = True
            phase = "sebagian_terjual"
        if phase == "sebagian_terjual" and high_since >= entry + trail and price <= high_since - trail and not state.get("trail_sent"):
            messages.append(
                name + " — jual sisa posisi\n"
                "Harga: %s\n"
                "Puncak sejak beli: %s\n"
                "Harga sudah turun %s dari puncak.\n"
                "Ini notifikasi, bukan order."
                % (format_price(price), format_price(high_since), format_price(trail))
            )
            state["trail_sent"] = True
            phase = "selesai"
    state["phase"] = phase
    deliver_messages(messages, state)
    with lock:
        fresh = default_state()
        fresh.update(load_json(STATE_PATH, default_state))
        if fresh.get("pair") not in (None, "", name):
            return
        save_json(STATE_PATH, state)
    status_text = {
        "menunggu": "Menunggu harga masuk zona beli.",
        "posisi_beli": "Notifikasi beli sudah dikirim. Menunggu harga naik ke target jual sebagian.",
        "sebagian_terjual": "Notifikasi jual sebagian sudah dikirim. Sisa posisi mengikuti puncak.",
        "selesai": "Notifikasi jual sisa sudah dikirim.",
        "batal": "Rencana batal karena candle 4 jam ditutup di bawah support.",
    }.get(phase, phase)
    if plan["trend"] == "turun" and phase == "menunggu":
        status_text = "Beli tidak aktif. Arah harian sedang turun."
    elif closed_below and phase == "menunggu":
        status_text = "Beli tidak aktif. Candle 4 jam terakhir ditutup di bawah support."
    extra = ""
    if state.get("entry"):
        extra = "Harga saat notifikasi beli: %s. Puncak sejak itu: %s." % (
            format_price(state.get("entry")),
            format_price(state.get("high_since")),
        )
    publish_plan(status_text, plan_detail(plan, extra))


def send_telegram(token, chat_id, text):
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "Telegram menolak pesan")


def evaluate_alerts(price):
    cfg = get_config()
    name = current_pair()
    with lock:
        state = default_state()
        state.update(load_json(STATE_PATH, default_state))
        if state.get("pair") not in (None, "", name):
            return
        messages = []
        above = cfg["above"]
        below = cfg["below"]
        if above is None:
            state["above_fired"] = False
        elif price >= above and not state["above_fired"]:
            messages.append(
                name + " menyentuh batas atas\nHarga: %s\nBatas: %s" % (format_price(price), format_price(above))
            )
            state["above_fired"] = True
        elif price < above:
            state["above_fired"] = False

        if below is None:
            state["below_fired"] = False
        elif price <= below and not state["below_fired"]:
            messages.append(
                name + " menyentuh batas bawah\nHarga: %s\nBatas: %s" % (format_price(price), format_price(below))
            )
            state["below_fired"] = True
        elif price > below:
            state["below_fired"] = False
        save_json(STATE_PATH, state)
        token = (cfg.get("telegram_token") or "").strip()
        chat_id = (cfg.get("telegram_chat_id") or "").strip()

    if not messages:
        return
    error = None
    results = []
    if not token or not chat_id:
        error = "Batas tersentuh, tetapi token Telegram atau chat ID masih kosong"
        results = [(message, error) for message in messages]
    else:
        for message in messages:
            one_error = None
            try:
                send_telegram(token, chat_id, message)
            except Exception as exc:
                one_error = str(exc)
                error = one_error
            results.append((message, one_error))
    with lock:
        state = default_state()
        state.update(load_json(STATE_PATH, default_state))
        for message, one_error in results:
            remember_notice(message, one_error, state)
        state["last_alert"] = now_wib()
        state["last_alert_error"] = error
        save_json(STATE_PATH, state)


def signed_get(path, params):
    cfg = get_config()
    key = (cfg.get("tokocrypto_api_key") or "").strip()
    secret = (cfg.get("tokocrypto_api_secret") or "").strip()
    if not key or not secret:
        raise RuntimeError("API key Tokocrypto belum disimpan")
    query_params = dict(params)
    query_params["timestamp"] = int(time.time() * 1000)
    query_params["recvWindow"] = 5000
    query = urllib.parse.urlencode(query_params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = "https://www.tokocrypto.com" + path + "?" + query + "&signature=" + signature
    request = urllib.request.Request(url, headers={"X-MBX-APIKEY": key, "User-Agent": "harga-hbar/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        message = "Tokocrypto menolak permintaan"
        try:
            message = json.loads(raw).get("msg") or message
        except json.JSONDecodeError:
            pass
        raise RuntimeError(message) from None
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(payload.get("msg") or "Tokocrypto menolak permintaan")
    return payload.get("data") or {}


def enum_label(mapping, value):
    try:
        return mapping.get(int(value), "-")
    except (TypeError, ValueError):
        return "-"


def format_fee(value):
    if value is None or value == "":
        return "-"
    number = float(value)
    percent = number / 100.0 if number >= 1 else number * 100.0
    return ("%.4f" % percent).rstrip("0").rstrip(".") + "%"


def format_time(value):
    if value in (None, "", 0, "0"):
        return "-"
    try:
        stamp = int(value) / 1000.0
    except (TypeError, ValueError):
        return "-"
    return datetime.fromtimestamp(stamp, WIB).strftime("%Y-%m-%d %H:%M")


def recent_rows(rows, time_key, limit=20):
    def sort_key(row):
        try:
            return int(row.get(time_key) or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(rows or [], key=sort_key, reverse=True)[:limit]


def fetch_price_map():
    request = urllib.request.Request(
        "https://www.tokocrypto.site/api/v3/ticker/price",
        headers={"User-Agent": "harga-hbar/1.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    prices = {}
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        symbol = item.get("symbol")
        if symbol and item.get("price") is not None:
            prices[symbol] = float(item["price"])
    return prices


def usdt_value(asset, amount, pair_price, price_map, base, quote):
    if amount <= 0:
        return None
    if asset == "USDT":
        return amount
    if asset == base and pair_price:
        if quote == "USDT":
            return amount * pair_price
        if quote == "IDR":
            usdt_idr = price_map.get("USDTIDR")
            if usdt_idr:
                return amount * pair_price / usdt_idr
    price = price_map.get(asset + "USDT")
    if price:
        return amount * price
    return None


def present_order(row):
    return {
        "side": enum_label(SIDE_LABEL, row.get("side")),
        "type": enum_label(TYPE_LABEL, row.get("type")),
        "price": format_price(float(row.get("price") or 0)),
        "quantity": format_price(float(row.get("origQty") or 0)),
        "filled": format_price(float(row.get("executedQty") or 0)),
        "status": enum_label(STATUS_LABEL, row.get("status")),
        "time": format_time(row.get("createTime")),
    }


def refresh_account(pair_price):
    try:
        pair = current_pair()
        base, quote = pair.split("/")
        symbol = order_symbol(pair)
        spot = signed_get("/open/v1/account/spot", {})
        open_orders = signed_get("/open/v1/orders", {"symbol": symbol, "type": 1, "limit": 50})
        history = signed_get("/open/v1/orders", {"symbol": symbol, "type": 2, "limit": 20})
        trades = signed_get("/open/v1/orders/trades", {"symbol": symbol, "limit": 20})
        try:
            price_map = fetch_price_map()
        except Exception:
            price_map = {}
        usdt_idr = price_map.get("USDTIDR")
        if not usdt_idr:
            try:
                rate_request = urllib.request.Request(
                    "https://www.tokocrypto.site/api/v3/ticker/price?symbol=USDTIDR",
                    headers={"User-Agent": "harga-hbar/1.0"},
                )
                with urllib.request.urlopen(rate_request, timeout=15) as response:
                    usdt_idr = float(json.loads(response.read().decode("utf-8"))["price"])
            except Exception:
                usdt_idr = None
        balances = []
        idr_total = 0.0
        missing_idr = 0
        for item in spot.get("accountAssets") or []:
            free = float(item.get("free") or 0)
            locked = float(item.get("locked") or 0)
            total = free + locked
            if total <= 0:
                continue
            asset = item.get("asset") or "-"
            value = usdt_value(asset, total, pair_price, price_map, base, quote)
            if asset == "IDR":
                idr_value = total
            elif value is not None and usdt_idr:
                idr_value = value * usdt_idr
            else:
                idr_value = None
            if idr_value is not None:
                idr_total += idr_value
            else:
                missing_idr += 1
            balances.append(
                {
                    "asset": asset,
                    "free": format_price(free),
                    "locked": format_price(locked),
                    "value": format_price(value) if value is not None else "-",
                    "idr": format_idr(idr_value),
                    "_sort": (0 if asset == "USDT" else 1 if asset == base else 2, asset),
                }
            )
        balances.sort(key=lambda row: row["_sort"])
        for row in balances:
            del row["_sort"]
        trade_rows = []
        for item in recent_rows((trades.get("list") or []), "time"):
            qty = float(item.get("qty") or 0)
            quote = item.get("quoteQty")
            total = float(quote) if quote not in (None, "") else qty * float(item.get("price") or 0)
            trade_rows.append(
                {
                    "side": "Beli" if int(item.get("isBuyer") or 0) == 1 else "Jual",
                    "price": format_price(float(item.get("price") or 0)),
                    "quantity": format_price(qty),
                    "total": format_price(total),
                    "fee": format_price(float(item.get("commission") or 0)) + " " + str(item.get("commissionAsset") or ""),
                    "time": format_time(item.get("time")),
                }
            )
        view = {
            "ok": True,
            "error": None,
            "updated_at": now_wib(),
            "maker_fee": format_fee(spot.get("makerCommission")),
            "taker_fee": format_fee(spot.get("takerCommission")),
            "total_idr": format_idr(idr_total) if usdt_idr else "Kurs belum tersedia",
            "idr_note": (
                "Kurs 1 USDT = " + format_idr(usdt_idr) + " · Tokocrypto"
                + (" · " + str(missing_idr) + " aset belum masuk total" if missing_idr else "")
                if usdt_idr
                else "Harga USDT/IDR tidak ditemukan"
            ),
            "balances": balances,
            "open_orders": [present_order(row) for row in recent_rows(open_orders.get("list") or [], "createTime", 50)],
            "order_history": [present_order(row) for row in recent_rows(history.get("list") or [], "createTime")],
            "trades": trade_rows,
        }
        with lock:
            account_view.update(view)
    except Exception as exc:
        with lock:
            account_view["ok"] = False
            account_view["error"] = str(exc)


def reload_market():
    price = None
    try:
        data = fetch_ticker()
        price = data["price"]
        with lock:
            latest.update(data)
    except Exception as exc:
        with lock:
            latest["ok"] = False
            latest["error"] = str(exc)
    try:
        refresh_account(price)
    except Exception as exc:
        with lock:
            account_view["ok"] = False
            account_view["error"] = str(exc)


def poll_loop():
    while True:
        cfg = get_config()
        price = None
        try:
            data = fetch_ticker()
            price = data["price"]
            with lock:
                latest.update(data)
            evaluate_alerts(data["price"])
            evaluate_plan(data["price"])
        except Exception as exc:
            with lock:
                latest["ok"] = False
                latest["error"] = str(exc)
        try:
            refresh_account(price)
        except Exception as exc:
            with lock:
                account_view["ok"] = False
                account_view["error"] = str(exc)
        time.sleep(max(10, int(cfg.get("poll_seconds") or 20)))


def secret_hint(value):
    value = (value or "").strip()
    if len(value) >= 4:
        return "…" + value[-4:]
    return ""


def format_price(value):
    if value is None:
        return "-"
    return ("%.8f" % value).rstrip("0").rstrip(".")


def format_idr(value):
    if value is None:
        return "-"
    number = float(value)
    if abs(number) >= 100:
        grouped = f"{int(round(number)):,}".replace(",", ".")
        return "Rp " + grouped
    text = ("%.2f" % number).replace(".", ",")
    return "Rp " + text


def public_status():
    cfg = get_config()
    state = get_state()
    token = (cfg.get("telegram_token") or "").strip()
    api_key = (cfg.get("tokocrypto_api_key") or "").strip()
    api_secret = (cfg.get("tokocrypto_api_secret") or "").strip()
    with lock:
        snapshot = dict(latest)
        account = {
            "ok": account_view["ok"],
            "error": account_view["error"],
            "updated_at": account_view["updated_at"],
            "maker_fee": account_view["maker_fee"],
            "taker_fee": account_view["taker_fee"],
            "total_idr": account_view.get("total_idr") or "…",
            "idr_note": account_view.get("idr_note") or "",
            "balances": list(account_view["balances"]),
            "open_orders": list(account_view["open_orders"]),
            "order_history": list(account_view["order_history"]),
            "trades": list(account_view["trades"]),
        }
    pair = current_pair()
    base, quote = pair.split("/")
    return {
        "symbol": pair,
        "base": base,
        "quote": quote,
        "source": "Tokocrypto",
        "price": snapshot["price"],
        "price_text": format_price(snapshot["price"]),
        "change_percent": snapshot["change_percent"],
        "high_text": format_price(snapshot["high"]),
        "low_text": format_price(snapshot["low"]),
        "updated_at": snapshot["updated_at"],
        "ok": snapshot["ok"],
        "error": snapshot["error"],
        "above": cfg["above"],
        "below": cfg["below"],
        "telegram_ready": bool(token and (cfg.get("telegram_chat_id") or "").strip()),
        "telegram_hint": secret_hint(token),
        "chat_id": cfg.get("telegram_chat_id") or "",
        "tokocrypto_ready": bool(api_key and api_secret),
        "tokocrypto_key_hint": secret_hint(api_key),
        "last_alert": state.get("last_alert"),
        "last_alert_error": state.get("last_alert_error"),
        "account": account,
        "plan": {"status": plan_view["status"], "detail": plan_view["detail"]},
        "notifications": [
            {
                "time": item.get("time") or "-",
                "text": item.get("text") or "",
                "status": item.get("status") or "-",
                "error": item.get("error"),
                "pair": notice_pair(item),
            }
            for item in (state.get("notices") or [])
            if notice_pair(item) == pair
        ][:40],
    }


def ensure_secrets():
    import secrets as _secrets

    cfg = get_config()
    if cfg.get("session_secret"):
        return cfg.get("session_secret")
    secret = _secrets.token_urlsafe(32)
    with lock:
        current = default_config()
        current.update(load_json(CONFIG_PATH, default_config))
        if not current.get("session_secret"):
            current["session_secret"] = secret
            save_json(CONFIG_PATH, current)
        return current.get("session_secret")


def save_settings(incoming):
    above = parse_optional_number(incoming.get("above"))
    below = parse_optional_number(incoming.get("below"))
    if above is not None and below is not None and above <= below:
        raise ValueError("batas atas harus lebih tinggi dari batas bawah")
    old_pair = current_pair()
    raw_pair = str(incoming.get("pair") or "").strip()
    new_pair = parse_pair(raw_pair) if raw_pair else old_pair
    pair_changed = new_pair != old_pair
    if pair_changed:
        ensure_pair(new_pair)
        above = None
        below = None
    token = str(incoming.get("telegram_token") or "").strip()
    chat_id = str(incoming.get("telegram_chat_id") or "").strip()
    api_key = str(incoming.get("tokocrypto_api_key") or "").strip()
    api_secret = str(incoming.get("tokocrypto_api_secret") or "").strip()
    with lock:
        cfg = default_config()
        cfg.update(load_json(CONFIG_PATH, default_config))
        cfg["pair"] = new_pair
        cfg["above"] = above
        cfg["below"] = below
        if token:
            cfg["telegram_token"] = token
        cfg["telegram_chat_id"] = chat_id
        if api_key:
            cfg["tokocrypto_api_key"] = api_key
        if api_secret:
            cfg["tokocrypto_api_secret"] = api_secret
        save_json(CONFIG_PATH, cfg)
        state = default_state()
        state.update(load_json(STATE_PATH, default_state))
        state["above_fired"] = False
        state["below_fired"] = False
        if pair_changed:
            clear_pair_plan(state, new_pair)
            kline_cache.clear()
            latest["ok"] = False
            latest["error"] = "Memuat " + new_pair
            latest["price"] = None
            latest["change_percent"] = None
            latest["high"] = None
            latest["low"] = None
            latest["updated_at"] = None
            account_view["open_orders"] = []
            account_view["order_history"] = []
            account_view["trades"] = []
            plan_view["status"] = "Menghitung rencana posisi…"
            plan_view["detail"] = ""
        save_json(STATE_PATH, state)
    if pair_changed:
        threading.Thread(target=reload_market, daemon=True).start()
        return "Pair diganti ke " + new_pair + ". Batas harga dikosongkan."
    return "Pengaturan disimpan"


def send_test_message():
    cfg = get_config()
    token = (cfg.get("telegram_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        raise ValueError("isi token bot dan chat ID, lalu simpan")
    trial = "Percobaan dari pemantau " + current_pair() + ". Koneksi Telegram berhasil."
    send_telegram(token, chat_id, trial)
    remember_notice(trial, None)
    return "Pesan percobaan terkirim"


def start_background():
    ensure_secrets()
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, default_config())
    if not STATE_PATH.exists():
        save_json(STATE_PATH, default_state())
    thread = threading.Thread(target=poll_loop, daemon=True, name="poll")
    thread.start()
