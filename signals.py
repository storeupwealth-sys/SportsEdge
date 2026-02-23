# signals.py
import os
import time

# How many price snapshots required before scoring
MIN_SNAPS = int(os.environ.get("MIN_SNAPS", "20"))

# Minimum move in price (cents expressed as decimal) to trigger (ex: 0.04 = 4c)
MIN_MOVE = float(os.environ.get("MIN_MOVE", "0.04"))

# Guardrails to avoid garbage prices
MIN_PRICE = float(os.environ.get("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.environ.get("MAX_PRICE", "0.95"))

# Cooldowns (seconds) - separate for LIVE vs PREGAME
LIVE_ALERT_COOLDOWN_SEC = int(os.environ.get("LIVE_ALERT_COOLDOWN_SEC", os.environ.get("ALERT_COOLDOWN_SEC", "600")))
PREGAME_ALERT_COOLDOWN_SEC = int(os.environ.get("PREGAME_ALERT_COOLDOWN_SEC", os.environ.get("ALERT_COOLDOWN_SEC", "900")))

_last_sent = {}

def _cooldown_for(is_live: bool) -> int:
    return LIVE_ALERT_COOLDOWN_SEC if is_live else PREGAME_ALERT_COOLDOWN_SEC

def can_send(key: str, is_live: bool) -> bool:
    """
    key should uniquely represent a team outcome (ex: 'cbb-ecar-charlt-2026-02-21|ECU')
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
    Trigger only when we get meaningful movement.
    """
    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return False
    move = price_now - price_prev
    return abs(move) >= MIN_MOVE

def confidence_from_context(price: float, period: str, score: str, is_live: bool) -> float:
    """
    Simple confidence heuristic. You can upgrade later.
    """
    base = 5.0

    if is_live:
        base += 1.0

    if period in ("2H", "4Q") and price < 0.35:
        base += 2.0
    if price < 0.20:
        base += 1.0
    if price > 0.80:
        base -= 1.0

    return max(1.0, min(10.0, base))
