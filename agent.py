from __future__ import annotations

import os
import time
import json
import math
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# =========================
# ENV HELPERS
# =========================
def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v.strip())
    except Exception:
        return default


# =========================
# CONFIG
# =========================
CONFIG = {
    # Loop timing
    "SLEEP_BETWEEN_CYCLES_SEC": env_int("SLEEP_BETWEEN_CYCLES_SEC", 60),
    "HEARTBEAT_EVERY_CYCLES": env_int("HEARTBEAT_EVERY_CYCLES", 10),

    # Alert thresholds
    # Start lower (0.02) so it fires while testing, then raise to 0.04 to reduce noise
    "MIN_EDGE_TO_REPORT": env_float("MIN_EDGE_TO_REPORT", 0.02),

    # Deduping
    "DEDUP_TTL_SEC": env_int("DEDUP_TTL_SEC", 600),  # 10 minutes

    # Signal caps
    "MAX_SIGNALS_PER_VENUE": env_int("MAX_SIGNALS_PER_VENUE", 10),
    "MAX_SIGNALS_TOTAL": env_int("MAX_SIGNALS_TOTAL", 12),

    # Routing
    "SEND_TELEGRAM": env_bool("SEND_TELEGRAM", True),
    "SEND_DISCORD": env_bool("SEND_DISCORD", True),

    # Enable venues
    "ENABLE_KALSHI": env_bool("ENABLE_KALSHI", True),
    "ENABLE_POLYMARKET": env_bool("ENABLE_POLYMARKET", True),

    # Kalshi market focus
    # This is keyword scanning. Put "mention" or "trump" or "election" etc.
    "KALSHI_MARKET_QUERY": os.getenv("KALSHI_MARKET_QUERY", "mention").strip().lower(),
    "KALSHI_MARKETS_LIMIT": env_int("KALSHI_MARKETS_LIMIT", 80),

    # Kalshi endpoints
    "KALSHI_ENV": os.getenv("KALSHI_ENV", "prod"),
    "KALSHI_BASE_PROD": os.getenv("KALSHI_BASE_PROD", "https://api.elections.kalshi.com"),
    "KALSHI_BASE_DEMO": os.getenv("KALSHI_BASE_DEMO", "https://demo-api.kalshi.co"),
    "KALSHI_TRADE_PREFIX": os.getenv("KALSHI_TRADE_PREFIX", "/trade-api/v2"),

    # Polymarket endpoints
    "POLY_GAMMA_BASE": os.getenv("POLY_GAMMA_BASE", "https://gamma-api.polymarket.com"),
    "POLY_ACTIVE_ONLY": env_bool("POLY_ACTIVE_ONLY", True),
    "POLY_CLOSED": env_bool("POLY_CLOSED", False),
    "POLY_MARKETS_LIMIT": env_int("POLY_MARKETS_LIMIT", 120),

    # Polymarket sports filters
    # Keyword filters so it catches NBA and NCAAB without needing tag IDs
    "POLY_INCLUDE_KEYWORDS": os.getenv(
        "POLY_INCLUDE_KEYWORDS",
        "nba,ncaab,college basketball,ncaa basketball"
    ).strip().lower(),

    # Optional: block noisy stuff
    "POLY_EXCLUDE_KEYWORDS": os.getenv(
        "POLY_EXCLUDE_KEYWORDS",
        "wnba,mlb,nfl,nhl,soccer,tennis,golf,cricket"
    ).strip().lower(),

    # Debug tools
    "STARTUP_TEST_PING": env_bool("STARTUP_TEST_PING", True),
    "FORCE_PING_EVERY_CYCLES": env_int("FORCE_PING_EVERY_CYCLES", 0),  # 0 disables
}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")


# =========================
# MODELS
# =========================
@dataclass
class Signal:
    venue: str
    market_id: str
    title: str
    side: str
    price: float
    edge_hint: float
    confidence: float
    recommended_limit_price: float
    notes: str

    def fingerprint(self) -> str:
        # Rounded to avoid noise resends
        return "|".join([
            self.venue,
            self.market_id,
            self.side,
            f"{self.price:.3f}",
            f"{self.edge_hint:.3f}",
        ])


