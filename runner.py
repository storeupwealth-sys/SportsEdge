import json
import os
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests
from websocket import WebSocketApp

# =========================
# REQUIRED
# =========================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

# Optional
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BOT_NAME = os.getenv("BOT_NAME", "God AI Predict Bot").strip()
PING_EVERYONE = os.getenv("PING_EVERYONE", "1").strip() == "1"

# =========================
# Toggles
# =========================
ENABLE_POLY_SPORTS = os.getenv("ENABLE_POLY_SPORTS", "1").strip() == "1"
ENABLE_KALSHI = os.getenv("ENABLE_KALSHI", "1").strip() == "1"

ENABLE_PREGAME_ALERTS = os.getenv("ENABLE_PREGAME_ALERTS", "1").strip() == "1"
ENABLE_LIVE_ALERTS = os.getenv("ENABLE_LIVE_ALERTS", "1").strip() == "1"

LIVE_LATE_GAME_ONLY = os.getenv("LIVE_LATE_GAME_ONLY", "0").strip() == "1"

SCAN_SEC = int(os.getenv("SCAN_SEC", "60"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_SLUGS_PER_LEAGUE = int(os.getenv("MAX_SLUGS_PER_LEAGUE", "80"))

HEARTBEAT_MIN = int(os.getenv("HEARTBEAT_MIN", "30"))

BANKROLL_USD = float(os.getenv("BANKROLL_USD", "0"))

# =========================
# Polymarket sources
# =========================
CBB_PAGE = "https://polymarket.com/sports/cbb/games"
NBA_PAGE = "https://polymarket.com/sports/nba/games"

SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; PolyKalshiEdgeBot/1.3; +https://polymarket.com)",
)
HEADERS = {"User-Agent": USER_AGENT}

# =========================
# Kalshi sources (public market data)
# =========================
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SCAN_SEC = int(os.getenv("KALSHI_SCAN_SEC", "120"))
KALSHI_MAX_MARKETS = int(os.getenv("KALSHI_MAX_MARKETS", "140"))
KALSHI_MIN_SNAPS = int(os.getenv("KALSHI_MIN_SNAPS", "10"))
KALSHI_MIN_MOVE_CENTS = float(os.getenv("KALSHI_MIN_MOVE_CENTS", "2"))
KALSHI_MIN_VOLUME = float(os.getenv("KALSHI_MIN_VOLUME", "5000"))
KALSHI_COOLDOWN_SEC = int(os.getenv("KALSHI_COOLDOWN_SEC", "1800"))

# Limit entry style for Kalshi:
# patient: bid only
# balanced: bid + 1c (capped at ask)
# aggressive: ask (or last price if no ask)
KALSHI_LIMIT_STYLE = os.getenv("KALSHI_LIMIT_STYLE", "balanced").strip().lower()

_kw_raw = os.getenv(
    "KALSHI_KEYWORDS",
    "trump,biden,harris,obama,desantis,newsom,rfk,kennedy,speaker,senate,house,governor,primary,nominee,approval,impeachment,shutdown,debt ceiling,supreme court",
)
KALSHI_KEYWORDS = [k.strip().lower() for k in _kw_raw.split(",") if k.strip()]

# =========================
# Sports tuning (separate)
# =========================
LEAGUE_CFG = {
    "CBB": {
        "slug_prefix": "cbb-",
        "min_snaps": int(os.getenv("CBB_MIN_SNAPS", "12")),
        "min_move": float(os.getenv("CBB_MIN_MOVE", "0.05")),
        "live_cooldown": int(os.getenv("CBB_LIVE_COOLDOWN_SEC", "600")),
        "pregame_cooldown": int(os.getenv("CBB_PREGAME_COOLDOWN_SEC", "900")),
        "min_liquidity": float(os.getenv("CBB_MIN_LIQUIDITY", "2500")),
        "max_spread_cents": float(os.getenv("CBB_MAX_SPREAD_CENTS", "5.0")),
        "pregame_big_move": float(os.getenv("CBB_PREGAME_BIG_MOVE", "0.06")),
        "opening_scout_minutes": int(os.getenv("CBB_OPENING_SCOUT_MIN", "90")),
    },
    "NBA": {
        "slug_prefix": "nba-",
        "min_snaps": int(os.getenv("NBA_MIN_SNAPS", "10")),
        "min_move": float(os.getenv("NBA_MIN_MOVE", "0.04")),
        "live_cooldown": int(os.getenv("NBA_LIVE_COOLDOWN_SEC", "480")),
        "pregame_cooldown": int(os.getenv("NBA_PREGAME_COOLDOWN_SEC", "900")),
        "min_liquidity": float(os.getenv("NBA_MIN_LIQUIDITY", "4000")),
        "max_spread_cents": float(os.getenv("NBA_MAX_SPREAD_CENTS", "5.0")),
        "pregame_big_move": float(os.getenv("NBA_PREGAME_BIG_MOVE", "0.05")),
        "opening_scout_minutes": int(os.getenv("NBA_OPENING_SCOUT_MIN", "90")),
    },
}

# =========================
# Guardrails + exits
# =========================
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.95"))

TP1_CENTS = float(os.getenv("TP1_CENTS", "3"))
TP2_CENTS = float(os.getenv("TP2_CENTS", "6"))
SL_CENTS = float(os.getenv("SL_CENTS", "2"))

TRAIL_START_CENTS = float(os.getenv("TRAIL_START_CENTS", "4"))
TRAIL_GAP_CENTS = float(os.getenv("TRAIL_GAP_CENTS", "2"))
TIME_STOP_MIN = float(os.getenv("TIME_STOP_MIN", "20"))

RECAP_HOUR_LOCAL = int(os.getenv("RECAP_HOUR_LOCAL", "23"))
RECAP_MIN_LOCAL = int(os.getenv("RECAP_MIN_LOCAL", "59"))

STATE_PATH = os.getenv("STATE_PATH", "state.json")

# =========================
# Utils
# =========================
MARKET_PARAM_RE = re.compile(r"market=([a-z0-9\-]+)", re.IGNORECASE)

def now_ts() -> int:
    return int(time.time())

def prefix_ping() -> str:
    return "@everyone " if PING_EVERYONE else ""

def safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def cents_diff(now_p: float, old_p: float) -> float:
    return (now_p - old_p) * 100.0

def market_link(slug: str) -> str:
    return f"https://polymarket.com/market/{slug}"

def notify(text: str) -> None:
    if DISCORD_WEBHOOK:
        try:
            r = requests.post(DISCORD_WEBHOOK, json={"content": text}, timeout=HTTP_TIMEOUT)
            if r.status_code >= 300:
                print("Discord error:", r.status_code, r.text[:200])
        except Exception as e:
            print("Discord send failed:", e)
    else:
        print(text)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
            r = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
            if r.status_code >= 300:
                print("Telegram error:", r.status_code, r.text[:200])
        except Exception as e:
            print("Telegram send failed:", e)

def kelly_fraction_from_conf(conf: float) -> float:
    x = max(0.0, conf - 5.5)
    frac = min(0.04, (x / 4.0) * 0.035)
    return frac

def in_guardrails(p: float) -> bool:
    return MIN_PRICE <= p <= MAX_PRICE

# =========================
# State
# =========================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {
            "history": {},
            "cooldowns": {},
            "positions": {},
            "first_seen": {},
            "alerts_log": [],
            "results": {},
            "last_slugs": {"CBB": [], "NBA": []},
            "last_recap_day": None,
            "last_heartbeat_ts": 0,
            "kalshi_last_scan_ts": 0,
            "kalshi_snapshot": {},
        }
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            raise ValueError("bad state")
        s.setdefault("history", {})
        s.setdefault("cooldowns", {})
        s.setdefault("positions", {})
        s.setdefault("first_seen", {})
        s.setdefault("alerts_log", [])
        s.setdefault("results", {})
        s.setdefault("last_slugs", {"CBB": [], "NBA": []})
        s["last_slugs"].setdefault("CBB", [])
        s["last_slugs"].setdefault("NBA", [])
        s.setdefault("last_recap_day", None)
        s.setdefault("last_heartbeat_ts", 0)
        s.setdefault("kalshi_last_scan_ts", 0)
        s.setdefault("kalshi_snapshot", {})
        return s
    except Exception:
        return {
            "history": {},
            "cooldowns": {},
            "positions": {},
            "first_seen": {},
            "alerts_log": [],
            "results": {},
            "last_slugs": {"CBB": [], "NBA": []},
            "last_recap_day": None,
            "last_heartbeat_ts": 0,
            "kalshi_last_scan_ts": 0,
            "kalshi_snapshot": {},
        }

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)

