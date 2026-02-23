# runner.py
# God AI Predict Bot (Polymarket) - CBB + NBA - single file
# No Odds API. Polymarket only.
#
# What it does:
# - Watches NBA + CBB markets
# - Uses Polymarket Sports WebSocket for live status + score
# - Uses Gamma API for market prices (bestAsk/bestBid/outcomePrices) + liquidity
# - Sends Discord + optional Telegram alerts
# - Pregame "scan ahead" alerts (momentum + opening scalp style)
# - Live alerts + optional late-game-only filter (2H/4Q)
# - Entry alerts + exits (TP1/TP2/SL/trailing/time stop)
# - Kelly-style sizing suggestion from confidence + BANKROLL_USD
# - Daily recap based on finished games (when WS provides ended status + score)

import json
import os
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests
from websocket import WebSocketApp

# =========================
# REQUIRED (set in Railway)
# =========================
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

# Optional
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BOT_NAME = os.getenv("BOT_NAME", "God AI Predict Bot").strip()
PING_EVERYONE = os.getenv("PING_EVERYONE", "1").strip() == "1"

# =========================
# Behavior toggles
# =========================
ENABLE_PREGAME_ALERTS = os.getenv("ENABLE_PREGAME_ALERTS", "1").strip() == "1"
ENABLE_LIVE_ALERTS = os.getenv("ENABLE_LIVE_ALERTS", "1").strip() == "1"

# If enabled, only alert live games during 2H/4Q (stronger window)
LIVE_LATE_GAME_ONLY = os.getenv("LIVE_LATE_GAME_ONLY", "0").strip() == "1"

# Heartbeat to prove it is alive even when no games
HEARTBEAT_MIN = int(os.getenv("HEARTBEAT_MIN", "30"))

# Scan speed
SCAN_SEC = int(os.getenv("SCAN_SEC", "60"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_SLUGS_PER_LEAGUE = int(os.getenv("MAX_SLUGS_PER_LEAGUE", "80"))

# Bankroll sizing (set your bankroll here)
BANKROLL_USD = float(os.getenv("BANKROLL_USD", "0"))  # set 39 if you want your personal sizing

# =========================
# Market sources
# =========================
CBB_PAGE = "https://polymarket.com/sports/cbb/games"
NBA_PAGE = "https://polymarket.com/sports/nba/games"

SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; PolymarketEdgeBot/1.1; +https://polymarket.com)",
)
HEADERS = {"User-Agent": USER_AGENT}

STATE_PATH = os.getenv("STATE_PATH", "state.json")

# =========================
# League tuning (separate)
# =========================
LEAGUE_CFG = {
    "CBB": {
        "slug_prefix": "cbb-",
        "min_snaps": int(os.getenv("CBB_MIN_SNAPS", "12")),
        "min_move": float(os.getenv("CBB_MIN_MOVE", "0.05")),  # 5c
        "live_cooldown": int(os.getenv("CBB_LIVE_COOLDOWN_SEC", "600")),
        "pregame_cooldown": int(os.getenv("CBB_PREGAME_COOLDOWN_SEC", "900")),
        "min_liquidity": float(os.getenv("CBB_MIN_LIQUIDITY", "2500")),
        "max_spread_cents": float(os.getenv("CBB_MAX_SPREAD_CENTS", "5.0")),
        "pregame_big_move": float(os.getenv("CBB_PREGAME_BIG_MOVE", "0.06")),  # 6c over snaps window
        "opening_scout_minutes": int(os.getenv("CBB_OPENING_SCOUT_MIN", "90")),  # treat first 90 min as "opening window"
    },
    "NBA": {
        "slug_prefix": "nba-",
        "min_snaps": int(os.getenv("NBA_MIN_SNAPS", "10")),
        "min_move": float(os.getenv("NBA_MIN_MOVE", "0.04")),  # 4c
        "live_cooldown": int(os.getenv("NBA_LIVE_COOLDOWN_SEC", "480")),
        "pregame_cooldown": int(os.getenv("NBA_PREGAME_COOLDOWN_SEC", "900")),
        "min_liquidity": float(os.getenv("NBA_MIN_LIQUIDITY", "4000")),
        "max_spread_cents": float(os.getenv("NBA_MAX_SPREAD_CENTS", "5.0")),
        "pregame_big_move": float(os.getenv("NBA_PREGAME_BIG_MOVE", "0.05")),  # 5c over snaps window
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

# Daily recap
RECAP_HOUR_LOCAL = int(os.getenv("RECAP_HOUR_LOCAL", "23"))  # 11pm local default
RECAP_MIN_LOCAL = int(os.getenv("RECAP_MIN_LOCAL", "59"))

# =========================
# Utilities
# =========================
MARKET_PARAM_RE = re.compile(r"market=([a-z0-9\-]+)", re.IGNORECASE)

def now_ts() -> int:
    return int(time.time())

def pct(x: float) -> float:
    return x * 100.0

def cents_diff(now_p: float, old_p: float) -> float:
    return (now_p - old_p) * 100.0

def safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

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

def prefix_ping() -> str:
    return "@everyone " if PING_EVERYONE else ""

def market_link(slug: str) -> str:
    return f"https://polymarket.com/market/{slug}"

# =========================
# State persistence
# =========================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {
            "history": {},            # key -> list of {"t":ts,"p":price}
            "cooldowns": {},          # key -> last alert ts
            "positions": {},          # key -> {"entry":p,"opened":ts,"peak":p}
            "first_seen": {},         # key -> {"ts":ts,"p":price}
            "alerts_log": [],         # list of alerts for recap
            "results": {},            # slug -> {"winner": team, "ended_ts": ts}
            "last_slugs": {"CBB": [], "NBA": []},
            "last_recap_day": None,   # YYYY-MM-DD
            "last_heartbeat_ts": 0,
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
        }

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)

STATE = load_state()

# =========================
# Sports WebSocket (live status, score, ended)
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

# =========================
# Slug discovery
# =========================
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

    # Dedup preserving order
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

# =========================
# Gamma market fetch + parse
# =========================
def maybe_json_list(v: Any) -> Any:
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

def extract_outcomes(market: Dict[str, Any]) -> List[Dict[str, Any]]:
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

        ask = None
        bid = None
        mid = None

        if isinstance(best_ask, list) and i < len(best_ask):
            ask = safe_float(best_ask[i])
        if isinstance(best_bid, list) and i < len(best_bid):
            bid = safe_float(best_bid[i])

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
                "ask": ask,
                "bid": bid,
                "spread_c": spread_c,
                "liquidity": float(liquidity),
            }
        )

    return packs

