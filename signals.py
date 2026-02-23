# signals.py
import os
import time

# Guardrails
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.environ.get("MAX_PRICE", "0.95"))

# Risk management (cents)
TP_CENTS = float(os.environ.get("TP_CENTS", "3"))
SL_CENTS = float(os.environ.get("SL_CENTS", "2"))
TRAIL_START_CENTS = float(os.environ.get("TRAIL_START_CENTS", "4"))
TRAIL_GAP_CENTS = float(os.environ.get("TRAIL_GAP_CENTS", "2"))
TIME_STOP_MIN = float(os.environ.get("TIME_STOP_MIN", "20"))

_last_sent = {}

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except Exception:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except Exception:
        return default

def min_snaps(league: str) -> int:
    league = (league or "").upper()
    if league == "NBA":
        return _env_int("NBA_MIN_SNAPS", 10)
    return _env_int("CBB_MIN_SNAPS", 12)

def min_move(league: str) -> float:
    league = (league or "").upper()
    if league == "NBA":
        return _env_float("NBA_MIN_MOVE", 0.04)
    return _env_float("CBB_MIN_MOVE", 0.05)

def cooldown_sec(league: str, is_live: bool) -> int:
    league = (league or "").upper()
    if league == "NBA":
        return _env_int("NBA_LIVE_COOLDOWN_SEC", 480) if is_live else _env_int("NBA_PREGAME_COOLDOWN_SEC", 900)
    return _env_int("CBB_LIVE_COOLDOWN_SEC", 600) if is_live else _env_int("CBB_PREGAME_COOLDOWN_SEC", 900)

def can_send(key: str, league: str, is_live: bool) -> bool:
    now = int(time.time())
    last = _last_sent.get(key, 0)
    cd = cooldown_sec(league, is_live)
    if now - last < cd:
        return False
    _last_sent[key] = now
    return True

def should_alert(price_now: float, price_prev: float, league: str) -> bool:
    if price_now is None or price_prev is None:
        return False
    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return False
    move = price_now - price_prev
    return abs(move) >= min_move(league)

def confidence_from_context(price: float, period: str = "", score: str = "", is_live: bool = True, league: str = "CBB") -> float:
    base = 5.0
    if is_live:
        base += 1.0

    # Late game cheap prices are interesting
    if period in ("2H", "4Q") and price < 0.35:
        base += 2.0
    if price < 0.20:
        base += 1.0
    if price > 0.80:
        base -= 1.0

    # Slight bump for NBA liquidity/efficiency, keeps score realistic
    if (league or "").upper() == "NBA":
        base += 0.5

    return max(1.0, min(10.0, base))

def _cents(now_p: float, entry_p: float) -> float:
    return (now_p - entry_p) * 100.0

def exit_signal(*args, **kwargs):
    """
    Returns dict:
      {"exit": bool, "reason": str, "action": "HOLD|TAKE_PROFIT|STOP_LOSS|TRAIL_STOP|TIME_STOP"}
    Works with flexible runner signatures.
    """
    price_now = kwargs.get("price_now", kwargs.get("price", kwargs.get("current_price")))
    entry_price = kwargs.get("entry_price", kwargs.get("avg_price", kwargs.get("cost_basis")))
    max_price = kwargs.get("max_price_since_entry", kwargs.get("max_price", price_now))
    entry_ts = kwargs.get("entry_ts", kwargs.get("entry_time", kwargs.get("opened_at")))

    if price_now is None and len(args) >= 1:
        price_now = args[0]
    if entry_price is None and len(args) >= 2:
        entry_price = args[1]
    if max_price is None and len(args) >= 3:
        max_price = args[2]
    if entry_ts is None and len(args) >= 4:
        entry_ts = args[3]

    if price_now is None or entry_price is None:
        return {"exit": False, "reason": "missing price_now/entry_price", "action": "HOLD"}

    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return {"exit": False, "reason": "price out of guardrails", "action": "HOLD"}

    pnl_c = _cents(price_now, entry_price)

    if pnl_c >= TP_CENTS:
        return {"exit": True, "reason": f"TP hit (+{pnl_c:.1f}c)", "action": "TAKE_PROFIT"}

    if pnl_c <= -SL_CENTS:
        return {"exit": True, "reason": f"SL hit ({pnl_c:.1f}c)", "action": "STOP_LOSS"}

    if max_price is not None:
        gain_from_entry = _cents(max_price, entry_price)
        if gain_from_entry >= TRAIL_START_CENTS:
            trail_floor = max_price - (TRAIL_GAP_CENTS / 100.0)
            if price_now <= trail_floor:
                return {"exit": True, "reason": "Trail stop hit", "action": "TRAIL_STOP"}

    if entry_ts is not None:
        try:
            age_sec = int(time.time()) - int(entry_ts)
            if age_sec >= int(TIME_STOP_MIN * 60) and pnl_c < (TP_CENTS * 0.5):
                return {"exit": True, "reason": f"Time stop ({age_sec//60}m)", "action": "TIME_STOP"}
        except Exception:
            pass

    return {"exit": False, "reason": f"hold (pnl {pnl_c:.1f}c)", "action": "HOLD"}