STATE = load_state()

# =========================
# Shared history + cooldown
# =========================
def hist_key(source: str, league: str, ident: str, outcome: str) -> str:
    return f"{source}|{league}|{ident}|{outcome}".lower()

def push_history(key: str, price: float) -> None:
    h = STATE["history"].setdefault(key, [])
    h.append({"t": now_ts(), "p": float(price)})
    if len(h) > 320:
        del h[:-320]

def hist_len(key: str) -> int:
    return len(STATE["history"].get(key) or [])

def prev_price(key: str) -> Optional[float]:
    h = STATE["history"].get(key) or []
    if len(h) < 2:
        return None
    return float(h[-2]["p"])

def price_over_snaps(key: str, snaps: int) -> Optional[Tuple[float, float]]:
    h = STATE["history"].get(key) or []
    if len(h) < snaps:
        return None
    return float(h[-snaps]["p"]), float(h[-1]["p"])

def cooldown_ok(cool_key: str, cooldown_sec: int) -> bool:
    now = now_ts()
    last = int(STATE["cooldowns"].get(cool_key, 0))
    if now - last < cooldown_sec:
        return False
    STATE["cooldowns"][cool_key] = now
    return True

# =========================
# Positions + exits
# =========================
def pos_open(key: str, entry_price: float) -> None:
    if key in STATE["positions"]:
        return
    STATE["positions"][key] = {"entry": float(entry_price), "opened": now_ts(), "peak": float(entry_price)}

