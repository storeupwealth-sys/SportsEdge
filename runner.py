# runner.py
# Polymarket-only CBB scalping bot (live + upcoming)
# Uses:
#  - Slug discovery from polymarket.com/sports/cbb/games page (scrape)
#  - Market prices from Gamma API (bestAsk/bestBid or outcomePrices)
#  - Live game state from Polymarket Sports WebSocket (scores, clock, status)
# Sends alerts to Discord webhook (DISCORD_WEBHOOK). Telegram optional.

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

try:
  from websocket import WebSocketApp
except Exception:
  WebSocketApp = None  # type: ignore

# Your signals rules (paste the signals.py I gave you earlier into the repo root)
try:
  from signals import (
    MIN_SNAPS,
    confidence_from_context,
    exit_signal,
    mark_sent,
    should_alert,
  )
except Exception as e:
  raise RuntimeError(
    "signals.py is missing or not importable. Put signals.py in the same folder as runner.py."
  ) from e


# -----------------------------
# Config
# -----------------------------
LEAGUE = os.getenv("LEAGUE", "cbb")  # "cbb" per Polymarket sports WS docs
SCAN_SEC = int(os.getenv("SCAN_SEC", "60"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATE_PATH = os.getenv("STATE_PATH", "state.json")

# How many slugs to keep watching
MAX_SLUGS = int(os.getenv("MAX_SLUGS", "40"))

# If you only want LIVE, set ONLY_LIVE=1
ONLY_LIVE = os.getenv("ONLY_LIVE", "0").strip() == "1"

# If you want @everyone or not
PING_EVERYONE = os.getenv("PING_EVERYONE", "1").strip() == "1"

BOT_NAME = os.getenv("BOT_NAME", "God AI Predict Bot").strip()

USER_AGENT = os.getenv(
  "USER_AGENT",
  "Mozilla/5.0 (compatible; SportsEdgeBot/1.0; +https://polymarket.com)",
)

HEADERS = {"User-Agent": USER_AGENT}

# Polymarket endpoints
SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"
CBB_GAMES_PAGE = "https://polymarket.com/sports/cbb/games"
GAMMA_BASE = "https://gamma-api.polymarket.com"


# -----------------------------
# Helpers: state
# -----------------------------
def load_state() -> Dict[str, Any]:
  if not os.path.exists(STATE_PATH):
    return {
      "cooldowns": {},
      "history": {},          # key -> list of {"t": unix, "p": cents}
      "open_positions": {},   # key -> {"entry": cents, "t": unix}
      "last_slugs": [],
    }
  try:
    with open(STATE_PATH, "r", encoding="utf-8") as f:
      data = json.load(f)
    if not isinstance(data, dict):
      raise ValueError("state not dict")
    data.setdefault("cooldowns", {})
    data.setdefault("history", {})
    data.setdefault("open_positions", {})
    data.setdefault("last_slugs", [])
    return data
  except Exception:
    # If file got corrupted, start fresh
    return {
      "cooldowns": {},
      "history": {},
      "open_positions": {},
      "last_slugs": [],
    }


def save_state(state: Dict[str, Any]) -> None:
  tmp = STATE_PATH + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(state, f)
  os.replace(tmp, STATE_PATH)


# -----------------------------
# Helpers: notifications
# -----------------------------
def send_discord(content: str) -> None:
  if not DISCORD_WEBHOOK:
    print("DISCORD_WEBHOOK not set. Message would be:\n", content)
    return
  try:
    r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=HTTP_TIMEOUT)
    if r.status_code >= 300:
      print("Discord send failed:", r.status_code, r.text[:200])
  except Exception as e:
    print("Discord send error:", e)


def send_telegram(content: str) -> None:
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    return
  try:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": content, "disable_web_page_preview": True}
    r = requests.post(url, data=payload, timeout=HTTP_TIMEOUT)
    if r.status_code >= 300:
      print("Telegram send failed:", r.status_code, r.text[:200])
  except Exception as e:
    print("Telegram send error:", e)


def notify(content: str) -> None:
  send_discord(content)
  send_telegram(content)


# -----------------------------
# Slug discovery from page scrape
# -----------------------------
SLUG_RE = re.compile(r"\bmarket=([a-z0-9\-]+)\b", re.IGNORECASE)
CBB_SLUG_RE = re.compile(r"^cbb\-[a-z0-9\-]+$", re.IGNORECASE)

def scrape_cbb_slugs() -> List[str]:
  """
  Scrape polymarket.com/sports/cbb/games and extract game slugs from embed URLs.
  Falls back to any cbb-* tokens found in the HTML.
  """
  try:
    r = requests.get(CBB_GAMES_PAGE, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    html = r.text
  except Exception as e:
    print("Page scrape error:", e)
    return []

  slugs: List[str] = []

  # 1) Look for embed "market=" params
  for m in SLUG_RE.finditer(html):
    slug = m.group(1).strip()
    if CBB_SLUG_RE.match(slug):
      slugs.append(slug)

  # 2) Fallback: find raw cbb- tokens
  if not slugs:
    for m in re.finditer(r"\bcbb\-[a-z0-9\-]{10,}\b", html, flags=re.IGNORECASE):
      slugs.append(m.group(0))

  # de-dupe preserving order
  seen = set()
  out: List[str] = []
  for s in slugs:
    if s in seen:
      continue
    seen.add(s)
    out.append(s)

  return out[:MAX_SLUGS]


# -----------------------------
# Gamma API: market fetch by slug
# -----------------------------
def gamma_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
  """
  Fetch market object(s) for a slug.
  Returns a single market dict if found, else None.
  """
  # Primary: /markets?slug=
  url = f"{GAMMA_BASE}/markets"
  try:
    r = requests.get(url, params={"slug": slug}, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
  except Exception as e:
    print("Gamma fetch error:", slug, e)
    return None

  # Gamma sometimes returns a list
  if isinstance(data, list) and data:
    return data[0]
  if isinstance(data, dict) and data:
    return data
  return None


def extract_team_prices_cents(market: Dict[str, Any]) -> List[Tuple[str, float, float, float]]:
  """
  Returns list of (team_name, ask_cents, bid_cents, liquidity)
  Tries bestAsk/bestBid first; fallback to outcomePrices.
  """
  outcomes = market.get("outcomes") or []
  best_ask = market.get("bestAsk")
  best_bid = market.get("bestBid")
  outcome_prices = market.get("outcomePrices")

  liquidity = float(market.get("liquidity") or 0.0)
  volume = float(market.get("volume") or 0.0)

  # In case liquidity is missing, use volume as a weak proxy
  liq = liquidity if liquidity > 0 else volume

  results: List[Tuple[str, float, float, float]] = []

  if outcomes and best_ask and best_bid and isinstance(best_ask, list) and isinstance(best_bid, list):
    for i, name in enumerate(outcomes):
      try:
        a = float(best_ask[i]) * 100.0
        b = float(best_bid[i]) * 100.0
      except Exception:
        continue
      results.append((str(name), a, b, liq))
    return results

  if outcomes and outcome_prices and isinstance(outcome_prices, list):
    for i, name in enumerate(outcomes):
      try:
        mid = float(outcome_prices[i]) * 100.0
      except Exception:
        continue
      # If only mid is known, approximate ask/bid around it by 0.5c
      results.append((str(name), mid, mid, liq))
    return results

  return results


# -----------------------------
# Sports WS listener
# -----------------------------
class SportsWS:
  def __init__(self):
    self.latest_by_slug: Dict[str, Dict[str, Any]] = {}
    self.last_message_ts: Optional[int] = None
    self._ws: Optional[Any] = None

  def start(self):
    if WebSocketApp is None:
      print("websocket-client not installed. Add websocket-client to requirements.txt")
      return

    def on_open(ws):
      print("Connected to Polymarket WS")

    def on_message(ws, message: str):
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

      # We only care about sport_result messages, but docs show direct game state objects.
      slug = obj.get("slug")
      league = (obj.get("leagueAbbreviation") or "").lower()
      if not slug or league != LEAGUE:
        return

      self.latest_by_slug[str(slug)] = obj
      self.last_message_ts = int(time.time())

    def on_error(ws, error):
      print("WS error:", error)

    def on_close(ws, code, reason):
      print("WS closed:", code, reason)

    self._ws = WebSocketApp(
      SPORTS_WS_URL,
      on_open=on_open,
      on_message=on_message,
      on_error=on_error,
      on_close=on_close,
    )

    # Run WS in a background thread
    import threading
    t = threading.Thread(target=self._ws.run_forever, kwargs={"ping_interval": None}, daemon=True)
    t.start()

  def get(self, slug: str) -> Optional[Dict[str, Any]]:
    return self.latest_by_slug.get(slug)

  def last_message(self) -> Optional[int]:
    return self.last_message_ts


# -----------------------------
# Formatting
# -----------------------------
def fmt_alert_prefix() -> str:
  if PING_EVERYONE:
    return "@everyone "
  return ""

def fmt_money(cents: float) -> str:
  return f"{cents/100.0:.2f}"

def build_entry_message(
  slug: str,
  team: str,
  ask_cents: float,
  confidence: float,
  ws: Optional[Dict[str, Any]],
  move_cents: float,
  liquidity: float,
) -> str:
  live_flag = "LIVE" if (ws and ws.get("live")) else "UPCOMING"
  score = ws.get("score") if ws else None
  period = ws.get("period") if ws else None
  elapsed = ws.get("elapsed") if ws else None

  score_line = ""
  if score and period:
    score_line = f" | {score} {period}"
    if elapsed:
      score_line += f" {elapsed}"

  move_emoji = "📈" if move_cents > 0 else "📉"
  liq_txt = f"{int(liquidity):,}" if liquidity else "0"

  return (
    f"{fmt_alert_prefix()}🚨 {BOT_NAME} {live_flag} BET\n"
    f"🏀 {team}  💰 {ask_cents/100.0:.2f}\n"
    f"🧠 Confidence: {confidence:.1f}/10  {move_emoji} Move: {move_cents:.0f}¢  💧Liq: {liq_txt}\n"
    f"🔗 https://polymarket.com/market/{slug}"
    f"{score_line}"
  )

def build_exit_message(slug: str, team: str, entry: float, current: float, note: str) -> str:
  pnl = current - entry
  pnl_emoji = "✅" if pnl >= 0 else "⚠️"
  return (
    f"{fmt_alert_prefix()}{pnl_emoji} {BOT_NAME} SCALE UPDATE\n"
    f"🏀 {team}\n"
    f"Entry: {entry:.0f}¢  Now: {current:.0f}¢  PnL: {pnl:+.0f}¢\n"
    f"{note}\n"
    f"🔗 https://polymarket.com/market/{slug}"
  )


# -----------------------------
# History tracking
# -----------------------------
def hist_key(slug: str, team: str) -> str:
  return f"{slug}::{team}".lower()

def push_history(state: Dict[str, Any], key: str, price_cents: float, ts: int) -> None:
  h = state.setdefault("history", {}).setdefault(key, [])
  h.append({"t": ts, "p": price_cents})
  # keep last 200 points per key
  if len(h) > 200:
    del h[:-200]

def move_from_history(state: Dict[str, Any], key: str) -> Optional[float]:
  h = state.get("history", {}).get(key) or []
  if len(h) < MIN_SNAPS:
    return None
  # Compare current to the earliest point within the snap window
  recent = h[-MIN_SNAPS:]
  return float(recent[-1]["p"]) - float(recent[0]["p"])


# -----------------------------
# Main loop
# -----------------------------
def main():
  state = load_state()
  ws = SportsWS()
  ws.start()

  print("Starting Container")
  while True:
    scan_start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    try:
      slugs = scrape_cbb_slugs()
      if slugs:
        state["last_slugs"] = slugs
      else:
        slugs = state.get("last_slugs", [])

      print(f"SCAN START: {scan_start}")
      print(f"Found {len(slugs)} CBB slugs")
      print("WS last message:", ws.last_message())

      extracted_prices = 0
      alerted = 0
      exited = 0

      for slug in slugs:
        ws_obj = ws.get(slug)
        if ws_obj and ws_obj.get("ended"):
          continue

        is_live = bool(ws_obj and ws_obj.get("live"))
        if ONLY_LIVE and not is_live:
          continue

        market = gamma_market_by_slug(slug)
        if not market:
          continue

        team_prices = extract_team_prices_cents(market)
        if not team_prices:
          continue

        # Track + decide signals per team
        now_ts = int(time.time())
        for team, ask_cents, bid_cents, liquidity in team_prices:
          # Guard: ignore generic Yes/No futures markets
          low = team.strip().lower()
          if low in ("yes", "no"):
            continue

          extracted_prices += 1
          key = hist_key(slug, team)

          # use ask as "entry price" proxy
          push_history(state, key, ask_cents, now_ts)

          mv = move_from_history(state, key)
          if mv is None:
            continue

          conf = confidence_from_context(move_cents=mv, liquidity=liquidity)

          # ENTRY alerts
          if should_alert(state, key, mv, liquidity):
            msg = build_entry_message(
              slug=slug,
              team=team,
              ask_cents=ask_cents,
              confidence=conf,
              ws=ws_obj,
              move_cents=mv,
              liquidity=liquidity,
            )
            notify(msg)
            mark_sent(state, key)
            alerted += 1

            # Store an "open position" snapshot so we can send scale updates
            state.setdefault("open_positions", {})[key] = {"entry": ask_cents, "t": now_ts}

          # EXIT / SCALE logic (only if we have an entry stored)
          pos = state.get("open_positions", {}).get(key)
          if pos:
            entry = float(pos.get("entry", ask_cents))
            note = exit_signal(entry_cents=entry, current_cents=ask_cents)
            if note:
              msg = build_exit_message(slug, team, entry, ask_cents, note)
              notify(msg)
              exited += 1

              # If TP2 or SL triggered, close the position so it does not spam
              if ("TP2" in note) or ("cutting" in note.lower()) or ("cut risk" in note.lower()):
                try:
                  del state["open_positions"][key]
                except Exception:
                  pass

      save_state(state)

      scan_end = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
      print(f"Extracted team prices: {extracted_prices}")
      print(f"Alerts sent: {alerted} | Scale updates: {exited}")
      print(f"SCAN END  : {scan_end}")
      print(f"Sleeping {SCAN_SEC}s...")
      time.sleep(SCAN_SEC)

    except Exception as e:
      # Do not crash the worker. Log, sleep a bit, continue.
      print("Runner error:", repr(e))
      try:
        save_state(state)
      except Exception:
        pass
      time.sleep(10)


if __name__ == "__main__":
  main()