@dataclass
class Memory:
    sent_fingerprints: Dict[str, float] = field(default_factory=dict)
    cycle_count: int = 0

    def log(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


# =========================
# HTTP HELPERS
# =========================
def http_get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Any:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        txt = resp.read().decode("utf-8")
        return json.loads(txt) if txt else {"ok": True}


# =========================
# NOTIFICATIONS
# =========================
def telegram_send(bot_token: str, chat_id: str, text: str) -> Tuple[bool, str]:
    if not bot_token or not chat_id:
        return False, "missing telegram vars"
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        resp = http_post_json(url, payload)
        return bool(resp.get("ok")), "ok" if resp.get("ok") else "not ok"
    except Exception as e:
        return False, str(e)


def discord_send(webhook_url: str, text: str) -> Tuple[bool, str]:
    if not webhook_url:
        return False, "missing discord webhook"
    try:
        resp = http_post_json(webhook_url, {"content": text})
        return True, "ok"
    except Exception as e:
        return False, str(e)


# =========================
# UTILS
# =========================
def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def confidence(edge_hint: float, liquidity_hint: float) -> float:
    edge_hint = max(0.0, float(edge_hint))
    liquidity_hint = clamp01(liquidity_hint)
    c = (1.0 - math.exp(-7.0 * edge_hint)) * (0.35 + 0.65 * liquidity_hint)
    return clamp01(c)


def recommend_limit_price(current_price: float, edge_hint: float) -> float:
    # small price improvement
    current_price = clamp01(current_price)
    improve = min(0.02, max(0.005, edge_hint * 0.25))
    return round(clamp01(current_price - improve), 3)


def trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n\n(truncated)"


def parse_csv_keywords(s: str) -> List[str]:
    parts = [p.strip().lower() for p in (s or "").split(",")]
    return [p for p in parts if p]


def format_recap(signals: List[Signal], cycle: int) -> str:
    if not signals:
        return f"OpenClaw Recap (cycle {cycle}): No signals."

    lines: List[str] = []
    lines.append(f"OpenClaw Recap (cycle {cycle})")
    lines.append(f"Signals: {len(signals)}  Threshold: {CONFIG['MIN_EDGE_TO_REPORT']:.2f}")
    lines.append("")

    for i, s in enumerate(signals, start=1):
        lines.append(
            f"{i}) {s.venue} | {s.side} | Price {s.price:.3f} | EdgeHint {s.edge_hint:.3f} | "
            f"Conf {s.confidence:.2f} | Limit {s.recommended_limit_price:.3f}"
        )
        lines.append(f"   {s.title}")
        if s.notes:
            lines.append(f"   Notes: {s.notes}")

    return "\n".join(lines)


# =========================
# KALSHI (public)
# =========================
class KalshiClient:
    def __init__(self):
        base = CONFIG["KALSHI_BASE_DEMO"] if CONFIG["KALSHI_ENV"].lower() == "demo" else CONFIG["KALSHI_BASE_PROD"]
        self.base = base.rstrip("/")
        self.prefix = CONFIG["KALSHI_TRADE_PREFIX"].rstrip("/")

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base}{self.prefix}{path}"

    def get_markets(self, limit: int) -> Dict[str, Any]:
        url = self._url("/markets") + "?" + urllib.parse.urlencode({"limit": str(limit)})
        return http_get_json(url)

    def get_orderbook(self, ticker: str) -> Dict[str, Any]:
        url = self._url(f"/markets/{urllib.parse.quote(ticker)}/orderbook")
        return http_get_json(url)


def best_bid_price_cents(levels: Any) -> Optional[int]:
    if not levels:
        return None
    best: Optional[int] = None
    for lvl in levels:
        price = None
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 1:
            price = lvl[0]
        elif isinstance(lvl, dict):
            price = lvl.get("price")
        try:
            p = int(price)
        except Exception:
            continue
        if best is None or p > best:
            best = p
    return best


# =========================
# POLYMARKET (Gamma public)
# =========================
class PolymarketClient:
    def __init__(self):
        self.gamma = CONFIG["POLY_GAMMA_BASE"].rstrip("/")

    def get_markets(self, limit: int, offset: int) -> List[Dict[str, Any]]:
        params = {
            "limit": str(limit),
            "offset": str(offset),
            "active": "true" if CONFIG["POLY_ACTIVE_ONLY"] else "false",
            "closed": "true" if CONFIG["POLY_CLOSED"] else "false",
        }
        url = f"{self.gamma}/markets?" + urllib.parse.urlencode(params)
        return http_get_json(url)