def pos_update_peak(key: str, price_now: float) -> None:
    p = STATE["positions"].get(key)
    if not p:
        return
    peak = float(p.get("peak", price_now))
    if price_now > peak:
        p["peak"] = float(price_now)

def pos_close(key: str) -> None:
    if key in STATE["positions"]:
        del STATE["positions"][key]

def exit_signal(key: str, price_now: float) -> Optional[Tuple[str, str]]:
    p = STATE["positions"].get(key)
    if not p:
        return None

    entry = float(p["entry"])
    opened = int(p["opened"])
    peak = float(p.get("peak", entry))

    pnl_c = cents_diff(price_now, entry)

    if pnl_c >= TP2_CENTS:
        return "TP2", f"TP2 hit (+{pnl_c:.1f}c)"
    if pnl_c >= TP1_CENTS:
        return "TP1", f"TP1 hit (+{pnl_c:.1f}c)"
    if pnl_c <= -SL_CENTS:
        return "SL", f"Stop hit ({pnl_c:.1f}c)"

    gain_from_entry_c = cents_diff(peak, entry)
    if gain_from_entry_c >= TRAIL_START_CENTS:
        trail_floor = peak - (TRAIL_GAP_CENTS / 100.0)
        if price_now <= trail_floor:
            return "TRAIL", "Trailing stop hit"

    age_min = (now_ts() - opened) / 60.0
    if age_min >= TIME_STOP_MIN and pnl_c < (TP1_CENTS * 0.5):
        return "TIME", f"Time stop ({age_min:.0f}m)"

    return None

# =========================
# Alerts log for recap
# =========================
def log_alert(source: str, league: str, ident: str, outcome: str, price: float, conf: float, kind: str, reason: str) -> None:
    STATE["alerts_log"].append(
        {
            "ts": now_ts(),
            "source": source,   # POLY or KALSHI
            "league": league,
            "ident": ident,     # slug or ticker
            "outcome": outcome,
            "price": float(price),
            "conf": float(conf),
            "kind": kind,       # ENTRY, SCOUT, UPDATE
            "reason": reason,
        }
    )
    if len(STATE["alerts_log"]) > 2500:
        STATE["alerts_log"] = STATE["alerts_log"][-2000:]

# =========================
# Polymarket Sports WS
# =========================
WS_STATE: Dict[str, Dict[str, Any]] = {}
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
    if league not in ("cbb", "nba"):
        return

    WS_STATE[slug] = obj
    WS_LAST_TS = now_ts()

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

threading.Thread(target=ws_loop, daemon=True).start()

def late_game_ok(ws: Optional[Dict[str, Any]]) -> bool:
    if not LIVE_LATE_GAME_ONLY:
        return True
    if not ws:
        return False
    period = str(ws.get("period") or "")
    return period in ("2H", "4Q", "OT", "FT OT")

def infer_winner_from_ws(ws: Dict[str, Any]) -> Optional[str]:
    score = str(ws.get("score") or "")
    away = ws.get("awayTeam")
    home = ws.get("homeTeam")
    if not score or "-" not in score or not away or not home:
        return None
    try:
        a_str, h_str = score.split("-", 1)
        a = int(a_str.strip())
        h = int(h_str.strip())
        if a > h:
            return str(away)
        if h > a:
            return str(home)
        return None
    except Exception:
        return None

# =========================
# Polymarket scraping + Gamma
# =========================
def scrape_slugs(page_url: str, slug_prefix: str) -> List[str]:
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print("Scrape error:", page_url, e)
        return []

    slugs: List[str] = []
    for m in MARKET_PARAM_RE.finditer(html):
        s = m.group(1).strip()
        if s.lower().startswith(slug_prefix):
            slugs.append(s)

    seen = set()
    out = []
    for s in slugs:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out[:MAX_SLUGS_PER_LEAGUE]

def merge_ws_slugs(slug_prefix: str, base: List[str]) -> List[str]:
    ws_slugs = [s for s in WS_STATE.keys() if s.lower().startswith(slug_prefix)]
    merged = []
    seen = set()
    for s in base + ws_slugs:
        if s in seen:
            continue
        seen.add(s)
        merged.append(s)
    return merged[:MAX_SLUGS_PER_LEAGUE]

def maybe_json_list(v: Any) -> Any:
    if isinstance(v, str) and v.startswith("["):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v

def fetch_gamma_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
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

