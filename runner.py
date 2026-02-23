# runner.py

import os
import time
import json
import requests
from bs4 import BeautifulSoup
from websocket import WebSocketApp
from signals import (
    MIN_SNAPS,
    should_alert,
    can_send,
    confidence_from_context,
    exit_signal,
)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
SCAN_SEC = 60
STATE_FILE = "state.json"

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CBB_PAGE = "https://polymarket.com/sports/cbb/games"
WS_URL = "wss://sports-api.polymarket.com/ws"

state = {
    "history": {},
    "positions": {},
}

# ==============================
# UTIL
# ==============================

def notify(msg: str):
    if not DISCORD_WEBHOOK:
        print(msg)
        return
    requests.post(DISCORD_WEBHOOK, json={"content": msg})


def scrape_slugs():
    try:
        r = requests.get(CBB_PAGE, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        links = soup.find_all("a", href=True)

        slugs = []
        for l in links:
            href = l["href"]
            if "/market/cbb-" in href:
                slug = href.split("/market/")[1]
                slugs.append(slug)

        return list(set(slugs))[:40]
    except Exception:
        return []


def fetch_market(slug):
    try:
        r = requests.get(GAMMA_URL, params={"slug": slug}, timeout=15)
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except:
        return None


def get_prices(market):
    outcomes = market.get("outcomes", [])
    bestAsk = market.get("bestAsk", [])
    liquidity = float(market.get("liquidity") or 0)

    results = []
    for i, team in enumerate(outcomes):
        try:
            price = float(bestAsk[i])
            results.append((team, price, liquidity))
        except:
            continue
    return results


def push_history(key, price):
    arr = state["history"].setdefault(key, [])
    arr.append(price)
    if len(arr) > 50:
        arr.pop(0)


def get_move(key):
    arr = state["history"].get(key, [])
    if len(arr) < MIN_SNAPS:
        return None
    return arr[-1] - arr[-MIN_SNAPS]


# ==============================
# MAIN LOOP
# ==============================

def main():
    print("Bot Starting...")

    while True:
        slugs = scrape_slugs()
        print("Found", len(slugs), "CBB slugs")

        for slug in slugs:
            market = fetch_market(slug)
            if not market:
                continue

            prices = get_prices(market)

            for team, price, liquidity in prices:
                key = f"{slug}:{team}"

                push_history(key, price)
                move = get_move(key)

                if move is None:
                    continue

                if should_alert(price, price - move) and can_send(key):
                    conf = confidence_from_context(move, liquidity)

                    msg = (
                        f"🚨 CBB BET\n"
                        f"{team} @ {price:.2f}\n"
                        f"Move: {move:.2f}\n"
                        f"Confidence: {conf:.1f}/10\n"
                        f"https://polymarket.com/market/{slug}"
                    )

                    notify(msg)

                    state["positions"][key] = price

                # Exit logic
                if key in state["positions"]:
                    entry = state["positions"][key]
                    result = exit_signal(entry, price)

                    if result:
                        msg = (
                            f"📊 SCALE UPDATE\n"
                            f"{team}\n"
                            f"Entry: {entry:.2f}\n"
                            f"Now: {price:.2f}\n"
                            f"Result: {result}"
                        )
                        notify(msg)

                        if result in ("TP2", "STOP"):
                            del state["positions"][key]

        time.sleep(SCAN_SEC)


if __name__ == "__main__":
    main()
