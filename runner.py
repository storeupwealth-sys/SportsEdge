# runner.py
# Polymarket Sports Edge Bot (CBB + NBA) - single file, self contained
# Features:
# - Watches CBB and NBA (live + upcoming)
# - Pulls slugs from Polymarket games pages + Sports WebSocket
# - Pulls prices from Gamma API (bestAsk/bestBid/outcomePrices)
# - Sends Discord + optional Telegram alerts
# - Separate tuning per league: MIN_SNAPS, MIN_MOVE, cooldowns
# - Exit management: TP1, TP2, SL, trailing, time stop
# - Persists state to state.json so it survives restarts

import json
import os
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from websocket import WebSocketApp

# =========================================================
# PUT YOUR KEYS HERE (or use Railway Variables)
# =========================================================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()  # recommended to set in Railway
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BOT_NAME = os.getenv("BOT_NAME", "God AI Predict Bot").strip()

# =========================================================
# League pages
# =========================================================
CBB_PAGE = "https://polymarket.com/sports/cbb/games"
NBA_PAGE = "https://polymarket.com/sports/nba/games"

# Polymarket APIs
SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# =========================================================
# HARD DEFAULTS (you can override via env vars if you want)
# =========================================================
SCAN_SEC = int(os.getenv("SCAN_SEC", "60"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_SLUGS_PER_LEAGUE = int(os.getenv("MAX_SLUGS_PER_LEAGUE", "60"))

# Separate tuning per league
LEAGUE_CFG = {
    "CBB": {
        "slug_prefix": "cbb-",
        "min_snaps": int(os.getenv("CBB_MIN_SNAPS", "12")),
        "min_move": float(os.getenv("CBB_MIN_MOVE", "0.05")),  # 5c move
        "live_cooldown": int(os.getenv("CBB_LIVE_COOLDOWN_SEC", "600")),
        "pregame_cooldown": int(os.getenv("CBB_PREGAME_COOLDOWN_SEC", "900")),
        "min_liquidity": float(os.getenv("CBB_MIN_LIQUIDITY", "2500")),  # optional filter
        "max_spread_cents": float(os.getenv("CBB_MAX_SPREAD_CENTS", "4.0")),  # optional filter
    },
    "NBA": {
        "slug_prefix": "nba-",
        "min_snaps": int(os.getenv("NBA_MIN_SNAPS", "10")),
        "min_move": float(os.getenv("NBA_MIN_MOVE", "0.04")),  # 4c move
        "live_cooldown": int(os.getenv("NBA_LIVE_COOLDOWN_SEC", "480")),
        "pregame_cooldown": int(os.getenv("NBA_PREGAME_COOLDOWN_SEC", "900")),
        "min_liquidity": float(os.getenv("NBA_MIN_LIQUIDITY", "4000")),
        "max_spread_cents": float(os.getenv("NBA_MAX_SPREAD_CENTS", "4.0")),
    },
}

# Exit management (cent based)
TP1_CENTS = float(os.getenv("TP1_CENTS", "3"))
TP2_CENTS = float(os.getenv("TP2_CENTS", "6"))
SL_CENTS = float(os.getenv("SL_CENTS", "2"))
TRAIL_START_CENTS = float(os.getenv("TRAIL_START_CENTS", "4"))
TRAIL_GAP_CENTS = float(os.getenv("TRAIL_GAP_CENTS", "2"))
TIME_STOP_MIN = float(os.getenv("TIME_STOP_MIN", "20"))

# Guardrails
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.95"))

STATE_PATH = os.getenv("STATE_PATH", "state.json")

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; PolymarketEdgeBot/1.0; +https://polymarket.com)",
)
HEADERS = {"User-Agent": USER_AGENT}

# =========================================================
# State
# =========================================================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {
            "history": {},        # key -> [ {"t":ts,"p":price} ]
            "cooldowns": {},      # key -> last_ts
            "positions": {},      # key -> { "entry":p, "opened":ts, "peak":p }
            "last_slugs": {"CBB": [], "NBA": []},
        }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError("bad state")
        s.setdefault("history", {})
        s.setdefault("cooldowns", {})
        s.setdefault("positions", {})
        s.setdefault("last_slugs", {"CBB": [], "NBA": []})
        s["last_slugs"].setdefault("CBB", [])
        s["last_slugs"].setdefault("NBA", [])
        return s
    except Exception:
        return {
            "history": {},
            "cooldowns": {},
            "positions": {},
            "last_slugs": {"CBB": [], "NBA": []},
        }

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)