def extract_poly_outcomes(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    outcomes = maybe_json_list(market.get("outcomes")) or []
    best_ask = maybe_json_list(market.get("bestAsk"))
    best_bid = maybe_json_list(market.get("bestBid"))
    outcome_prices = maybe_json_list(market.get("outcomePrices"))

    liq = safe_float(market.get("liquidity")) or 0.0
    vol = safe_float(market.get("volume")) or safe_float(market.get("volume24hr")) or 0.0
    liquidity = liq if liq > 0 else vol

    packs: List[Dict[str, Any]] = []
    if not isinstance(outcomes, list) or len(outcomes) < 2:
        return packs

    for i, name in enumerate(outcomes):
        team = str(name).strip()
        if team.lower() in ("yes", "no"):
            continue

        ask = safe_float(best_ask[i]) if isinstance(best_ask, list) and i < len(best_ask) else None
        bid = safe_float(best_bid[i]) if isinstance(best_bid, list) and i < len(best_bid) else None
        mid = None

        if ask is None and bid is None and isinstance(outcome_prices, list) and i < len(outcome_prices):
            mid = safe_float(outcome_prices[i])

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
                "mid": float(mid),
                "spread_c": spread_c,
                "liquidity": float(liquidity),
            }
        )

    return packs

def poly_confidence(league: str, move: float, liquidity: float, spread_c: Optional[float], is_live: bool) -> float:
    base = 4.5
    base += min(3.0, abs(move) * 50.0)

    if liquidity >= 15000:
        base += 1.5
    elif liquidity >= 8000:
        base += 1.0
    elif liquidity >= 4000:
        base += 0.6
    elif liquidity >= 2500:
        base += 0.3

    if spread_c is not None:
        if spread_c <= 2.0:
            base += 0.8
        elif spread_c <= 4.0:
            base += 0.4
        elif spread_c >= 8.0:
            base -= 0.8

    if is_live:
        base += 0.5
    if league == "NBA":
        base += 0.3

    return max(1.0, min(10.0, base))

def record_first_seen(key: str, price: float) -> None:
    if key in STATE["first_seen"]:
        return
    STATE["first_seen"][key] = {"ts": now_ts(), "p": float(price)}

def in_opening_window(key: str, opening_minutes: int) -> bool:
    fs = STATE["first_seen"].get(key)
    if not fs:
        return True
    minutes = (now_ts() - int(fs["ts"])) / 60.0
    return minutes <= float(opening_minutes)

def opening_scout_signal(key: str, price_now: float, thresh: float, opening_minutes: int) -> Optional[float]:
    fs = STATE["first_seen"].get(key)
    if not fs:
        return None
    p0 = float(fs["p"])
    mv = price_now - p0
    if abs(mv) >= thresh and in_opening_window(key, opening_minutes):
        return mv
    return None

def pregame_big_move_signal(key: str, snaps: int, thresh: float) -> Optional[float]:
    pair = price_over_snaps(key, snaps)
    if not pair:
        return None
    old_p, now_p = pair
    mv = now_p - old_p
    if abs(mv) >= thresh:
        return mv
    return None

def poly_entry_message(kind: str, league: str, slug: str, team: str, price: float, move: float, liq: float, spread_c: Optional[float], ws: Optional[Dict[str, Any]], conf: float) -> str:
    is_live = bool(ws and ws.get("live"))
    status = "LIVE" if is_live else "PREGAME"

    period = (str(ws.get("period")) if ws else "").strip()
    score = (str(ws.get("score")) if ws else "").strip()
    elapsed = (str(ws.get("elapsed")) if ws else "").strip()

    move_c = move * 100.0
    move_emoji = "📈" if move > 0 else "📉"
    spread_txt = f"{spread_c:.1f}c" if spread_c is not None else "n/a"

    sizing_txt = ""
    if BANKROLL_USD > 0:
        frac = kelly_fraction_from_conf(conf)
        stake = max(0.0, BANKROLL_USD * frac)
        sizing_txt = f"\n💵 Sizing: ~{frac*100:.1f}% bankroll ≈ ${stake:.2f}"

    ctx = ""
    if is_live and (period or score):
        ctx = f"\n⏱️ {period} {elapsed} | 🧾 {score}".strip()

    return (
        f"{prefix_ping()}🚨 {BOT_NAME} {league} {status} {kind}\n"
        f"🏀 {team}  💰 {price:.2f}\n"
        f"{move_emoji} Move: {move_c:+.1f}c | 💧Liq: {int(liq):,} | ↔️ Spread: {spread_txt}\n"
        f"🧠 Confidence: {conf:.1f}/10"
        f"{sizing_txt}"
        f"{ctx}\n"
        f"🔗 {market_link(slug)}"
    )

