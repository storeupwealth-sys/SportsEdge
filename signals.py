# signals.py
import os
import time

# ----------------------------
# Core tuning knobs
# ----------------------------
MIN_SNAPS = int(os.environ.get("MIN_SNAPS", "20"))

# Movement required to alert (decimal). 0.04 = 4 cents move
MIN_MOVE = float(os.environ.get("MIN_MOVE", "0.04"))

# Safety range (ignore crazy prices)
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.environ.get("MAX_PRICE", "0.95"))

# Cooldowns
LIVE_ALERT_COOLDOWN_SEC = int(os.environ.get("LIVE_ALERT_COOLDOWN_SEC", os.environ.get("ALERT_COOLDOWN_SEC", "600")))
PREGAME_ALERT_COOLDOWN_SEC = int(os.environ.get("PREGAME_ALERT_COOLDOWN_SEC", os.environ.get("ALERT_COOLDOWN_SEC", "900")))

# ----------------------------
# Exit / risk knobs (cent-based)
# These are for "when should I sell"
# ----------------------------
TP_CENTS = float(os.environ.get("TP_CENTS", "3"))                 # take profit after +3c
SL_CENTS = float(os.environ.get("SL_CENTS", "2"))                 # stop after -2c
TRAIL_START_CENTS = float(os.environ.get("TRAIL_START_CENTS", "4"))  # start trailing after +4c
TRAIL_GAP_CENTS = float(os.environ.get("TRAIL_GAP_CENTS", "2"))      # trail by 2c
TIME_STOP_MIN = float(os.environ.get("TIME_STOP_MIN", "20"))      # exit after 20 min if not moving

# ----------------------------
# Internal state
# ----------------------------
_last_sent = {}

def _cooldown_for(is_live: bool) -> int:
    return LIVE_ALERT_COOLDOWN_SEC if is_live else PREGAME_ALERT_COOLDOWN_SEC

def can_send(key: str, is_live: bool = True) -> bool:
    """
    key should uniquely represent an outcome (ex: 'slug|TEAM' or 'market|Yes')
    """
    now = int(time.time())
    last = _last_sent.get(key, 0)
    cd = _cooldown_for(is_live)
    if now - last < cd:
        return False
    _last_sent[key] = now
    return True

def should_alert(price_now: float, price_prev: float) -> bool:
    """
    Fire alerts on meaningful movement, not noise.
    """
    if price_now is None or price_prev is None:
        return False
    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return False
    move = price_now - price_prev
    return abs(move) >= MIN_MOVE

def confidence_from_context(price: float, period: str = "", score: str = "", is_live: bool = True) -> float:
    """
    Simple heuristic confidence (1-10). Upgrade later.
    """
    base = 5.0
    if is_live:
        base += 1.0

    # late game + cheap price = more interesting for scalps
    if period in ("2H", "4Q") and price < 0.35:
        base += 2.0
    if price < 0.20:
        base += 1.0
    if price > 0.80:
        base -= 1.0

    return max(1.0, min(10.0, base))

def _cents(a: float, b: float) -> float:
    """Return difference in cents between two probability prices."""
    return (a - b) * 100.0

def exit_signal(*args, **kwargs):
    """
    Flexible exit logic so runner.py can call it without signature mismatch.

    Expected fields (any style):
      - price_now / price / current_price
      - entry_price / avg_price / cost_basis
      - max_price_since_entry (optional, for trailing)
      - entry_ts (optional, epoch seconds)
      - is_live (optional)

    Returns dict:
      {"exit": bool, "reason": str, "action": "HOLD|TAKE_PROFIT|STOP_LOSS|TRAIL_STOP|TIME_STOP"}
    """

    # pull from kwargs with aliases
    price_now = kwargs.get("price_now", kwargs.get("price", kwargs.get("current_price")))
    entry_price = kwargs.get("entry_price", kwargs.get("avg_price", kwargs.get("cost_basis")))
    max_price = kwargs.get("max_price_since_entry", kwargs.get("max_price", price_now))
    entry_ts = kwargs.get("entry_ts", kwargs.get("entry_time", kwargs.get("opened_at")))
    is_live = kwargs.get("is_live", True)

    # If runner passed positional args, try to map common pattern:
    # exit_signal(price_now, entry_price, max_price, entry_ts)
    if price_now is None and len(args) >= 1:
        price_now = args[0]
    if entry_price is None and len(args) >= 2:
        entry_price = args[1]
    if max_price is None and len(args) >= 3:
        max_price = args[2]
    if entry_ts is None and len(args) >= 4:
        entry_ts = args[3]

    # If we still don't have what we need, just don't exit
    if price_now is None or entry_price is None:
        return {"exit": False, "reason": "missing price_now/entry_price", "action": "HOLD"}

    # Guardrails
    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return {"exit": False, "reason": "price out of guardrails", "action": "HOLD"}

    pnl_cents = _cents(price_now, entry_price)

    # 1) Take profit
    if pnl_cents >= TP_CENTS:
        return {"exit": True, "reason": f"TP hit (+{pnl_cents:.1f}c)", "action": "TAKE_PROFIT"}

    # 2) Stop loss
    if pnl_cents <= -SL_CENTS:
        return {"exit": True, "reason": f"SL hit ({pnl_cents:.1f}c)", "action": "STOP_LOSS"}

    # 3) Trailing stop (only after trail start)
    # Track max_price externally if you can; this supports it if runner provides it
    if max_price is not None:
        gain_from_entry_cents = _cents(max_price, entry_price)
        if gain_from_entry_cents >= TRAIL_START_CENTS:
            trail_floor = max_price - (TRAIL_GAP_CENTS / 100.0)
            if price_now <= trail_floor:
                return {"exit": True, "reason": f"Trail stop (max {max_price:.3f}, now {price_now:.3f})", "action": "TRAIL_STOP"}

    # 4) Time stop (if stuck)
    if entry_ts is not None:
        try:
            age_sec = int(time.time()) - int(entry_ts)
            if age_sec >= int(TIME_STOP_MIN * 60):
                # only time-stop if not meaningfully green
                if pnl_cents < (TP_CENTS * 0.5):
                    return {"exit": True, "reason": f"Time stop ({age_sec//60}m, pnl {pnl_cents:.1f}c)", "action": "TIME_STOP"}
        except Exception:
            pass

    return {"exit": False, "reason": f"hold (pnl {pnl_cents:.1f}c)", "action": "HOLD"}