STATE = load_state()

# =========================================================
# Notifier
# =========================================================
def send_discord(text: str) -> None:
    if not DISCORD_WEBHOOK:
        print("DISCORD_WEBHOOK not set. Message would be:\n", text)
        return
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": text}, timeout=HTTP_TIMEOUT)
        if r.status_code >= 300:
            print("Discord error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Discord send failed:", e)

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
        r = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
        if r.status_code >= 300:
            print("Telegram error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram send failed:", e)

def notify(text: str) -> None:
    send_discord(text)
    send_telegram(text)

# =========================================================
# Sports WS (live scores and status)
# =========================================================
WS_STATE: Dict[str, Dict[str, Any]] = {}  # slug -> game state
WS_LAST_TS: Optional[int] = None

def ws_on_message(ws, message: str):
    global WS_LAST_TS
    if message == "ping":
        try:
            ws.send("pong")
        except Exception:
            pass
        return

    try:
        obj = json.loads(message)
    except Exception:
        return

    league = str(obj.get("leagueAbbreviation", "")).lower()
    slug = obj.get("slug")
    if not isinstance(slug, str):
        return

    # Only keep CBB/NBA
    if league not in ("cbb", "nba"):
        return

    WS_STATE[slug] = obj
    WS_LAST_TS = int(time.time())

def ws_loop():
    def on_open(ws):
        print("Connected to Polymarket Sports WS")

    def on_error(ws, err):
        print("WS error:", err)

    def on_close(ws, code, reason):
        print("WS closed:", code, reason)

    while True:
        try:
            app = WebSocketApp(
                SPORTS_WS_URL,
                on_open=on_open,
                on_message=ws_on_message,
                on_error=on_error,
                on_close=on_close,
            )
            app.run_forever(ping_interval=None)
        except Exception as e:
            print("WS reconnecting after error:", e)
        time.sleep(3)

# Start WS in background
threading.Thread(target=ws_loop, daemon=True).start()

# =========================================================
# Slug discovery
# =========================================================
MARKET_PARAM_RE = re.compile(r"market=([a-z0-9\-]+)", re.IGNORECASE)

def scrape_slugs(page_url: str, slug_prefix: str) -> List[str]:
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print("Scrape error:", page_url, e)
        return []

    slugs = []
    for m in MARKET_PARAM_RE.finditer(html):
        s = m.group(1).strip()
        if s.lower().startswith(slug_prefix):
            slugs.append(s)

    # Fallback: scan raw tokens
    if not slugs:
        for m in re.finditer(rf"\b{re.escape(slug_prefix)}[a-z0-9\-]{{10,}}\b", html, re.IGNORECASE):
            slugs.append(m.group(0))

    # Dedup preserve order
    seen = set()
    out = []
    for s in slugs:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out[:MAX_SLUGS_PER_LEAGUE]

def merge_ws_slugs(league: str, slug_prefix: str, base: List[str]) -> List[str]:
    # Pull slugs from WS_STATE that match prefix
    ws_slugs = [s for s in WS_STATE.keys() if s.lower().startswith(slug_prefix)]
    merged = []
    seen = set()
    for s in base + ws_slugs:
        if s in seen:
            continue
        seen.add(s)
        merged.append(s)
    return merged[:MAX_SLUGS_PER_LEAGUE]

# =========================================================
# Gamma market fetch
# =========================================================
def _parse_maybe_json_list(v: Any) -> Any:
    if isinstance(v, str) and v.startswith("["):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v

