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
