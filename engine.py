import json
import time
import requests
from datetime import datetime, timezone, timedelta

# ================= CONFIGURATION =================
TWELVE_DATA_API_KEY = "YOUR_TWELVE_DATA_API_KEY"
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"
SETUPS_FILE = "setups.json"

# Confluence buffer in pips (how close the trendline must be to the 50%-61.8% Fib zone)
CONFLUENCE_BUFFER_PIPS = 15
# =================================================

SAST = timezone(timedelta(hours=2))

def send_telegram_message(text):
    url = f"https://telegram.org{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def is_sast_active_window(dt_sast):
    """Checks if current SAST time is inside 10:00-13:00 or 15:00-18:30."""
    hour = dt_sast.hour
    minute = dt_sast.minute
    morning_session = 10 <= hour <= 13
    ny_overlap = (15 <= hour <= 17) or (hour == 18 and minute <= 35)
    return morning_session or ny_overlap

def fetch_latest_closed_candle(pair):
    """Fetches the most recently closed 1H candle from Twelve Data."""
    url = (
        f"https://twelvedata.com"
        f"?symbol={pair}&interval=1h&outputsize=2&apikey={TWELVE_DATA_API_KEY}"
    )
    res = requests.get(url, timeout=10).json()
    if "values" not in res or len(res["values"]) < 2:
        print(f"API issue for {pair}: {res}")
        return None

    # Index 0 is the currently forming candle; Index 1 is the just-closed 1H candle
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

    # 1. Project trendline price at the closed candle's timestamp (y = mx + c)
    slope = (p2["price"] - p1["price"]) / (p2["timestamp"] - p1["timestamp"])
    trendline_price = p1["price"] + slope * (candle["timestamp"] - p1["timestamp"])

    # 2. Calculate Fibonacci levels
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

    # 3. Check Confluence: Is trendline inside or within buffer of the 50%-61.8% Golden Zone?
    has_confluence = (zone_bottom - buffer_price) <= trendline_price <= (zone_top + buffer_price)
    if not has_confluence:
        print(f"[{pair}] No confluence yet. Trendline: {trendline_price:.{decimals}f} vs Fib Zone: {zone_bottom:.{decimals}f}-{zone_top:.{decimals}f}")
        return

    # 4. Check Candle Wick Touch + Rejection Close
    if direction == "BUY":
        touched_zone = candle["low"] <= (zone_top + buffer_price) and candle["low"] >= fib_786
        rejected_up = candle["close"] > zone_bottom and candle["close"] >= candle["open"]
        valid_signal = touched_zone and rejected_up
    else:
        touched_zone = candle["high"] >= (zone_bottom - buffer_price) and candle["high"] <= fib_786
        rejected_down = candle["close"] < zone_top and candle["close"] <= candle["open"]
        valid_signal = touched_zone and rejected_down

    if valid_signal:
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
        print(f"[{pair}] SIGNAL SENT TO TELEGRAM!")
    else:
        print(f"[{pair}] Confluence active, waiting for 1H rejection candle.")

def run_scan():
    now_sast = datetime.now(SAST)
    print(f"--- Running Scan at {now_sast.strftime('%Y-%m-%d %H:%M:%S SAST')} ---")

    if not is_sast_active_window(now_sast):
        print("Outside active SAST trading windows (10:00–13:00 & 15:00–18:30). Sleeping.")
        return

    try:
        with open(SETUPS_FILE, "r") as f:
            setups = json.load(f)
    except FileNotFoundError:
        print("setups.json not found. Export it from your laptop webpage first.")
        return

    for pair, setup in setups.items():
        if not setup.get("active"):
            continue
        candle = fetch_latest_closed_candle(pair)
        if candle:
            evaluate_setup(pair, setup, candle)

if __name__ == "__main__":
    # Runs continuously and triggers 15 seconds after every hour close
    while True:
        now = datetime.now(SAST)
        if now.minute == 0 and now.second >= 15:
            run_scan()
            time.sleep(65)  # Prevent double-triggering in the same minute
        time.sleep(10)
