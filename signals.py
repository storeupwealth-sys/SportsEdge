# signals.py

import os
import time

# ==============================
# CONFIG (Adjust in Railway vars)
# ==============================

MIN_SNAPS = int(os.environ.get("MIN_SNAPS", "8"))  # lower = faster alerts
MIN_MOVE = float(os.environ.get("MIN_MOVE", "0.04"))  # 4¢ move
ALERT_COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", "300"))

MIN_PRICE = 0.05
MAX_PRICE = 0.95

_last_sent = {}

# ==============================
# ALERT CONTROL
# ==============================

def can_send(key: str) -> bool:
    now = int(time.time())
    last = _last_sent.get(key, 0)
    if now - last < ALERT_COOLDOWN_SEC:
        return False
    _last_sent[key] = now
    return True


def should_alert(price_now: float, price_prev: float) -> bool:
    if price_now < MIN_PRICE or price_now > MAX_PRICE:
        return False
    move = price_now - price_prev
    return abs(move) >= MIN_MOVE


def confidence_from_context(move: float, liquidity: float) -> float:
    base = 5.0

    # Larger move = more conviction
    base += min(3.0, abs(move) * 50)

    # More liquidity = stronger signal
    if liquidity > 10000:
        base += 1.5
    elif liquidity > 5000:
        base += 1.0

    return max(1.0, min(10.0, base))


def exit_signal(entry: float, current: float):
    pnl = current - entry

    if pnl >= 0.06:
        return "TP2"
    if pnl >= 0.03:
        return "TP1"
    if pnl <= -0.03:
        return "STOP"

    return None