def fetch_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(GAMMA_MARKETS_URL, params={"slug": slug}, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None
    except Exception as e:
        print("Gamma fetch error:", slug, e)
        return None

def extract_prices(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    # outcomes and bid/ask arrays can be strings
    outcomes = _parse_maybe_json_list(market.get("outcomes")) or []
    best_ask = _parse_maybe_json_list(market.get("bestAsk"))
    best_bid = _parse_maybe_json_list(market.get("bestBid"))
    outcome_prices = _parse_maybe_json_list(market.get("outcomePrices"))
    liq = float(market.get("liquidity") or 0.0)
    vol = float(market.get("volume") or market.get("volume24hr") or 0.0)
    liquidity = liq if liq > 0 else vol

    packs: List[Dict[str, Any]] = []
    if isinstance(outcomes, list) and len(outcomes) >= 2:
        for i, name in enumerate(outcomes):
            team = str(name)
            low = team.strip().lower()
            if low in ("yes", "no"):
                continue

            ask = None
            bid = None
            mid = None

            if isinstance(best_ask, list) and i < len(best_ask):
                try:
                    ask = float(best_ask[i])
                except Exception:
                    ask = None
            if isinstance(best_bid, list) and i < len(best_bid):
                try:
                    bid = float(best_bid[i])
                except Exception:
                    bid = None

            if ask is None and bid is None and isinstance(outcome_prices, list) and i < len(outcome_prices):
                try:
                    mid = float(outcome_prices[i])
                except Exception:
                    mid = None

            if mid is None:
                if ask is not None and bid is not None:
                    mid = (ask + bid) / 2.0
                else:
                    mid = ask if ask is not None else bid

            if mid is None:
                continue

            spread_c = None
            if ask is not None and bid is not None:
                spread_c = (ask - bid) * 100.0

            packs.append(
                {
                    "team": team,
                    "ask": ask,
                    "bid": bid,
                    "mid": mid,
                    "spread_c": spread_c,
                    "liquidity": liquidity,
                }
            )
    return packs

# =========================================================
# Signal + trade management
# =========================================================
def hist_key(league: str, slug: str, team: str) -> str:
    return f"{league}|{slug}|{team}".lower()

def push_history(key: str, price: float) -> None:
    h = STATE["history"].setdefault(key, [])
    h.append({"t": int(time.time()), "p": float(price)})
    if len(h) > 200:
        del h[:-200]

def get_prev_price(key: str) -> Optional[float]:
    h = STATE["history"].get(key) or []
    if len(h) < 2:
        return None
    return float(h[-2]["p"])

def snaps_ready(league: str, key: str) -> bool:
    h = STATE["history"].get(key) or []
    need = LEAGUE_CFG[league]["min_snaps"]
    return len(h) >= need

def move_over_snaps(league: str, key: str) -> Optional[float]:
    h = STATE["history"].get(key) or []
    need = LEAGUE_CFG[league]["min_snaps"]
    if len(h) < need:
        return None
    a = float(h[-need]["p"])
    b = float(h[-1]["p"])
    return b - a

def can_send(league: str, key: str, is_live: bool) -> bool:
    now = int(time.time())
    last = int(STATE["cooldowns"].get(key, 0))
    cd = LEAGUE_CFG[league]["live_cooldown"] if is_live else LEAGUE_CFG[league]["pregame_cooldown"]
    if now - last < cd:
        return False
    STATE["cooldowns"][key] = now
    return True

def confidence(move: float, liquidity: float, league: str) -> float:
    base = 5.0
    # bigger move = higher
    base += min(3.0, abs(move) * 50.0)  # 0.05 => +2.5
    # higher liquidity = higher
    if liquidity >= 10000:
        base += 1.5
    elif liquidity >= 5000:
        base += 1.0
    elif liquidity >= 2500:
        base += 0.5
    # slight bump NBA
    if league == "NBA":
        base += 0.5
    return max(1.0, min(10.0, base))

def should_alert(league: str, price_now: float, price_prev: float) -> bool:
    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return False
    mv = abs(price_now - price_prev)
    return mv >= LEAGUE_CFG[league]["min_move"]

def pos_get(key: str) -> Optional[Dict[str, Any]]:
    return STATE["positions"].get(key)

def pos_open(key: str, entry: float) -> None:
    STATE["positions"][key] = {
        "entry": float(entry),
        "opened": int(time.time()),
        "peak": float(entry),
    }

def pos_update_peak(key: str, price_now: float) -> None:
    p = pos_get(key)
    if not p:
        return
    if price_now > float(p.get("peak", price_now)):
        p["peak"] = float(price_now)

def pos_close(key: str) -> None:
    if key in STATE["positions"]:
        del STATE["positions"][key]

def exit_signal(key: str, price_now: float) -> Optional[Tuple[str, str]]:
    """
    Returns (action, reason) or None
    action in: TP1, TP2, SL, TRAIL, TIME
    """
    p = pos_get(key)
    if not p:
        return None

    entry = float(p["entry"])
    peak = float(p.get("peak", entry))
    opened = int(p.get("opened", int(time.time())))

    pnl_c = (price_now - entry) * 100.0

    # TP2
    if pnl_c >= TP2_CENTS:
        return ("TP2", f"TP2 hit (+{pnl_c:.1f}c)")

    # TP1
    if pnl_c >= TP1_CENTS:
        return ("TP1", f"TP1 hit (+{pnl_c:.1f}c)")

    # SL
    if pnl_c <= -SL_CENTS:
        return ("SL", f"Stop hit ({pnl_c:.1f}c)")

    # Trailing (activate after trail start)
    gain_from_entry_c = (peak - entry) * 100.0
    if gain_from_entry_c >= TRAIL_START_CENTS:
        trail_floor = peak - (TRAIL_GAP_CENTS / 100.0)
        if price_now <= trail_floor:
            return ("TRAIL", "Trailing stop hit")

    # Time stop
    age_min = (int(time.time()) - opened) / 60.0
    if age_min >= TIME_STOP_MIN and pnl_c < (TP1_CENTS * 0.5):
        return ("TIME", f"Time stop ({age_min:.0f}m)")

    return None

# =========================================================
# Message formatting
# =========================================================
def market_link(slug: str) -> str:
    return f"https://polymarket.com/market/{slug}"

def fmt_price(p: float) -> str:
    return f"{p:.2f}"

def entry_msg(league: str, slug: str, team: str, price: float, mv: float, liq: float, ws: Optional[Dict[str, Any]]) -> str:
    is_live = bool(ws and ws.get("live"))
    status = "LIVE" if is_live else "UPCOMING"
    score = (ws.get("score") if ws else "") or ""
    period = (ws.get("period") if ws else "") or ""
    elapsed = (ws.get("elapsed") if ws else "") or ""
    ctx = ""
    if score or period:
        ctx = f"\n⏱️ {period} {elapsed} | 🧾 {score}".strip()

    conf = confidence(mv, liq, league)
    move_c = mv * 100.0
    move_emoji = "📈" if mv > 0 else "📉"

    return (
        f"@everyone 🚨 {BOT_NAME} {league} {status} BET\n"
        f"🏀 {team}  💰 {fmt_price(price)}\n"
        f"{move_emoji} Move: {move_c:+.1f}c | 💧Liq: {int(liq):,} | 🧠 Conf: {conf:.1f}/10"
        f"{ctx}\n"
        f"🔗 {market_link(slug)}"
    )

def update_msg(league: str, slug: str, team: str, entry: float, nowp: float, action: str, reason: str) -> str:
    pnl_c = (nowp - entry) * 100.0
    emoji = "✅" if pnl_c >= 0 else "⚠️"
    return (
        f"@everyone {emoji} {BOT_NAME} {league} UPDATE\n"
        f"🏀 {team}\n"
        f"Entry: {entry*100:.0f}c | Now: {nowp*100:.0f}c | PnL: {pnl_c:+.1f}c\n"
        f"Action: {action} | {reason}\n"
        f"🔗 {market_link(slug)}"
    )

# =========================================================
# Main loop
# =========================================================
def main():
    notify(f"🟣 {BOT_NAME} is ONLINE ✅ | Watching CBB + NBA")

    last_heartbeat = 0

    while True:
        try:
            # Discover slugs for each league
            cbb_slugs = scrape_slugs(CBB_PAGE, LEAGUE_CFG["CBB"]["slug_prefix"])
            nba_slugs = scrape_slugs(NBA_PAGE, LEAGUE_CFG["NBA"]["slug_prefix"])

            # fallback to last if scrape fails
            if cbb_slugs:
                STATE["last_slugs"]["CBB"] = cbb_slugs
            else:
                cbb_slugs = STATE["last_slugs"]["CBB"]

            if nba_slugs:
                STATE["last_slugs"]["NBA"] = nba_slugs
            else:
                nba_slugs = STATE["last_slugs"]["NBA"]

            # merge WS discovered slugs too
            cbb_slugs = merge_ws_slugs("CBB", LEAGUE_CFG["CBB"]["slug_prefix"], cbb_slugs)
            nba_slugs = merge_ws_slugs("NBA", LEAGUE_CFG["NBA"]["slug_prefix"], nba_slugs)

            # heartbeat every 30 min
            now = time.time()
            if now - last_heartbeat >= 1800:
                last_heartbeat = now
                notify(f"🟣 {BOT_NAME} heartbeat ✅ | CBB slugs={len(cbb_slugs)} | NBA slugs={len(nba_slugs)} | WS={WS_LAST_TS}")

            totals = {"CBB": 0, "NBA": 0}
            alerts = 0
            updates = 0

            for league, slugs in (("CBB", cbb_slugs), ("NBA", nba_slugs)):
                for slug in slugs:
                    ws = WS_STATE.get(slug)
                    if ws and bool(ws.get("ended")):
                        continue

                    market = fetch_market_by_slug(slug)
                    if not market:
                        continue

                    packs = extract_prices(market)
                    if not packs:
                        continue

                    for p in packs:
                        team = p["team"]
                        mid = float(p["mid"])
                        liq = float(p["liquidity"] or 0.0)
                        spread_c = p.get("spread_c")

                        # Optional filters: liquidity and spread
                        if liq < float(LEAGUE_CFG[league]["min_liquidity"]):
                            continue
                        if spread_c is not None and float(spread_c) > float(LEAGUE_CFG[league]["max_spread_cents"]):
                            continue

                        totals[league] += 1

                        key = hist_key(league, slug, team)

                        push_history(key, mid)
                        prev = get_prev_price(key)
                        if prev is None:
                            continue

                        # Warmup check
                        if not snaps_ready(league, key):
                            continue

                        # Move over snaps window
                        mv = move_over_snaps(league, key)
                        if mv is None:
                            continue

                        is_live = bool(ws and ws.get("live"))

                        # Entry alert
                        if should_alert(league, mid, prev) and can_send(league, key, is_live):
                            notify(entry_msg(league, slug, team, mid, mv, liq, ws))
                            alerts += 1
                            pos_open(key, mid)

                        # Update peak and manage exits
                        if pos_get(key):
                            pos_update_peak(key, mid)
                            sig = exit_signal(key, mid)
                            if sig:
                                action, reason = sig
                                entry = float(STATE["positions"][key]["entry"])
                                notify(update_msg(league, slug, team, entry, mid, action, reason))
                                updates += 1
                                # Close on TP2 / SL / TRAIL / TIME. Keep open on TP1 so it can keep running.
                                if action in ("TP2", "SL", "TRAIL", "TIME"):
                                    pos_close(key)

            save_state(STATE)
            print(
                f"SCAN ok | CBB outcomes={totals['CBB']} NBA outcomes={totals['NBA']} alerts={alerts} updates={updates} | WS={WS_LAST_TS}"
            )
            time.sleep(SCAN_SEC)

        except Exception as e:
            print("Runner error:", repr(e))
            try:
                save_state(STATE)
            except Exception:
                pass
            time.sleep(10)

if __name__ == "__main__":
    main()