# =========================
# Signal + risk logic
# =========================
def key_for(league: str, slug: str, team: str) -> str:
    return f"{league}|{slug}|{team}".lower()

def push_history(key: str, price: float) -> None:
    h = STATE["history"].setdefault(key, [])
    h.append({"t": now_ts(), "p": float(price)})
    if len(h) > 250:
        del h[:-250]

def hist_len(key: str) -> int:
    return len(STATE["history"].get(key) or [])

def prev_price(key: str) -> Optional[float]:
    h = STATE["history"].get(key) or []
    if len(h) < 2:
        return None
    return float(h[-2]["p"])

def price_over_snaps(league: str, key: str) -> Optional[Tuple[float, float]]:
    need = LEAGUE_CFG[league]["min_snaps"]
    h = STATE["history"].get(key) or []
    if len(h) < need:
        return None
    old_p = float(h[-need]["p"])
    now_p = float(h[-1]["p"])
    return old_p, now_p

def in_guardrails(p: float) -> bool:
    return MIN_PRICE <= p <= MAX_PRICE

def late_game_ok(ws: Optional[Dict[str, Any]]) -> bool:
    if not LIVE_LATE_GAME_ONLY:
        return True
    if not ws:
        return False
    period = str(ws.get("period") or "")
    return period in ("2H", "4Q", "OT", "FT OT")

def cooldown_ok(league: str, key: str, is_live: bool) -> bool:
    now = now_ts()
    last = int(STATE["cooldowns"].get(key, 0))
    cd = LEAGUE_CFG[league]["live_cooldown"] if is_live else LEAGUE_CFG[league]["pregame_cooldown"]
    if now - last < cd:
        return False
    STATE["cooldowns"][key] = now
    return True

def confidence_score(league: str, move: float, liquidity: float, spread_c: Optional[float], is_live: bool) -> float:
    # 1-10 score based on: move magnitude, liquidity, spread tightness, live/pregame
    base = 4.5
    base += min(3.0, abs(move) * 50.0)  # 0.05 -> +2.5
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

def kelly_sizing(conf: float) -> float:
    # Practical sizing: converts confidence to % bankroll
    # Conf 6 => ~1%, Conf 8 => ~2.5%, Conf 9.5 => ~3.5%
    x = max(0.0, conf - 5.5)
    pct_bankroll = min(0.04, (x / 4.0) * 0.035)  # cap 4%
    return pct_bankroll

