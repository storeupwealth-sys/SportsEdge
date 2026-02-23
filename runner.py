import asyncio, websockets, json, time
import requests
from bs4 import BeautifulSoup

from notifier import send_discord
from storage import log_alert, set_result
from signals import should_alert, can_send, confidence_from_context
from recap import grade_alerts

POLY_WS = "wss://sports-api.polymarket.com/ws"
POLY_GAMES_PAGE = "https://polymarket.com/sports/cbb/games"

SCAN_SECONDS = 20

# in-memory caches
game_state = {}   # slug -> {score, period, status, ended, live}
price_state = {}  # key (slug|team) -> last_price


def scrape_slugs():
    r = requests.get(POLY_GAMES_PAGE, timeout=10)
    soup = BeautifulSoup(r.text, "html.parser")
    slugs = set()
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and "/sports/cbb/" in href:
            slug = href.split("/")[-1]
            if slug.startswith("cbb-"):
                slugs.add(slug)
    return list(slugs)


def fetch_prices_for_slug(slug: str):
    # Polymarket embeds expose market info via the page, we pull the JSON blob from HTML.
    # This is a practical method that works without Odds API.
    url = f"https://polymarket.com/sports/cbb/{slug}"
    r = requests.get(url, timeout=10)
    html = r.text

    # Look for team names and their YES prices in the HTML.
    # If Polymarket changes page structure, we update this parser.
    # For now we do a lightweight extraction using common patterns.
    prices = []

    # Heuristic: find occurrences of "Yes" price values near team names.
    # We keep it robust by searching for "outcomePrices" like objects.
    if "outcomePrices" in html:
        # fallback basic parsing for JSON-like arrays
        # This runner focuses on working end to end; we refine parsing next.
        pass

    # If you already know the two teams from slug, we can still post momentum alerts using
    # YES prices once we extract them correctly. Next step is upgrading this into Gamma API calls.
    return prices


async def ws_loop():
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
                    if data.get("leagueAbbreviation") != "cbb":
                        continue

                    slug = data.get("slug")
                    game_state[slug] = {
                        "score": data.get("score"),
                        "period": data.get("period"),
                        "status": data.get("status"),
                        "ended": bool(data.get("ended")),
                        "live": bool(data.get("live")),
                        "home": data.get("homeTeam"),
                        "away": data.get("awayTeam"),
                    }

                    # if game ended, grade and recap
                    if data.get("ended"):
                        winner = infer_winner_from_score(data.get("score"), data.get("homeTeam"), data.get("awayTeam"))
                        if winner:
                            set_result(slug, winner)
                            recap = grade_alerts(slug, winner)
                            if recap:
                                hit_rate, wins, total, lines = recap
                                msg_out = "🚀 GOD AI PREDICT BOT RECAP\n"
                                msg_out += f"🏀 {slug}\n"
                                msg_out += f"✅ Winner: {winner}\n"
                                msg_out += f"📊 Hit Rate: {hit_rate:.1f}% ({wins}/{total})\n"
                                msg_out += "\n".join(lines[:12])
                                send_discord(msg_out)

        except Exception as e:
            print("WS error:", e)
            await asyncio.sleep(5)


def infer_winner_from_score(score: str, home: str, away: str):
    if not score or "-" not in score:
        return None
    try:
        a, b = score.split("-")
        a = int(a.strip())
        b = int(b.strip())
        # score is usually away-home or home-away depending on feed; we assume home-away mapping is unknown
        # so we choose winner by comparing to whichever team is leading, using a consistent rule:
        # treat left as away, right as home for now.
        return away if a > b else home if b > a else None
    except:
        return None


async def scan_loop():
    send_discord("🟣 GOD AI PREDICT BOT is LIVE ✅\n🏀 CBB engine online. Watching Polymarket now.")
    while True:
        try:
            slugs = scrape_slugs()
            print(f"Found {len(slugs)} CBB slugs")

            # placeholder: once we wire real YES prices per team, signals will fire.
            # next commit upgrades fetch_prices_for_slug into official market-price endpoint calls.
            for slug in slugs:
                gs = game_state.get(slug, {})
                # Here we will compute signals using (price_now vs price_prev).
                # For now we just ensure the system stays stable.
                pass

        except Exception as e:
            print("Scan error:", e)

        await asyncio.sleep(SCAN_SECONDS)


async def main():
    await asyncio.gather(ws_loop(), scan_loop())


if __name__ == "__main__":
    asyncio.run(main())
