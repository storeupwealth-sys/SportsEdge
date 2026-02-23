# runner.py
import os
import time
import json
import random
import threading
from typing import Dict, List, Optional, Tuple

import requests

try:
    import websocket  # websocket-client
except Exception:
    websocket = None

# =========================
# ENV / CONFIG
# =========================

BOT_NAME = os.getenv("BOT_NAME", "God AI Predict Bot")

# Notifications
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Polymarket sports websocket
POLY_SPORTS_WS = os.getenv("POLY_SPORTS_WS", "wss://sports-api.polymarket.com/ws")

# Kalshi
# NOTE: Kalshi rate limits exist. This runner does:
# - DISCOVERY scan rarely
# - WATCH polling frequently
KALSHI_BASE = os.getenv("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_DISCOVERY_SEC = int(os.getenv("KALSHI_DISCOVERY_SEC", "600"))  # every 10 minutes
KALSHI_WATCH_SEC = int(os.getenv("KALSHI_WATCH_SEC", "20"))           # poll watched markets
KALSHI_MAX_PAGES_PER_DISCOVERY = int(os.getenv("KALSHI_MAX_PAGES_PER_DISCOVERY", "2"))
KALSHI_PAGE_LIMIT = int(os.getenv("KALSHI_PAGE_LIMIT", "200"))

# Auto-discovery keywords (comma-separated)
# Example: "trump, mention, biden, election, senate, house"
KALSHI_KEYWORDS = [k.strip().lower() for k in os.getenv(
    "KALSHI_KEYWORDS",
    "trump, mention, election, biden, gop, democrat, senate, house, fed, tariff, china, ukraine"
).split(",") if k.strip()]

# Alert rules
MIN_SNAPS = int(os.getenv("MIN_SNAPS", "3"))  # snapshots needed before alerting
MIN_MOVE_CENTS = int(os.getenv("MIN_MOVE_CENTS", "1"))  # change in best bid/ask midpoint in cents
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "600"))

# How many markets to keep in watchlist max (safety)
KALSHI_WATCH_MAX = int(os.getenv("KALSHI_WATCH_MAX", "30"))

# =========================
# STATE
# =========================

session = requests.Session()
session.headers.update({"User-Agent": f"{BOT_NAME}/1.0"})

_last_alert_time: Dict[str, int] = {}
_price_snaps: Dict[str, List[float]] = {}  # ticker -> recent mid prices
_watchlist: Dict[str, Dict] = {}  # ticker -> market info (title, etc.)

REC_FILE = "recap.jsonl"


# =========================
# NOTIFICATION
# =========================

def send_discord(text: str) -> None:
    if not DISCORD_WEBHOOK:
        return
    try:
        session.post(DISCORD_WEBHOOK, json={"content": text}, timeout=10)
    except Exception as e:
        print(f"Discord send error: {e}")

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = session.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            # Common: 403 bots can't send to bots
            print(f"Telegram error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Telegram send exception: {e}")

def notify(text: str) -> None:
    # single fan-out
    send_discord(text)
    send_telegram(text)

def can_alert(key: str) -> bool:
    now = int(time.time())
    last = _last_alert_time.get(key, 0)
    if now - last < ALERT_COOLDOWN_SEC:
        return False
    _last_alert_time[key] = now
    return True

def cents(x: float) -> int:
    return int(round(x * 100))

def fmt_price(p: float) -> str:
    return f"{cents(p)}¢"


# =========================
# KALSHI HELPERS
# =========================

def kalshi_get(path: str, params: Optional[dict] = None, max_retries: int = 5) -> Optional[dict]:
    """
    Kalshi can 429. We do exponential backoff + jitter.
    """
    url = f"{KALSHI_BASE}{path}"
    delay = 1.0
    for attempt in range(max_retries):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 429:
                # backoff
                sleep_for = delay + random.random()
                print(f"Kalshi 429 rate limit. Backing off {sleep_for:.2f}s")
                time.sleep(sleep_for)
                delay = min(delay * 2, 30)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"Kalshi request error: {repr(e)} url={url}")
            time.sleep(delay + random.random())
            delay = min(delay * 2, 30)
    return None