# =========================
# AGENT
# =========================
class OpenClawAgent:
    def __init__(self):
        self.mem = Memory()
        self.kalshi = KalshiClient()
        self.poly = PolymarketClient()
        self.poly_include = parse_csv_keywords(CONFIG["POLY_INCLUDE_KEYWORDS"])
        self.poly_exclude = parse_csv_keywords(CONFIG["POLY_EXCLUDE_KEYWORDS"])

    def plan(self) -> List[str]:
        steps: List[str] = []
        if CONFIG["ENABLE_KALSHI"]:
            steps.append("scan_kalshi")
        if CONFIG["ENABLE_POLYMARKET"]:
            steps.append("scan_polymarket")
        steps.append("notify")
        return steps

    def cleanup_dedup_cache(self) -> None:
        now = time.time()
        ttl = CONFIG["DEDUP_TTL_SEC"]
        expired = [fp for fp, ts in self.mem.sent_fingerprints.items() if (now - ts) > ttl]
        for fp in expired:
            self.mem.sent_fingerprints.pop(fp, None)

    def should_send(self, signals: List[Signal]) -> List[Signal]:
        self.cleanup_dedup_cache()
        out: List[Signal] = []
        for s in signals:
            if s.fingerprint() in self.mem.sent_fingerprints:
                continue
            out.append(s)
        return out

    def send_recap(self, recap: str) -> None:
        # Always log recap to console
        print("\n" + recap + "\n", flush=True)

        if CONFIG["SEND_TELEGRAM"]:
            ok, msg = telegram_send(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, recap)
            self.mem.log(f"Telegram send ok={ok} detail={msg}")

        if CONFIG["SEND_DISCORD"]:
            ok, msg = discord_send(DISCORD_WEBHOOK_URL, recap)
            self.mem.log(f"Discord send ok={ok} detail={msg}")

    def notify(self, signals: List[Signal]) -> None:
        # Sort and cap
        signals.sort(key=lambda s: (s.edge_hint, s.confidence), reverse=True)
        signals = signals[: CONFIG["MAX_SIGNALS_TOTAL"]]

        to_send = self.should_send(signals)
        if not to_send:
            self.mem.log("Notify: nothing new to send.")
            return

        recap = format_recap(to_send, self.mem.cycle_count)
        recap = trim_text(recap, 3500)

        self.mem.log("Notify: sending recap.")
        self.send_recap(recap)

        now = time.time()
        for s in to_send:
            self.mem.sent_fingerprints[s.fingerprint()] = now

    def scan_kalshi(self) -> List[Signal]:
        out: List[Signal] = []
        q = CONFIG["KALSHI_MARKET_QUERY"]

        try:
            data = self.kalshi.get_markets(CONFIG["KALSHI_MARKETS_LIMIT"])
        except Exception as e:
            self.mem.log(f"Kalshi get_markets error: {e}")
            return out

        markets = data.get("markets") or data.get("data") or []
        self.mem.log(f"Kalshi markets fetched: {len(markets)}")

        for m in markets:
            title = str(m.get("title") or m.get("subtitle") or m.get("event_title") or "")
            ticker = str(m.get("ticker") or m.get("market_ticker") or "")
            if not ticker:
                continue

            hay = (title + " " + ticker).lower()
            if q and q not in hay:
                continue

            try:
                ob = self.kalshi.get_orderbook(ticker)
            except Exception:
                continue

            yes_levels = ob.get("orderbook", {}).get("yes") or ob.get("yes") or []
            no_levels = ob.get("orderbook", {}).get("no") or ob.get("no") or []

            best_yes = best_bid_price_cents(yes_levels)
            best_no = best_bid_price_cents(no_levels)
            if best_yes is None or best_no is None:
                continue

            total = best_yes + best_no
            gap = abs(100 - total) / 100.0
            if gap < CONFIG["MIN_EDGE_TO_REPORT"]:
                continue

            yes_price = best_yes / 100.0
            implied_yes_from_no = 1.0 - (best_no / 100.0)

            if yes_price < implied_yes_from_no:
                side = "BUY_YES"
                price = yes_price
            else:
                side = "BUY_NO"
                price = 1.0 - implied_yes_from_no

            conf = confidence(gap, liquidity_hint=0.55)
            limit_price = recommend_limit_price(price, gap)

            out.append(Signal(
                venue="KALSHI",
                market_id=ticker,
                title=title or ticker,
                side=side,
                price=clamp01(price),
                edge_hint=clamp01(gap),
                confidence=conf,
                recommended_limit_price=limit_price,
                notes=f"BestYesBid={best_yes}c BestNoBid={best_no}c Sum={total}c"
            ))

            if len(out) >= CONFIG["MAX_SIGNALS_PER_VENUE"]:
                break

        self.mem.log(f"Kalshi signals: {len(out)}")
        return out

    def poly_title_passes(self, title: str) -> bool:
        t = (title or "").lower()
        if not t:
            return False

        # Exclude first
        for bad in self.poly_exclude:
            if bad and bad in t:
                return False

        # Include if any include keyword is present
        for good in self.poly_include:
            if good and good in t:
                return True

        return False

    def scan_polymarket(self) -> List[Signal]:
        out: List[Signal] = []
        try:
            markets = self.poly.get_markets(CONFIG["POLY_MARKETS_LIMIT"], 0)
        except Exception as e:
            self.mem.log(f"Polymarket get_markets error: {e}")
            return out

        self.mem.log(f"Polymarket markets fetched: {len(markets)}")

        for m in markets:
            title = str(m.get("question") or m.get("title") or "")
            if not self.poly_title_passes(title):
                continue

            market_id = str(m.get("id") or m.get("conditionId") or "")
            enable_ob = bool(m.get("enableOrderBook", False))

            try:
                prices_raw = m.get("outcomePrices")
                outcomes_raw = m.get("outcomes")
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                if not prices or not outcomes or len(prices) < 2:
                    continue
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except Exception:
                continue

            imbalance = abs(yes_price - no_price)
            if imbalance < CONFIG["MIN_EDGE_TO_REPORT"]:
                continue

            # prefer orderbook enabled markets with higher liquidity hint
            liq = 0.70 if enable_ob else 0.35
            conf = confidence(imbalance, liquidity_hint=liq)

            if yes_price < no_price:
                side = "BUY_YES"
                price = yes_price
            else:
                side = "BUY_NO"
                price = no_price

            limit_price = recommend_limit_price(price, imbalance)

            out.append(Signal(
                venue="POLYMARKET",
                market_id=market_id,
                title=title or market_id,
                side=side,
                price=clamp01(price),
                edge_hint=clamp01(imbalance),
                confidence=conf,
                recommended_limit_price=limit_price,
                notes=f"Yes={yes_price:.3f} No={no_price:.3f} enableOrderBook={enable_ob}"
            ))

            if len(out) >= CONFIG["MAX_SIGNALS_PER_VENUE"]:
                break

        self.mem.log(f"Polymarket signals: {len(out)}")
        return out

    def startup_test(self) -> None:
        if not CONFIG["STARTUP_TEST_PING"]:
            return
        msg = "OpenClaw agent started. If you see this, Discord and Telegram wiring is good."
        self.mem.log("Startup test ping sending.")
        self.send_recap(msg)

    def run_forever(self) -> None:
        self.startup_test()

        while True:
            self.mem.cycle_count += 1
            cycle = self.mem.cycle_count

            if cycle == 1:
                self.mem.log("Agent running always on.")
                self.mem.log(f"Sleep={CONFIG['SLEEP_BETWEEN_CYCLES_SEC']}s MinEdge={CONFIG['MIN_EDGE_TO_REPORT']} DedupTTL={CONFIG['DEDUP_TTL_SEC']}s")
                self.mem.log(f"KalshiQuery='{CONFIG['KALSHI_MARKET_QUERY']}' PolyInclude='{CONFIG['POLY_INCLUDE_KEYWORDS']}'")

            steps = self.plan()
            self.mem.log(f"Cycle {cycle} plan: {', '.join(steps)}")

            signals: List[Signal] = []
            try:
                if "scan_kalshi" in steps:
                    signals.extend(self.scan_kalshi())
                if "scan_polymarket" in steps:
                    signals.extend(self.scan_polymarket())
                if "notify" in steps:
                    self.notify(signals)

                force_every = CONFIG["FORCE_PING_EVERY_CYCLES"]
                if force_every and (cycle % force_every == 0):
                    self.mem.log("Force ping enabled. Sending heartbeat recap.")
                    self.send_recap(f"Heartbeat: cycle {cycle}, signals found {len(signals)}")

                if cycle % CONFIG["HEARTBEAT_EVERY_CYCLES"] == 0:
                    self.mem.log(f"Heartbeat: cycle={cycle} dedup_cache={len(self.mem.sent_fingerprints)}")

            except Exception as e:
                self.mem.log(f"Top level cycle error: {e}")

            time.sleep(CONFIG["SLEEP_BETWEEN_CYCLES_SEC"])


if __name__ == "__main__":
    OpenClawAgent().run_forever()