def poly_update_message(league: str, slug: str, team: str, entry: float, nowp: float, action: str, reason: str) -> str:
    pnl_c = cents_diff(nowp, entry)
    emoji = "✅" if pnl_c >= 0 else "⚠️"
    return (
        f"{prefix_ping()}{emoji} {BOT_NAME} {league} SCALE UPDATE\n"
        f"🏀 {team}\n"
        f"Entry: {entry*100:.0f}c | Now: {nowp*100:.0f}c | PnL: {pnl_c:+.1f}c\n"
        f"Action: {action} | {reason}\n"
        f"🔗 {market_link(slug)}"
    )

# =========================
# Kalshi
# =========================
def kalshi_get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{KALSHI_BASE}{path}"
    r = requests.get(url, params=params or {}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, dict) else {}

def kalshi_politics_series(limit: int = 200) -> List[Dict[str, Any]]:
    data = kalshi_get_json("/series", params={"category": "Politics", "limit": limit})
    return data.get("series", []) or []

def kalshi_open_markets_for_series(series_ticker: str, limit: int = 200) -> List[Dict[str, Any]]:
    data = kalshi_get_json("/markets", params={"series_ticker": series_ticker, "status": "open", "limit": limit})
    return data.get("markets", []) or []

def kalshi_matches_keywords(title: str, ticker: str, event_ticker: str) -> bool:
    hay = f"{title} {ticker} {event_ticker}".lower()
    return any(k in hay for k in KALSHI_KEYWORDS)

def kalshi_yes_price_cents(market: Dict[str, Any]) -> Optional[float]:
    yp = market.get("yes_price")
    if yp is None:
        return None
    try:
        return float(yp)
    except Exception:
        return None

def kalshi_orderbook(ticker: str) -> Optional[Dict[str, Any]]:
    try:
        return kalshi_get_json(f"/markets/{ticker}/orderbook")
    except Exception:
        return None

def _extract_best_price_from_side(orders: List[Any], side: str) -> Optional[float]:
    prices: List[float] = []
    for o in orders:
        if isinstance(o, dict) and "price" in o:
            p = safe_float(o.get("price"))
            if p is not None:
                prices.append(p)
        elif isinstance(o, list) and o:
            p = safe_float(o[0])
            if p is not None:
                prices.append(p)
    if not prices:
        return None
    return max(prices) if side == "bid" else min(prices)

def kalshi_best_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(orderbook, dict):
        return None, None

    ob = orderbook.get("orderbook") if isinstance(orderbook.get("orderbook"), dict) else orderbook

    best_bid = None
    best_ask = None

    for k in ("yes", "yes_orders", "yesOrderBook", "yes_orderbook"):
        v = ob.get(k)
        if isinstance(v, dict):
            bids = v.get("bids")
            asks = v.get("asks")
            if isinstance(bids, list) and bids:
                bb = _extract_best_price_from_side(bids, side="bid")
                if bb is not None:
                    best_bid = bb if best_bid is None else max(best_bid, bb)
            if isinstance(asks, list) and asks:
                ba = _extract_best_price_from_side(asks, side="ask")
                if ba is not None:
                    best_ask = ba if best_ask is None else min(best_ask, ba)

    return best_bid, best_ask

def kalshi_recommended_limit(yes_c: float, best_bid: Optional[float], best_ask: Optional[float]) -> Tuple[float, str]:
    style = KALSHI_LIMIT_STYLE

    if best_bid is not None and best_ask is not None and best_ask >= best_bid:
        if style == "patient":
            return float(best_bid), "patient: bid only"
        if style == "aggressive":
            return float(best_ask), "aggressive: take ask"
        # balanced
        if best_ask == best_bid:
            return float(best_bid), "balanced: tight market"
        return float(min(best_ask, best_bid + 1.0)), "balanced: bid+1c capped at ask"

    # fallback if orderbook not usable
    if style == "aggressive":
        return float(max(1.0, yes_c)), "aggressive: no book, using last"
    # patient or balanced fallback
    return float(max(1.0, yes_c - 1.0)), "no book, patient limit 1c below last"