def kalshi_list_open_markets(cursor: Optional[str] = None, limit: int = 200) -> Tuple[List[dict], Optional[str]]:
    """
    General open markets listing. This is the broad discovery endpoint.
    It may return many categories; we keyword-filter client-side.
    """
    params = {"status": "open", "limit": limit}
    if cursor:
        params["cursor"] = cursor
    data = kalshi_get("/markets", params=params)
    if not data:
        return [], None
    markets = data.get("markets", []) or []
    next_cursor = data.get("cursor")
    return markets, next_cursor

def kalshi_market_snapshot(ticker: str) -> Optional[dict]:
    """
    Fetch market details including orderbook summary if available.
    Some endpoints vary by market type; this is a generic call.
    """
    data = kalshi_get(f"/markets/{ticker}")
    return data

def market_matches_keywords(m: dict) -> bool:
    title = (m.get("title") or "").lower()
    subtitle = (m.get("subtitle") or "").lower()
    yes_name = (m.get("yes_name") or "").lower()
    no_name = (m.get("no_name") or "").lower()
    blob = " ".join([title, subtitle, yes_name, no_name])
    return any(k in blob for k in KALSHI_KEYWORDS)

def extract_mid_price(market_details: dict) -> Optional[float]:
    """
    Try to compute a midpoint from best bid/ask.
    Kalshi payloads differ; we try common fields safely.
    """
    m = market_details.get("market") if isinstance(market_details, dict) else None
    if not m:
        return None

    # Try common fields (these may not exist on all contracts)
    best_bid = m.get("best_bid")
    best_ask = m.get("best_ask")

    # Sometimes nested
    if best_bid is None and "yes_bid" in m:
        best_bid = m.get("yes_bid")
    if best_ask is None and "yes_ask" in m:
        best_ask = m.get("yes_ask")

    if best_bid is None or best_ask is None:
        return None

    try:
        best_bid = float(best_bid)
        best_ask = float(best_ask)
        if best_bid <= 0 or best_ask <= 0:
            return None
        return (best_bid + best_ask) / 2.0
    except Exception:
        return None


# =========================
# KALSHI ENGINE
# =========================

def discovery_loop() -> None:
    """
    Slow loop: discovers new interesting markets automatically and updates watchlist.
    """
    global _watchlist

    while True:
        print("KALSHI discovery scan start")
        cursor = None
        pages = 0
        found = 0

        while pages < KALSHI_MAX_PAGES_PER_DISCOVERY:
            markets, cursor = kalshi_list_open_markets(cursor=cursor, limit=KALSHI_PAGE_LIMIT)
            if not markets:
                break

            pages += 1
            for m in markets:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                if ticker in _watchlist:
                    continue
                if market_matches_keywords(m):
                    # add to watch
                    _watchlist[ticker] = {
                        "ticker": ticker,
                        "title": m.get("title") or "",
                        "subtitle": m.get("subtitle") or "",
                        "added_at": int(time.time()),
                    }
                    found += 1

                    # cap watchlist size
                    if len(_watchlist) > KALSHI_WATCH_MAX:
                        # remove oldest
                        oldest = sorted(_watchlist.items(), key=lambda kv: kv[1].get("added_at", 0))[0][0]
                        _watchlist.pop(oldest, None)

            # polite spacing between pages
            time.sleep(2.0)

            if not cursor:
                break

        print(f"KALSHI discovery scan done. pages={pages} new_watch={found} watch_total={len(_watchlist)}")
        time.sleep(KALSHI_DISCOVERY_SEC)

