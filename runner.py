import asyncio
import websockets
import json
import time
import os
import requests
from bs4 import BeautifulSoup

POLY_WS = "wss://sports-api.polymarket.com/ws"
POLY_GAMES_PAGE = "https://polymarket.com/sports/cbb/games"

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

CHECK_INTERVAL = 60


def send_discord(msg):
    if not DISCORD_WEBHOOK:
        print("No webhook set")
        return

    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg})
    except:
        pass


def scrape_slugs():
    try:
        r = requests.get(POLY_GAMES_PAGE, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        slugs = set()

        for a in soup.find_all("a"):
            href = a.get("href")
            if href and "/sports/cbb/" in href:
                slug = href.split("/")[-1]
                if "cbb-" in slug:
                    slugs.add(slug)

        return list(slugs)
    except:
        return []


async def websocket_listener(live_games):
    while True:
        try:
            async with websockets.connect(POLY_WS) as ws:
                print("Connected to Polymarket WS")

                while True:
                    msg = await ws.recv()

                    if msg == "ping":
                        await ws.send("pong")
                        continue

                    data = json.loads(msg)

                    if data.get("leagueAbbreviation") == "cbb":
                        slug = data.get("slug")
                        live = data.get("live")
                        score = data.get("score")
                        period = data.get("period")

                        if live and slug not in live_games:
                            live_games.add(slug)
                            message = f"🏀 LIVE CBB GAME\n{slug}\nScore: {score}\nPeriod: {period}"
                            print(message)
                            send_discord(message)

                        if not live and slug in live_games:
                            live_games.remove(slug)

        except Exception as e:
            print("WS Error:", e)
            await asyncio.sleep(5)


async def scanner():
    live_games = set()

    asyncio.create_task(websocket_listener(live_games))

    while True:
        slugs = scrape_slugs()
        print(f"Found {len(slugs)} CBB slugs")

        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(scanner())