def kalshi_contracts_for_stake(stake_usd: float, limit_cents: float) -> int:
    if limit_cents <= 0:
        return 0
    cost = limit_cents / 100.0
    if cost <= 0:
        return 0
    return int(stake_usd // cost)

def kalshi_confidence(move_snaps_c: float, volume: float) -> float:
    base = 4.5
    base += min(3.0, abs(move_snaps_c) / 2.0)
    if volume >= 50000:
        base += 1.5
    elif volume >= 20000:
        base += 1.0
    elif volume >= 10000:
        base += 0.6
    elif volume >= 5000:
        base += 0.3
    return max(1.0, min(10.0, base))

def kalshi_alert_message(title: str, ticker: str, yes_c: float, move_snaps_c: float, volume: float, best_bid: Optional[float], best_ask: Optional[float], conf: float) -> str:
    move_emoji = "📈" if move_snaps_c > 0 else "📉"

    limit_price, limit_note = kalshi_recommended_limit(yes_c, best_bid, best_ask)

    sizing_txt = ""
    if BANKROLL_USD > 0:
        frac = kelly_fraction_from_conf(conf)
        stake = max(0.0, BANKROLL_USD * frac)
        contracts = kalshi_contracts_for_stake(stake, limit_price)
        sizing_txt = f"\n💵 Limit buy: {limit_price:.0f}¢ | Size: ~${stake:.2f} ≈ {contracts} contracts"

    bidask_txt = ""
    if best_bid is not None or best_ask is not None:
        bidask_txt = f"\n↔️ YES bid/ask: {best_bid if best_bid is not None else 'n/a'}¢ / {best_ask if best_ask is not None else 'n/a'}¢"

    return (
        f"{prefix_ping()}🏛️ {BOT_NAME} KALSHI POLITICS\n"
        f"🗳️ {title}\n"
        f"YES: {yes_c:.0f}¢  {move_emoji} Move: {move_snaps_c:+.0f}¢  Vol: {int(volume):,}\n"
        f"🧠 Confidence: {conf:.1f}/10"
        f"{bidask_txt}"
        f"\n🎯 Limit: {limit_price:.0f}¢ ({limit_note})"
        f"{sizing_txt}"
        f"\n🔎 Ticker: {ticker}"
    )

# =========================
# Daily recap
# =========================
def local_day_str() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())

def local_hm() -> Tuple[int, int]:
    lt = time.localtime()
    return lt.tm_hour, lt.tm_min

def run_daily_recap_if_time() -> None:
    day = local_day_str()
    hour, minute = local_hm()

    if STATE.get("last_recap_day") == day:
        return
    if hour != RECAP_HOUR_LOCAL or minute != RECAP_MIN_LOCAL:
        return

    since_ts = now_ts() - 24 * 3600
    alerts = [a for a in STATE["alerts_log"] if int(a["ts"]) >= since_ts and a.get("kind") in ("ENTRY", "SCOUT")]

    sports_graded = 0
    sports_wins = 0
    sports_losses = 0

    kalshi_alerts = 0
    kalshi_mtm_sum = 0.0
    kalshi_mtm_n = 0

    by_source = {"POLY": 0, "KALSHI": 0}
    by_league = {"CBB": 0, "NBA": 0, "POLITICS": 0}

    for a in alerts:
        src = a.get("source")
        league = a.get("league")
        by_source[src] = by_source.get(src, 0) + 1
        by_league[league] = by_league.get(league, 0) + 1

        if src == "POLY":
            slug = a.get("ident")
            res = STATE["results"].get(slug)
            if res and res.get("winner"):
                sports_graded += 1
                if str(a.get("outcome")) == str(res.get("winner")):
                    sports_wins += 1
                else:
                    sports_losses += 1

        if src == "KALSHI":
            kalshi_alerts += 1
            ticker = str(a.get("ident"))
            entry_c = float(a.get("price", 0.0))
            cur_c = STATE.get("kalshi_snapshot", {}).get(ticker)
            if cur_c is not None:
                kalshi_mtm_sum += float(cur_c) - entry_c
                kalshi_mtm_n += 1

    sports_hit = (sports_wins / sports_graded * 100.0) if sports_graded > 0 else 0.0
    kalshi_avg_mtm = (kalshi_mtm_sum / kalshi_mtm_n) if kalshi_mtm_n > 0 else 0.0

    msg = (
        f"📊 {BOT_NAME} Daily Recap\n"
        f"Alerts (24h): {len(alerts)} | POLY: {by_source.get('POLY',0)} | KALSHI: {by_source.get('KALSHI',0)}\n"
        f"CBB: {by_league.get('CBB',0)} | NBA: {by_league.get('NBA',0)} | Politics: {by_league.get('POLITICS',0)}\n"
        f"Sports graded: {sports_graded} | W: {sports_wins} | L: {sports_losses} | Hit: {sports_hit:.1f}%\n"
        f"Kalshi MTM avg: {kalshi_avg_mtm:+.2f}¢ across {kalshi_mtm_n} alerts\n"
        f"Notes: Sports grading uses Polymarket Sports WS finals. Kalshi recap is mark-to-market."
    )
    notify(msg)
    STATE["last_recap_day"] = day