def watch_loop() -> None:
    """
    Fast loop: polls only watched markets for movement and alerts.
    """
    while True:
        if not _watchlist:
            # No watchlist yet, wait a bit
            time.sleep(5)
            continue

        for ticker, info in list(_watchlist.items()):
            details = kalshi_market_snapshot(ticker)
            if not details:
                continue

            mid = extract_mid_price(details)
            if mid is None:
                continue

            snaps = _price_snaps.setdefault(ticker, [])
            snaps.append(mid)
            if len(snaps) > 25:
                snaps.pop(0)

            # Need enough snaps before signals
            if len(snaps) < MIN_SNAPS:
                continue

            prev = snaps[-2]
            move_cents = abs(cents(mid) - cents(prev))

            if move_cents >= MIN_MOVE_CENTS and can_alert(f"kalshi:{ticker}"):
                title = info.get("title") or ticker
                msg = (
                    f"📣 {BOT_NAME} | KALSHI MOVE\n"
                    f"🧾 {title}\n"
                    f"🏷️ {ticker}\n"
                    f"📈 {fmt_price(prev)} → {fmt_price(mid)} ({move_cents}¢)\n"
                    f"⏱️ watch={KALSHI_WATCH_SEC}s | snaps={len(snaps)}"
                )
                notify(msg)
                append_recap({
                    "ts": int(time.time()),
                    "source": "kalshi",
                    "ticker": ticker,
                    "title": title,
                    "prev_mid": prev,
                    "mid": mid,
                    "move_cents": move_cents
                })

            # tiny pause between tickers to be polite
            time.sleep(0.25)

        time.sleep(KALSHI_WATCH_SEC)


# =========================
# POLYMARKET SPORTS WS (optional)
# =========================

def polymarket_ws_loop() -> None:
    """
    Keeps WS connected. Right now, we just print sport_result messages.
    You can expand this to map sports events to Polymarket markets later.
    """
    if websocket is None:
        print("websocket-client not installed; skipping Polymarket Sports WS")
        return

    def on_message(ws, message):
        if message == "ping":
            try:
                ws.send("pong")
            except Exception:
                pass
            return
        try:
            data = json.loads(message)
        except Exception:
            return

        # Minimal handling
        if isinstance(data, dict) and data.get("type") == "sport_result":
            # Example fields from docs:
            # gameId, leagueAbbreviation, slug, homeTeam, awayTeam, status, score, period, elapsed, live, ended
            league = data.get("leagueAbbreviation")
            slug = data.get("slug")
            score = data.get("score")
            period = data.get("period")
            status = data.get("status")
            live = data.get("live")
            ended = data.get("ended")
            print(f"POLY WS: {league} {slug} {score} {period} {status} live={live} ended={ended}")

    def on_error(ws, error):
        print(f"Polymarket WS error: {error}")

    def on_close(ws, close_status_code, close_msg):
        print(f"Polymarket WS closed: {close_status_code} {close_msg}")

    def on_open(ws):
        print("Connected to Polymarket Sports WS")

    while True:
        try:
            ws = websocket.WebSocketApp(
                POLY_SPORTS_WS,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=0)  # we respond manually to server ping strings
        except Exception as e:
            print(f"WS loop exception: {e}")
        time.sleep(3)


# =========================
# RECAP
# =========================

def append_recap(obj: dict) -> None:
    try:
        with open(REC_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception as e:
        print(f"recap write error: {e}")


# =========================
# MAIN
# =========================

def sanity_print():
    print(f"Starting {BOT_NAME}")
    print(f"Discord: {'on' if DISCORD_WEBHOOK else 'off'}")
    print(f"Telegram: {'on' if (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) else 'off'}")
    print(f"Kalshi base: {KALSHI_BASE}")
    print(f"Kalshi discovery: {KALSHI_DISCOVERY_SEC}s, watch: {KALSHI_WATCH_SEC}s")
    print(f"Keywords: {KALSHI_KEYWORDS[:12]}{'...' if len(KALSHI_KEYWORDS) > 12 else ''}")

def main():
    sanity_print()

    # Start Kalshi loops
    t1 = threading.Thread(target=discovery_loop, daemon=True)
    t2 = threading.Thread(target=watch_loop, daemon=True)
    t1.start()
    t2.start()

    # Start Polymarket WS loop (optional)
    t3 = threading.Thread(target=polymarket_ws_loop, daemon=True)
    t3.start()

    # keep process alive
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()