def maybe_open_position(key: str, entry_price: float) -> None:
    if key in STATE["positions"]:
        return
    STATE["positions"][key] = {
        "entry": float(entry_price),
        "opened": now_ts(),
        "peak": float(entry_price),
    }

def update_peak(key: str, price_now: float) -> None:
    p = STATE["positions"].get(key)
    if not p:
        return
    peak = float(p.get("peak", price_now))
    if price_now > peak:
        p["peak"] = float(price_now)

def close_position(key: str) -> None:
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
        return ("TP2", f"TP2 hit (+{pnl_c:.1f}c). Consider scaling out heavy.")
    if pnl_c >= TP1_CENTS:
        return ("TP1", f"TP1 hit (+{pnl_c:.1f}c). Consider scaling out partial.")
    if pnl_c <= -SL_CENTS:
        return ("SL", f"Stop hit ({pnl_c:.1f}c). Consider cutting risk.")

    # Trailing stop after trail start
    gain_from_entry_c = cents_diff(peak, entry)
    if gain_from_entry_c >= TRAIL_START_CENTS:
        trail_floor = peak - (TRAIL_GAP_CENTS / 100.0)
        if price_now <= trail_floor:
            return ("TRAIL", "Trailing stop hit. Protect profits.")

    age_min = (now_ts() - opened) / 60.0
    if age_min >= TIME_STOP_MIN and pnl_c < (TP1_CENTS * 0.5):
        return ("TIME", f"Time stop ({age_min:.0f}m). Capital efficiency exit.")

    return None

# =========================
# Winner inference + grading
# =========================
def infer_winner_from_ws(ws: Dict[str, Any]) -> Optional[str]:
    # WS provides score as "away-home" (example shows "3-16" with awayTeam/homeTeam)
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

def log_alert(kind: str, league: str, slug: str, team: str, price: float, conf: float, is_live: bool, reason: str) -> None:
    STATE["alerts_log"].append(
        {
            "ts": now_ts(),
            "kind": kind,  # "ENTRY" or "PREGAME_SCOUT" or "UPDATE"
            "league": league,
            "slug": slug,
            "team": team,
            "price": float(price),
            "conf": float(conf),
            "is_live": bool(is_live),
            "reason": reason,
        }
    )
    # Keep log bounded
    if len(STATE["alerts_log"]) > 2000:
        STATE["alerts_log"] = STATE["alerts_log"][-1500:]

# =========================
# Message builders
# =========================
def entry_message(kind: str, league: str, slug: str, team: str, price: float, move: float, liquidity: float, spread_c: Optional[float], ws: Optional[Dict[str, Any]], conf: float) -> str:
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
        frac = kelly_sizing(conf)
        stake = max(0.0, BANKROLL_USD * frac)
        sizing_txt = f"\n💵 Sizing: ~{frac*100:.1f}% bankroll ≈ ${stake:.2f} (set by BANKROLL_USD)"

    ctx = ""
    if is_live and (period or score):
        ctx = f"\n⏱️ {period} {elapsed} | 🧾 {score}".strip()

    return (
        f"{prefix_ping()}🚨 {BOT_NAME} {league} {status} {kind}\n"
        f"🏀 {team}  💰 {price:.2f}\n"
        f"{move_emoji} Move: {move_c:+.1f}c | 💧Liq: {int(liquidity):,} | ↔️ Spread: {spread_txt}\n"
        f"🧠 Confidence: {conf:.1f}/10"
        f"{sizing_txt}"
        f"{ctx}\n"
        f"🔗 {market_link(slug)}"
    )

def update_message(league: str, slug: str, team: str, entry: float, nowp: float, action: str, reason: str) -> str:
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
# Pregame scout logic
# =========================
def is_opening_window(key: str, league: str) -> bool:
    fs = STATE["first_seen"].get(key)
    if not fs:
        return True
    minutes = (now_ts() - int(fs["ts"])) / 60.0
    return minutes <= float(LEAGUE_CFG[league]["opening_scout_minutes"])

def record_first_seen(key: str, price: float) -> None:
    if key in STATE["first_seen"]:
        return
    STATE["first_seen"][key] = {"ts": now_ts(), "p": float(price)}

def opening_scout_signal(league: str, key: str, price_now: float) -> Optional[Tuple[str, float]]:
    """
    "Opening scalp" idea:
    If a market just appeared (opening window) and price moved quickly from first_seen,
    it can indicate early imbalance.
    """
    fs = STATE["first_seen"].get(key)
    if not fs:
        return None
    p0 = float(fs["p"])
    move = price_now - p0
    # require meaningful move during opening window
    thresh = float(LEAGUE_CFG[league]["pregame_big_move"])  # 0.05-0.06 type
    if abs(move) >= thresh and is_opening_window(key, league):
        return ("OPENING_SCALP", move)
    return None