# =========================
# Main
# =========================
def main():
    notify(f"🟣 {BOT_NAME} is ONLINE ✅ | Polymarket (CBB+NBA) + Kalshi (Politics)")

    while True:
        try:
            # Heartbeat
            if HEARTBEAT_MIN > 0:
                if now_ts() - int(STATE.get("last_heartbeat_ts", 0)) >= HEARTBEAT_MIN * 60:
                    STATE["last_heartbeat_ts"] = now_ts()
                    notify(
                        f"🟣 {BOT_NAME} heartbeat ✅ | "
                        f"CBB={len(STATE['last_slugs'].get('CBB', []))} | "
                        f"NBA={len(STATE['last_slugs'].get('NBA', []))} | "
                        f"WS={WS_LAST_TS} | "
                        f"KalshiStyle={KALSHI_LIMIT_STYLE}"
                    )

            # Recap
            run_daily_recap_if_time()

            # =========================
            # Polymarket sports scan
            # =========================
            if ENABLE_POLY_SPORTS:
                cbb_slugs = scrape_slugs(CBB_PAGE, LEAGUE_CFG["CBB"]["slug_prefix"])
                nba_slugs = scrape_slugs(NBA_PAGE, LEAGUE_CFG["NBA"]["slug_prefix"])

                if cbb_slugs:
                    STATE["last_slugs"]["CBB"] = cbb_slugs
                else:
                    cbb_slugs = STATE["last_slugs"]["CBB"]

                if nba_slugs:
                    STATE["last_slugs"]["NBA"] = nba_slugs
                else:
                    nba_slugs = STATE["last_slugs"]["NBA"]

                cbb_slugs = merge_ws_slugs(LEAGUE_CFG["CBB"]["slug_prefix"], cbb_slugs)
                nba_slugs = merge_ws_slugs(LEAGUE_CFG["NBA"]["slug_prefix"], nba_slugs)

                for league, slugs in (("CBB", cbb_slugs), ("NBA", nba_slugs)):
                    cfg = LEAGUE_CFG[league]
                    for slug in slugs:
                        ws = WS_STATE.get(slug)
                        is_live = bool(ws and ws.get("live"))
                        ended = bool(ws and ws.get("ended"))

                        if ended and ws:
                            winner = infer_winner_from_ws(ws)
                            if winner:
                                STATE["results"][slug] = {"winner": winner, "ended_ts": now_ts()}

                        if is_live and not ENABLE_LIVE_ALERTS:
                            continue
                        if (not is_live) and not ENABLE_PREGAME_ALERTS:
                            continue
                        if is_live and not late_game_ok(ws):
                            continue

                        market = fetch_gamma_market_by_slug(slug)
                        if not market:
                            continue
                        outcomes = extract_poly_outcomes(market)
                        if not outcomes:
                            continue

                        for o in outcomes:
                            team = o["team"]
                            mid = float(o["mid"])
                            liq = float(o["liquidity"])
                            spread_c = o.get("spread_c")

                            if not in_guardrails(mid):
                                continue
                            if liq < float(cfg["min_liquidity"]):
                                continue
                            if spread_c is not None and float(spread_c) > float(cfg["max_spread_cents"]):
                                continue

                            key = hist_key("POLY", league, slug, team)
                            record_first_seen(key, mid)
                            push_history(key, mid)

                            pprev = prev_price(key)
                            if pprev is None:
                                continue
                            if hist_len(key) < int(cfg["min_snaps"]):
                                continue

                            pair = price_over_snaps(key, int(cfg["min_snaps"]))
                            if not pair:
                                continue
                            old_p, now_p = pair
                            move = now_p - old_p

                            # Pregame scout alerts
                            if (not is_live) and ENABLE_PREGAME_ALERTS:
                                open_mv = opening_scout_signal(
                                    key,
                                    mid,
                                    float(cfg["pregame_big_move"]),
                                    int(cfg["opening_scout_minutes"]),
                                )
                                if open_mv is not None:
                                    conf = poly_confidence(league, open_mv, liq, spread_c, False)
                                    cool_key = key + "|SCOUT_OPEN"
                                    if cooldown_ok(cool_key, int(cfg["pregame_cooldown"])):
                                        msg = poly_entry_message("PREGAME SCOUT (Opening)", league, slug, team, mid, open_mv, liq, spread_c, None, conf)
                                        notify(msg)
                                        log_alert("POLY", league, slug, team, mid, conf, "SCOUT", "opening-scout")

                                big_mv = pregame_big_move_signal(key, int(cfg["min_snaps"]), float(cfg["pregame_big_move"]))
                                if big_mv is not None:
                                    conf = poly_confidence(league, big_mv, liq, spread_c, False)
                                    cool_key = key + "|SCOUT_MOM"
                                    if cooldown_ok(cool_key, int(cfg["pregame_cooldown"])):
                                        msg = poly_entry_message("PREGAME SCOUT (Momentum)", league, slug, team, mid, big_mv, liq, spread_c, None, conf)
                                        notify(msg)
                                        log_alert("POLY", league, slug, team, mid, conf, "SCOUT", "pregame-momentum")

                            # Entry displacement
                            tick_move = mid - pprev
                            if abs(tick_move) >= float(cfg["min_move"]):
                                conf = poly_confidence(league, move, liq, spread_c, is_live)
                                cool_key = key + ("|LIVE" if is_live else "|PREGAME")
                                cd = int(cfg["live_cooldown"]) if is_live else int(cfg["pregame_cooldown"])
                                if cooldown_ok(cool_key, cd):
                                    msg = poly_entry_message("BET", league, slug, team, mid, move, liq, spread_c, ws if is_live else None, conf)
                                    notify(msg)
                                    log_alert("POLY", league, slug, team, mid, conf, "ENTRY", "displacement")
                                    pos_open(key, mid)

                            # Exits
                            if key in STATE["positions"]:
                                pos_update_peak(key, mid)
                                sig = exit_signal(key, mid)
                                if sig:
                                    action, reason = sig
                                    entry = float(STATE["positions"][key]["entry"])
                                    notify(poly_update_message(league, slug, team, entry, mid, action, reason))
                                    log_alert("POLY", league, slug, team, mid, 0.0, "UPDATE", f"exit-{action}")
                                    if action in ("TP2", "SL", "TRAIL", "TIME"):
                                        pos_close(key)

            # =========================
            # Kalshi politics scan
            # =========================
            if ENABLE_KALSHI and (now_ts() - int(STATE.get("kalshi_last_scan_ts", 0))) >= KALSHI_SCAN_SEC:
                STATE["kalshi_last_scan_ts"] = now_ts()

                try:
                    series_list = kalshi_politics_series(limit=200)
                    markets_seen = 0
                    alerts_sent = 0

                    for s in series_list:
                        st = s.get("ticker")
                        if not st:
                            continue

                        mkts = kalshi_open_markets_for_series(st, limit=200)
                        for m in mkts:
                            if markets_seen >= KALSHI_MAX_MARKETS:
                                break

                            title = str(m.get("title") or "").strip()
                            ticker = str(m.get("ticker") or "").strip()
                            event_ticker = str(m.get("event_ticker") or "").strip()
                            if not title or not ticker:
                                continue
                            if not kalshi_matches_keywords(title, ticker, event_ticker):
                                continue

                            yes_c = kalshi_yes_price_cents(m)
                            if yes_c is None:
                                continue

                            volume = float(m.get("volume") or 0.0)
                            if volume < KALSHI_MIN_VOLUME:
                                continue

                            markets_seen += 1
                            STATE["kalshi_snapshot"][ticker] = float(yes_c)

                            key = hist_key("KALSHI", "POLITICS", ticker, "YES")
                            push_history(key, yes_c)

                            pprev = prev_price(key)
                            if pprev is None:
                                continue
                            if hist_len(key) < KALSHI_MIN_SNAPS:
                                continue

                            pair = price_over_snaps(key, KALSHI_MIN_SNAPS)
                            if not pair:
                                continue
                            old_p, now_p = pair
                            move_snaps_c = now_p - old_p

                            # Entry alert on tick move
                            if abs(yes_c - pprev) >= KALSHI_MIN_MOVE_CENTS:
                                cool_key = key + "|ALERT"
                                if cooldown_ok(cool_key, KALSHI_COOLDOWN_SEC):
                                    ob = kalshi_orderbook(ticker)
                                    best_bid, best_ask = (None, None)
                                    if ob:
                                        best_bid, best_ask = kalshi_best_bid_ask(ob)

                                    conf = kalshi_confidence(move_snaps_c, volume)
                                    msg = kalshi_alert_message(title, ticker, yes_c, move_snaps_c, volume, best_bid, best_ask, conf)
                                    notify(msg)
                                    log_alert("KALSHI", "POLITICS", ticker, "YES", yes_c, conf, "ENTRY", "displacement")
                                    alerts_sent += 1
                                    pos_open(key, yes_c)

                            # Exits
                            if key in STATE["positions"]:
                                pos_update_peak(key, yes_c)
                                sig = exit_signal(key, yes_c)
                                if sig:
                                    action, reason = sig
                                    entry = float(STATE["positions"][key]["entry"])
                                    pnl_c = yes_c - entry
                                    emoji = "✅" if pnl_c >= 0 else "⚠️"
                                    upd = (
                                        f"{prefix_ping()}{emoji} {BOT_NAME} KALSHI SCALE UPDATE\n"
                                        f"🗳️ {title}\n"
                                        f"Entry: {entry:.0f}¢ | Now: {yes_c:.0f}¢ | PnL: {pnl_c:+.0f}¢\n"
                                        f"Action: {action} | {reason}\n"
                                        f"🔎 Ticker: {ticker}"
                                    )
                                    notify(upd)
                                    log_alert("KALSHI", "POLITICS", ticker, "YES", yes_c, 0.0, "UPDATE", f"exit-{action}")
                                    if action in ("TP2", "SL", "TRAIL", "TIME"):
                                        pos_close(key)

                    print(f"KALSHI scan ok | markets_seen={markets_seen} alerts={alerts_sent}")
                except Exception as e:
                    print("Kalshi scan error:", repr(e))

            save_state(STATE)
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