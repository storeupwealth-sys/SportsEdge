# signals.py

# How many price snapshots we require before we start scoring signals
MIN_SNAPS = int(__import__("os").environ.get("MIN_SNAPS", "20"))

# Safety: minimum edge to trigger an alert (ex: 0.03 = 3%)
MIN_EDGE = float(__import__("os").environ.get("MIN_EDGE", "0.03"))

# How long to wait before alerting the same game again (seconds)
ALERT_COOLDOWN_SEC = int(__import__("os").environ.get("ALERT_COOLDOWN_SEC", "900"))
import time

COOLDOWN_SECONDS = 120
MIN_PRICE = 0.05
MAX_PRICE = 0.95

_last_sent = {}

def can_send(key: str) -> bool:
    now = int(time.time())
    last = _last_sent.get(key, 0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_sent[key] = now
    return True

def confidence_from_context(price: float, period: str, score: str) -> float:
    base = 5.0
    if period in ("2H", "4Q") and price < 0.35:
        base += 2.0
    if price < 0.20:
        base += 1.0
    if price > 0.80:
        base -= 1.0
    return max(1.0, min(10.0, base))

def should_alert(price_now: float, price_prev: float) -> bool:
    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return False
    move = price_now - price_prev
    return abs(move) >= 0.04