def pregame_big_move_signal(league: str, key: str) -> Optional[float]:
    """
    Pregame "scan ahead":
    If price has moved a lot over snaps window before game is live, flag it.
    """
    pair = price_over_snaps(league, key)
    if not pair:
        return None
    old_p, now_p = pair
    move = now_p - old_p
    if abs(move) >= float(LEAGUE_CFG[league]["pregame_big_move"]):
        return move
    return None

# =========================
# Daily recap
# =========================
def local_day_str() -> str:
    # uses server local time. Railway is often UTC. That is fine for a daily recap.
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

    # Build recap from alerts_log and results
    since_ts = now_ts() - 24 * 3600
    alerts = [a for a in STATE["alerts_log"] if int(a["ts"]) >= since_ts and a.get("kind") in ("ENTRY", "PREGAME_SCOUT")]
    if not alerts:
        notify(f"📊 {BOT_NAME} Daily Recap\nNo alerts in the last 24h.")
        STATE["last_recap_day"] = day
        return

    # Grade only those slugs with results known
    wins = 0
    losses = 0
    graded = 0

    by_league = {"CBB": 0, "NBA": 0}
    for a in alerts:
        by_league[a["league"]] = by_league.get(a["league"], 0) + 1
        slug = a["slug"]
        res = STATE["results"].get(slug)
        if not res:
            continue
        winner = res.get("winner")
        if not winner:
            continue
        graded += 1
        if str(a["team"]) == str(winner):
            wins += 1
        else:
            losses += 1

    hit_rate = (wins / graded * 100.0) if graded > 0 else 0.0

    msg = (
        f"📊 {BOT_NAME} Daily Recap\n"
        f"Alerts: {len(alerts)} | CBB: {by_league.get('CBB', 0)} | NBA: {by_league.get('NBA', 0)}\n"
        f"Graded: {graded} | Wins: {wins} | Losses: {losses} | Hit Rate: {hit_rate:.1f}%\n"
        f"Notes: Grading only counts games where Polymarket WS provided a final result."
    )
    notify(msg)
    STATE["last_recap_day"] = day

