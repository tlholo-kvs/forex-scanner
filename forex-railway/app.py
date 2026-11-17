import os
import json
import time
import threading
import requests
from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# Keys loaded securely from Railway Environment Variables
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")

SETUPS_FILE = "setups.json"
CONFLUENCE_BUFFER_PIPS = 15
SAST = timezone(timedelta(hours=2))

def load_setups():
    if not os.path.exists(SETUPS_FILE):
        return {}
    with open(SETUPS_FILE, "r") as f:
        return json.load(f)

def save_setups(data):
    with open(SETUPS_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ================= WEB ROUTES =================

@app.route("/")
def serve_index():
    return send_from_directory(".", "index.html")

@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "apiKey": TWELVE_DATA_API_KEY,
        "setups": load_setups()
    })

@app.route("/api/setups", methods=["POST"])
def update_setups():
    auth = request.headers.get("X-Dashboard-Password", "")
    if auth != DASHBOARD_PASSWORD:
        return jsonify({"error": "Invalid password"}), 401
    data = request.json
    save_setups(data)
    return jsonify({"status": "saved", "setups": data})

# ================= SIGNAL ENGINE =================

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def is_sast_active_window(dt_sast):
    hour = dt_sast.hour
    minute = dt_sast.minute
    morning_session = 10 <= hour <= 13
    ny_overlap = (15 <= hour <= 17) or (hour == 18 and minute <= 35)
    return morning_session or ny_overlap

def fetch_latest_closed_candle(pair):
    url = (
        f"https://twelvedata.com"
        f"?symbol={pair}&interval=1h&outputsize=2&apikey={TWELVE_DATA_API_KEY}"
    )
    res = requests.get(url, timeout=10).json()
    if "values" not in res or len(res["values"]) < 2:
        return None

    closed = res["values"][1]
    dt_utc = datetime.strptime(closed["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return {
        "timestamp": int(dt_utc.timestamp()),
        "datetime_sast": dt_utc.astimezone(SAST).strftime("%Y-%m-%d %H:%M SAST"),
        "open": float(closed["open"]),
        "high": float(closed["high"]),
        "low": float(closed["low"]),
        "close": float(closed["close"]),
    }

def evaluate_setup(pair, setup, candle):
    direction = setup["direction"]
    fib_high = setup["fib_high"]
    fib_low = setup["fib_low"]
    p1 = setup["trendline_p1"]
    p2 = setup["trendline_p2"]

    pip_size = 0.01 if "JPY" in pair else 0.0001
    decimals = 3 if "JPY" in pair else 5
    buffer_price = CONFLUENCE_BUFFER_PIPS * pip_size

    slope = (p2["price"] - p1["price"]) / (p2["timestamp"] - p1["timestamp"])
    trendline_price = p1["price"] + slope * (candle["timestamp"] - p1["timestamp"])

    diff = fib_high - fib_low
    if direction == "BUY":
        fib_50 = fib_high - (diff * 0.50)
        fib_618 = fib_high - (diff * 0.618)
        fib_786 = fib_high - (diff * 0.786)
        target_price = fib_high
    else:
        fib_50 = fib_low + (diff * 0.50)
        fib_618 = fib_low + (diff * 0.618)
        fib_786 = fib_low + (diff * 0.786)
        target_price = fib_low

    zone_top = max(fib_50, fib_618)
    zone_bottom = min(fib_50, fib_618)

    has_confluence = (zone_bottom - buffer_price) <= trendline_price <= (zone_top + buffer_price)
    if not has_confluence:
        return

    if direction == "BUY":
        touched = (zone_top + buffer_price) >= candle["low"] >= fib_786
        rejected = candle["close"] > zone_bottom and candle["close"] >= candle["open"]
        valid = touched and rejected
    else:
        touched = (zone_bottom - buffer_price) <= candle["high"] <= fib_786
        rejected = candle["close"] < zone_top and candle["close"] <= candle["open"]
        valid = touched and rejected

    if valid:
        sl_pips = abs(candle["close"] - fib_786) / pip_size
        tp_pips = abs(target_price - candle["close"]) / pip_size
        rr_ratio = round(tp_pips / sl_pips, 1) if sl_pips > 0 else 0

        msg = (
            f"🚨 *1H CONFLUENCE SIGNAL: {pair} ({direction})*\n"
            f"🕒 *Candle Close:* {candle['datetime_sast']}\n"
            f"📐 *Setup:* 50%–61.8% Fib + Trendline Rejection\n\n"
            f"🎯 *Entry:* `{candle['close']:.{decimals}f}`\n"
            f"🛑 *Stop-Loss (78.6% Fib):* `{fib_786:.{decimals}f}` ({sl_pips:.1f} pips)\n"
            f"💰 *Take-Profit (0% Swing):* `{target_price:.{decimals}f}` ({tp_pips:.1f} pips | 1:{rr_ratio} R:R)"
        )
        send_telegram_message(msg)

def background_scanner():
    print("Background SAST Scanner Started...")
    while True:
        now_sast = datetime.now(SAST)
        if now_sast.minute == 0 and now_sast.second >= 15:
            if is_sast_active_window(now_sast):
                setups = load_setups()
                for pair, setup in setups.items():
                    if setup.get("active"):
                        candle = fetch_latest_closed_candle(pair)
                        if candle:
                            evaluate_setup(pair, setup, candle)
            time.sleep(65)
        time.sleep(10)

if __name__ == "__main__":
    scanner_thread = threading.Thread(target=background_scanner, daemon=True)
    scanner_thread.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