# =========================
# Main loop
# =========================
def main():
    notify(f"🟣 {BOT_NAME} is ONLINE ✅ | Watching CBB + NBA")

    while True:
        try:
            # Heartbeat
            if HEARTBEAT_MIN > 0:
                if now_ts() - int(STATE.get("last_heartbeat_ts", 0)) >= HEARTBEAT_MIN * 60:
                    STATE["last_heartbeat_ts"] = now_ts()
                    notify(
                        f"🟣 {BOT_NAME} heartbeat ✅ | "
                        f"CBB slugs={len(STATE['last_slugs'].get('CBB', []))} | "
                        f"NBA slugs={len(STATE['last_slugs'].get('NBA', []))} | "
                        f"WS={WS_LAST_TS}"
                    )

            # Daily recap check
            run_daily_recap_if_time()

            # Discover slugs
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

            # Merge in WS-discovered slugs
            cbb_slugs = merge_ws_slugs(LEAGUE_CFG["CBB"]["slug_prefix"], cbb_slugs)
            nba_slugs = merge_ws_slugs(LEAGUE_CFG["NBA"]["slug_prefix"], nba_slugs)

            totals = {"CBB": 0, "NBA": 0}
            alerts_sent = 0
            updates_sent = 0

            for league, slugs in (("CBB", cbb_slugs), ("NBA", nba_slugs)):
                cfg = LEAGUE_CFG[league]
                for slug in slugs:
                    ws = WS_STATE.get(slug)
                    is_live = bool(ws and ws.get("live"))
                    ended = bool(ws and ws.get("ended"))

                    # Store results when ended
                    if ended and ws:
                        winner = infer_winner_from_ws(ws)
                        if winner:
                            STATE["results"][slug] = {"winner": winner, "ended_ts": now_ts()}

                    # Alert type filters
                    if is_live and not ENABLE_LIVE_ALERTS:
                        continue
                    if (not is_live) and not ENABLE_PREGAME_ALERTS:
                        continue

                    # Late game only filter (live)
                    if is_live and not late_game_ok(ws):
                        continue

                    market = fetch_market_by_slug(slug)
                    if not market:
                        continue

                    outcomes = extract_outcomes(market)
                    if not outcomes:
                        continue

                    for o in outcomes:
                        team = o["team"]
                        mid = float(o["mid"])
                        liquidity = float(o["liquidity"])
                        spread_c = o.get("spread_c")

                        if not in_guardrails(mid):
                            continue

                        # Liquidity + spread filters
                        if liquidity < float(cfg["min_liquidity"]):
                            continue
                        if spread_c is not None and float(spread_c) > float(cfg["max_spread_cents"]):
                            continue

                        totals[league] += 1
                        k = key_for(league, slug, team)

                        # first seen
                        record_first_seen(k, mid)

                        # history
                        push_history(k, mid)

                        # need previous
                        pprev = prev_price(k)
                        if pprev is None:
                            continue

                        # warmup
                        if hist_len(k) < int(cfg["min_snaps"]):
                            continue

                        # move over snaps window
                        pair = price_over_snaps(league, k)
                        if not pair:
                            continue
                        old_p, now_p = pair
                        move = now_p - old_p

                        # --- Pregame "scan ahead" ---
                        if not is_live and ENABLE_PREGAME_ALERTS:
                            # Opening scalp signal (from first_seen)
                            op = opening_scout_signal(league, k, mid)
                            if op:
                                kind, move_open = op
                                conf = confidence_score(league, move_open, liquidity, spread_c, is_live=False)
                                if cooldown_ok(league, k + "|PREGAME_OPEN", is_live=False):
                                    msg = entry_message(
                                        kind="PREGAME SCOUT (Opening)",
                                        league=league,
                                        slug=slug,
                                        team=team,
                                        price=mid,
                                        move=move_open,
                                        liquidity=liquidity,
                                        spread_c=spread_c,
                                        ws=None,
                                        conf=conf,
                                    )
                                    notify(msg)
                                    log_alert("PREGAME_SCOUT", league, slug, team, mid, conf, False, "opening-scout")
                                    alerts_sent += 1

                            # Big move over snaps window pregame
                            big_mv = pregame_big_move_signal(league, k)
                            if big_mv is not None:
                                conf = confidence_score(league, big_mv, liquidity, spread_c, is_live=False)
                                if cooldown_ok(league, k + "|PREGAME_BIG", is_live=False):
                                    msg = entry_message(
                                        kind="PREGAME SCOUT (Momentum)",
                                        league=league,
                                        slug=slug,
                                        team=team,
                                        price=mid,
                                        move=big_mv,
                                        liquidity=liquidity,
                                        spread_c=spread_c,
                                        ws=None,
                                        conf=conf,
                                    )
                                    notify(msg)
                                    log_alert("PREGAME_SCOUT", league, slug, team, mid, conf, False, "pregame-momentum")
                                    alerts_sent += 1

                        # --- Live/Pregame entry alerts (price displacement) ---
                        # Trigger if the latest move vs previous tick is large enough
                        tick_move = mid - pprev
                        if abs(tick_move) >= float(cfg["min_move"]):
                            conf = confidence_score(league, move, liquidity, spread_c, is_live=is_live)
                            alert_key = k + ("|LIVE" if is_live else "|PREGAME")
                            if cooldown_ok(league, alert_key, is_live=is_live):
                                kind = "LIVE BET" if is_live else "PREGAME BET"
                                msg = entry_message(
                                    kind=kind,
                                    league=league,
                                    slug=slug,
                                    team=team,
                                    price=mid,
                                    move=move,
                                    liquidity=liquidity,
                                    spread_c=spread_c,
                                    ws=ws if is_live else None,
                                    conf=conf,
                                )
                                notify(msg)
                                log_alert("ENTRY", league, slug, team, mid, conf, is_live, "displacement")
                                alerts_sent += 1
                                maybe_open_position(k, mid)

                        # --- Exit updates for open positions ---
                        if k in STATE["positions"]:
                            update_peak(k, mid)
                            sig = exit_signal(k, mid)
                            if sig:
                                action, reason = sig
                                entry = float(STATE["positions"][k]["entry"])
                                notify(update_message(league, slug, team, entry, mid, action, reason))
                                log_alert("UPDATE", league, slug, team, mid, 0.0, is_live, f"exit-{action}")
                                updates_sent += 1

                                # Close position on TP2/SL/TRAIL/TIME. Keep on TP1.
                                if action in ("TP2", "SL", "TRAIL", "TIME"):
                                    close_position(k)

            save_state(STATE)
            print(
                f"SCAN ok | CBB outcomes={totals['CBB']} NBA outcomes={totals['NBA']} "
                f"alerts={alerts_sent} updates={updates_sent} | WS={WS_LAST_TS}"
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