#!/usr/bin/env python3
"""
Polymarket Paper Trading Bot
────────────────────────────
Monitors 15-minute crypto Up/Down markets (BTC, ETH, SOL, XRP) via the
Polymarket CLOB WebSocket and executes a mean-reversion strategy on paper.

Strategy
--------
• Up markets  (YES = asset goes higher):
    - Buy YES when price < 0.40 during an uptrend in the YES price
    - Sell YES when price > 0.60
• Down markets (YES = asset goes lower) — inverse:
    - Buy YES when price > 0.60 during an uptrend in the YES price
      (fading an over-priced Down claim, expecting reversion)
    - Sell YES when price < 0.40

Circuit breaker: halt after 3 consecutive losing trades.
Position sizing: max 10 % of current wallet per trade.
All trades are paper-only — saved to data/paper_trades.json.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv()

# ── Market start-time filter ──────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")
_MONTHS = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
}

def _market_started(question: str) -> bool:
    """
    Parse the START time from a market title and return True only if that
    time has already passed.  Unrecognised titles are allowed through.

    Handles both formats:
      "Bitcoin Up or Down - March 9, 10:00PM-10:15PM ET"  → start 10:00 PM ET
      "Bitcoin Up or Down - March 9, 10PM ET"             → start 10:00 PM ET
    """
    # Try ranged format first: "March 9, 10:00PM-10:15PM ET"
    m = re.search(
        r"(\w+)\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
        question, re.I,
    )
    if m:
        month_s, day_s, hr_s, min_s, ampm = m.groups()
        hour, minute = int(hr_s), int(min_s)
    else:
        # Try single-hour format: "March 9, 10PM ET"
        m = re.search(
            r"(\w+)\s+(\d{1,2}),\s+(\d{1,2})\s*(AM|PM)",
            question, re.I,
        )
        if not m:
            return True  # can't parse → allow through
        month_s, day_s, hr_s, ampm = m.groups()
        hour, minute = int(hr_s), 0

    month = _MONTHS.get(month_s.lower())
    if not month:
        return True
    day = int(day_s)
    if ampm.upper() == "PM" and hour != 12:
        hour += 12
    elif ampm.upper() == "AM" and hour == 12:
        hour = 0

    year = datetime.now(timezone.utc).year
    try:
        start_et = datetime(year, month, day, hour, minute, tzinfo=_ET)
    except ValueError:
        return True
    return start_et.astimezone(timezone.utc) <= datetime.now(timezone.utc)

# ── Configuration ─────────────────────────────────────────────────────────────

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

CRYPTO_KEYWORDS = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "ripple"]
# Markets are titled "Bitcoin Up or Down - March 8, 7:00PM-7:15PM ET"
# A 15-minute window has exactly 15 minutes between start and end times.
WINDOW_KEYWORDS = ["15 min", "15min", "15 minute", "15-min", "up or down"]

# Mean-reversion thresholds (YES share price, 0–1 scale)
BUY_THRESHOLD_UP = 0.40    # Up market: buy YES below this
SELL_THRESHOLD_UP = 0.60   # Up market: sell YES above this
BUY_THRESHOLD_DOWN = 0.40  # Down market: buy when token is cheap (same logic as up)
SELL_THRESHOLD_DOWN = 0.60 # Down market: sell when token has recovered

INITIAL_WALLET = 100.0
MAX_POSITION_PCT = 0.10          # max 10 % of wallet per trade
MAX_POSITION_USD = 25.0          # hard cap per trade regardless of wallet size
MIN_HOLD_SECONDS = 60            # must hold a position at least this long before closing
STALE_POSITION_SECONDS = 1200    # force-close positions with no tick for 20 min (market expired)
CIRCUIT_BREAKER_LOSSES = 3       # halt after this many consecutive losses
TREND_WINDOW = 8                 # price ticks needed before trend is valid
MARKET_REFRESH_INTERVAL = 900    # re-scan Gamma API every 15 min (seconds)

DATA_FILE = "data/paper_trades.json"
DASHBOARD_DATA_FILE = "dashboard/data.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class Position:
    trade_id: str
    market_id: str
    question: str
    asset: str        # BTC / ETH / SOL / XRP
    side: str         # "up" or "down"
    entry_price: float
    size_usd: float
    shares: float
    token_id: str
    opened_at: str


# ── Market discovery ──────────────────────────────────────────────────────────

def _detect_asset(question: str) -> Optional[str]:
    q = question.lower()
    if "bitcoin" in q or "btc" in q:
        return "BTC"
    if "ethereum" in q or "eth" in q:
        return "ETH"
    if "solana" in q or "sol" in q:
        return "SOL"
    if "xrp" in q or "ripple" in q:
        return "XRP"
    return None


def _is_15min_window(question: str) -> bool:
    """
    Return True if the question describes a 15-minute time window.
    Matches patterns like "7:00PM-7:15PM" or "7:00AM-7:15AM".
    """
    m = re.search(r"(\d+):(\d+)\s*(AM|PM)\s*[-–]\s*(\d+):(\d+)\s*(AM|PM)", question, re.I)
    if not m:
        # Also accept questions that literally say "15 min" or similar
        return any(kw in question.lower() for kw in ["15 min", "15min", "15 minute", "15-min"])

    h1, mi1, p1, h2, mi2, p2 = m.groups()

    def to_mins(h: int, mi: int, period: str) -> int:
        h = int(h) % 12
        if period.upper() == "PM":
            h += 12
        return h * 60 + int(mi)

    t1 = to_mins(int(h1), int(mi1), p1)
    t2 = to_mins(int(h2), int(mi2), p2)
    diff = (t2 - t1) % (24 * 60)
    return diff == 15


def fetch_crypto_15m_markets() -> List[dict]:
    """
    Fetch active 15-minute crypto Up/Down markets from Gamma API.

    These are titled like "Bitcoin Up or Down - March 8, 7:00PM-7:15PM ET".
    Strategy: pull top-2000 markets by volume (20 fast requests) instead of
    paginating all 27,000+ markets on the platform.
    """
    log.info("Scanning Gamma API for 15-min crypto markets…")
    all_markets: List[dict] = []

    with httpx.Client(timeout=30) as client:
        # Fetch top 2000 by volume — Up/Down markets are actively traded
        for offset in range(0, 2000, 100):
            try:
                res = client.get(
                    f"{GAMMA_URL}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "archived": "false",
                        "order": "volume",
                        "ascending": "false",
                        "limit": 100,
                        "offset": offset,
                    },
                )
                res.raise_for_status()
            except Exception as exc:
                log.error(f"Gamma API error at offset {offset}: {exc}")
                break

            batch = res.json()
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break

    log.info(f"Fetched {len(all_markets)} markets total; filtering…")

    # Current UTC time for expiry filtering
    now_utc = datetime.now(timezone.utc)

    matched: List[dict] = []
    for m in all_markets:
        q = m.get("question", "")
        ql = q.lower()

        # Skip markets whose end date has passed (Gamma API sometimes returns
        # stale markets as "active" despite being closed months ago).
        end_date_str = m.get("endDateIso") or m.get("endDate")
        if end_date_str:
            try:
                # Normalize: ensure timezone-aware datetime
                end_dt_str = end_date_str.rstrip("Z")
                if "+" in end_dt_str or end_dt_str.endswith("Z"):
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                else:
                    end_dt = datetime.fromisoformat(end_dt_str).replace(tzinfo=timezone.utc)
                if end_dt < now_utc:
                    continue
            except Exception:
                pass  # if we can't parse the date, don't filter it out

        # Skip markets that haven't started yet (pre-market prices are garbage)
        if not _market_started(q):
            continue

        # Must be a crypto asset
        asset = _detect_asset(ql)
        if not asset:
            continue

        # Must be an "Up or Down" style market with a 15-min window
        if "up or down" not in ql:
            continue
        if not _is_15min_window(q):
            continue

        # "Up or Down" markets have YES = price goes UP
        # The two outcomes are "Up" and "Down"; YES token = Up
        side = "up"

        raw_ids = m.get("clobTokenIds", "[]")
        if isinstance(raw_ids, str):
            try:
                token_ids = json.loads(raw_ids)
            except Exception:
                continue
        else:
            token_ids = raw_ids or []

        if not token_ids:
            continue

        # YES token = "Up" outcome; NO token = "Down" outcome
        # Register both so we can apply inverse logic to the Down side
        matched.append(
            {
                "id": str(m.get("id", "")) + "_up",
                "question": q,
                "asset": asset,
                "side": "up",
                "token_id": token_ids[0],
            }
        )
        if len(token_ids) > 1:
            matched.append(
                {
                    "id": str(m.get("id", "")) + "_down",
                    "question": q + " [DOWN token]",
                    "asset": asset,
                    "side": "down",
                    "token_id": token_ids[1],
                }
            )

    log.info(f"Found {len(matched)} matching 15-min crypto Up/Down market(s)")
    for m in matched:
        log.info(f"  [{m['asset']}] {m['question']}")
    return matched


# ── Combined dashboard writer ─────────────────────────────────────────────────

def _write_combined_dashboard(current_state: dict):
    """Read all 3 bot state files and write combined dashboard/data.json."""
    files = {"15m": DATA_FILE, "5m": "data/paper_trades_5m.json", "1h": "data/paper_trades_1h.json"}
    bots = {}
    for key, path in files.items():
        if path == DATA_FILE:
            bots[key] = current_state
            continue
        try:
            with open(path) as f:
                bots[key] = json.load(f)
        except Exception:
            bots[key] = {"wallet": 100.0, "positions": [], "trades": [],
                         "stats": {}, "circuit_breaker": False}

    wins = losses = total_trades = open_pos = 0
    total_pnl = total_wallet = 0.0
    for d in bots.values():
        s = d.get("stats", {})
        wins         += s.get("wins",         0)
        losses       += s.get("losses",       0)
        total_pnl    += s.get("total_pnl",    0.0)
        total_trades += s.get("total_trades", 0)
        open_pos     += len(d.get("positions", []))
        total_wallet += d.get("wallet", 100.0)

    total = wins + losses
    payload = {
        **bots,
        "combined": {
            "total_pnl":      round(total_pnl, 4),
            "win_rate":       round(wins / total * 100, 1) if total > 0 else 0.0,
            "total_trades":   total_trades,
            "wins":           wins,
            "losses":         losses,
            "total_wallet":   round(total_wallet, 4),
            "initial_wallet": 300.0,
            "open_positions": open_pos,
        },
    }
    os.makedirs("dashboard", exist_ok=True)
    with open(DASHBOARD_DATA_FILE, "w") as f:
        json.dump(payload, f, indent=2)


# ── Persistent state ──────────────────────────────────────────────────────────

class TradingState:
    def __init__(self):
        self.wallet: float = INITIAL_WALLET
        self.positions: Dict[str, Position] = {}   # keyed by token_id
        self.trades: List[dict] = []
        self.consecutive_losses: int = 0
        self.circuit_breaker: bool = False
        self.price_history: Dict[str, List[float]] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self):
        os.makedirs("data", exist_ok=True)
        if not os.path.exists(DATA_FILE):
            self.save()
            return
        try:
            with open(DATA_FILE) as f:
                s = json.load(f)
            self.wallet = s.get("wallet", INITIAL_WALLET)
            self.trades = s.get("trades", [])
            self.consecutive_losses = s.get("consecutive_losses", 0)
            self.circuit_breaker = s.get("circuit_breaker", False)
            for p in s.get("positions", []):
                pos = Position(**p)
                self.positions[pos.token_id] = pos
            log.info(
                f"Loaded state: wallet=${self.wallet:.2f}, "
                f"{len(self.positions)} open position(s), "
                f"{len(self.trades)} trade record(s)"
            )
        except Exception as exc:
            log.warning(f"Could not load state ({exc}) — starting fresh")

    def save(self):
        state = {
            "wallet": round(self.wallet, 6),
            "positions": [asdict(p) for p in self.positions.values()],
            "trades": self.trades,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker": self.circuit_breaker,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "stats": self.compute_stats(),
        }
        with open(DATA_FILE, "w") as f:
            json.dump(state, f, indent=2)
        _write_combined_dashboard(state)

    # ── analytics ─────────────────────────────────────────────────────────────

    def compute_stats(self) -> dict:
        closed = [t for t in self.trades if t.get("pnl") is not None]
        wins = [t for t in closed if t["pnl"] > 0]
        return {
            "total_pnl": round(sum(t["pnl"] for t in closed), 4) if closed else 0.0,
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "initial_wallet": INITIAL_WALLET,
            "current_wallet": round(self.wallet, 4),
        }


# ── Mean-reversion strategy ───────────────────────────────────────────────────

class MeanReversionStrategy:
    """
    Trend is detected from the token's own YES-price history.

    Up market:   uptrend in YES price + price < 0.40  → open
                 price > 0.60                          → close
    Down market: uptrend in YES price + price > 0.60  → open (inverse)
                 price < 0.40                          → close (inverse)
    """

    def __init__(self, state: TradingState):
        self._state = state

    def _trend(self, token_id: str) -> Optional[str]:
        prices = self._state.price_history.get(token_id, [])
        if len(prices) < TREND_WINDOW:
            return None
        recent = prices[-TREND_WINDOW:]
        half = TREND_WINDOW // 2
        early = sum(recent[:half]) / half
        late = sum(recent[half:]) / (TREND_WINDOW - half)
        if late > early * 1.005:
            return "up"
        if late < early * 0.995:
            return "down"
        return "neutral"

    def signal(self, market: dict, price: float) -> Optional[str]:
        """Return 'open', 'close', or None."""
        if self._state.circuit_breaker:
            return None

        token_id = market["token_id"]
        side = market["side"]
        trend = self._trend(token_id)
        in_position = token_id in self._state.positions

        # Reject near-settled prices (settled tokens trade at ~0 or ~1)
        if price < 0.03 or price > 0.97:
            return None

        if not in_position:
            if side == "up" and trend == "up" and price < BUY_THRESHOLD_UP:
                return "open"
            if side == "down" and trend == "up" and price < BUY_THRESHOLD_DOWN:
                return "open"
        else:
            pos = self._state.positions.get(token_id)
            # Enforce minimum hold time to prevent same-burst open/close
            if pos:
                held = (datetime.now(timezone.utc) - datetime.fromisoformat(pos.opened_at)).total_seconds()
                if held < MIN_HOLD_SECONDS:
                    return None
            if side == "up" and price > SELL_THRESHOLD_UP:
                return "close"
            if side == "down" and price > SELL_THRESHOLD_DOWN:
                return "close"

        return None


# ── Paper-trade execution ─────────────────────────────────────────────────────

class PaperExecutor:
    def __init__(self, state: TradingState):
        self._state = state

    def open_position(self, market: dict, price: float):
        size = round(min(self._state.wallet * MAX_POSITION_PCT, MAX_POSITION_USD, self._state.wallet), 4)
        if size < 0.01:
            log.warning("Wallet too low to open a position")
            return

        shares = round(size / price, 6)
        trade_id = uuid.uuid4().hex[:8]

        pos = Position(
            trade_id=trade_id,
            market_id=market["id"],
            question=market["question"],
            asset=market["asset"],
            side=market["side"],
            entry_price=price,
            size_usd=size,
            shares=shares,
            token_id=market["token_id"],
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        self._state.positions[market["token_id"]] = pos
        self._state.wallet -= size

        self._state.trades.append(
            {
                "id": trade_id,
                "market_id": market["id"],
                "question": market["question"],
                "asset": market["asset"],
                "side": market["side"],
                "action": "open",
                "price": price,
                "size_usd": size,
                "shares": shares,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._state.save()

        log.info(
            f"[OPEN ] {market['asset']} {market['side'].upper()} "
            f"price={price:.4f}  size=${size:.2f}  shares={shares:.4f}  "
            f"wallet=${self._state.wallet:.2f}"
        )

    def close_position(self, market: dict, price: float):
        token_id = market["token_id"]
        pos = self._state.positions.get(token_id)
        if pos is None:
            return

        proceeds = round(pos.shares * price, 6)
        pnl = round(proceeds - pos.size_usd, 6)

        if pnl < 0:
            self._state.consecutive_losses += 1
            if self._state.consecutive_losses >= CIRCUIT_BREAKER_LOSSES:
                self._state.circuit_breaker = True
                log.warning(
                    f"⛔  CIRCUIT BREAKER TRIGGERED — "
                    f"{self._state.consecutive_losses} consecutive losses. "
                    "Edit data/paper_trades.json (set circuit_breaker=false) to resume."
                )
        else:
            self._state.consecutive_losses = 0

        self._state.wallet = round(self._state.wallet + proceeds, 6)
        del self._state.positions[token_id]

        pnl_tag = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
        self._state.trades.append(
            {
                "id": uuid.uuid4().hex[:8],
                "market_id": market["id"],
                "question": market["question"],
                "asset": market["asset"],
                "side": market["side"],
                "action": "close",
                "price": price,
                "size_usd": proceeds,
                "shares": pos.shares,
                "entry_price": pos.entry_price,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pnl": pnl,
            }
        )
        self._state.save()

        log.info(
            f"[CLOSE] {market['asset']} {market['side'].upper()} "
            f"price={price:.4f}  pnl={pnl_tag}  "
            f"wallet=${self._state.wallet:.2f}  streak={self._state.consecutive_losses}"
        )


# ── WebSocket feed handler ────────────────────────────────────────────────────

class PolymarketFeed:
    def __init__(self, state: TradingState):
        self._state = state
        self._strategy = MeanReversionStrategy(state)
        self._executor = PaperExecutor(state)
        self._market_map: Dict[str, dict] = {}
        self._last_refresh = 0.0
        self._tick_count = 0
        self._last_status_log = time.monotonic()
        self._last_tick_time: Dict[str, datetime] = {}
        self._settled_tokens: set = set()  # tokens that have settled — never re-enter

    # ── price routing ─────────────────────────────────────────────────────────

    def _record_price(self, token_id: str, price: float):
        history = self._state.price_history.setdefault(token_id, [])
        history.append(price)
        if len(history) > 200:
            del history[0]

    def _on_tick(self, tick: dict):
        # Handle price_changes batch format: {"market":..., "price_changes":[{asset_id, price},...]}
        if "price_changes" in tick:
            for change in tick.get("price_changes", []):
                self._on_tick(change)
            return

        asset_id = tick.get("asset_id")
        if not asset_id:
            return

        # Price preference: explicit field > last_trade_price > tight-spread mid
        # Avoid using bid/ask mid when the spread is very wide (illiquid market)
        price_raw = tick.get("price") or tick.get("mid_price")
        if price_raw is None:
            price_raw = tick.get("last_trade_price")
        if price_raw is None:
            bids = tick.get("bids", [])
            asks = tick.get("asks", [])
            if bids and asks:
                try:
                    bid_p = float(bids[0]["price"])
                    ask_p = float(asks[0]["price"])
                    if ask_p - bid_p <= 0.20:  # only use mid when spread is tight
                        price_raw = (bid_p + ask_p) / 2
                except (KeyError, ValueError, TypeError):
                    pass
        if price_raw is None:
            return

        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            return
        if price <= 0:
            return

        market = self._market_map.get(asset_id)
        if not market:
            return

        self._record_price(asset_id, price)
        self._last_tick_time[asset_id] = datetime.now(timezone.utc)
        self._tick_count += 1
        history_len = len(self._state.price_history.get(asset_id, []))
        log.debug(
            f"[TICK ] {market['asset']} {market['side'].upper()} "
            f"price={price:.4f}  ticks={history_len}"
        )
        # Periodic status log every 5 minutes
        now = time.monotonic()
        if now - self._last_status_log >= 300:
            self._last_status_log = now
            histories = {
                f"{self._market_map[tid]['asset']} {self._market_map[tid]['side']}": (
                    len(self._state.price_history.get(tid, [])),
                    round(self._state.price_history[tid][-1], 4) if self._state.price_history.get(tid) else None,
                )
                for tid in self._market_map
            }
            summary = "  ".join(
                f"{k}={v[1]}({v[0]}t)" for k, v in histories.items() if v[1] is not None
            )
            log.info(
                f"[STATUS] total_ticks={self._tick_count}  wallet=${self._state.wallet:.2f}  "
                f"positions={len(self._state.positions)}  {summary}"
            )
        token_id = market["token_id"]
        # Skip tokens that have already settled this session
        if token_id in self._settled_tokens:
            return
        sig = self._strategy.signal(market, price)
        if sig == "open":
            self._executor.open_position(market, price)
        elif sig == "close":
            self._executor.close_position(market, price)
            # If closed near resolution price, mark as settled so we don't re-enter
            if price >= 0.90 or price <= 0.10:
                self._settled_tokens.add(token_id)

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(data, list):
            for tick in data:
                self._on_tick(tick)
        elif isinstance(data, dict):
            self._on_tick(data)

    # ── market refresh ────────────────────────────────────────────────────────

    async def _refresh_markets(self) -> List[str]:
        markets = await asyncio.get_event_loop().run_in_executor(
            None, fetch_crypto_15m_markets
        )
        self._market_map = {m["token_id"]: m for m in markets}
        self._last_refresh = time.monotonic()
        return list(self._market_map)

    # ── WebSocket lifecycle ───────────────────────────────────────────────────

    async def run(self):
        # Loop forever — retry with backoff if no markets found
        no_market_backoff = 60
        while True:
            token_ids = await self._refresh_markets()
            if token_ids:
                break
            log.warning(f"No markets found. Retrying in {no_market_backoff}s…")
            await asyncio.sleep(no_market_backoff)
            no_market_backoff = min(no_market_backoff * 2, 900)

        backoff = 1
        while True:
            try:
                await self._ws_session(token_ids)
                backoff = 1
            except Exception as exc:
                log.error(f"WS error: {exc}. Reconnecting in {backoff}s…")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                if time.monotonic() - self._last_refresh >= MARKET_REFRESH_INTERVAL:
                    token_ids = await self._refresh_markets() or token_ids

    async def _ws_session(self, token_ids: List[str]):
        log.info(f"Connecting to {CLOB_WS_URL} …")
        async with websockets.connect(
            CLOB_WS_URL,
            ping_interval=20,
            ping_timeout=30,
        ) as ws:
            await ws.send(json.dumps({"assets_ids": token_ids, "type": "Market"}))
            log.info(f"Subscribed to {len(token_ids)} token(s)")

            refresh_task  = asyncio.create_task(self._periodic_refresh(ws))
            stale_task    = asyncio.create_task(self._stale_position_checker())
            try:
                async for raw in ws:
                    self._handle_message(raw)
            finally:
                refresh_task.cancel()
                stale_task.cancel()

    async def _periodic_refresh(self, ws):
        """Re-scan Gamma API every MARKET_REFRESH_INTERVAL and resubscribe."""
        while True:
            await asyncio.sleep(MARKET_REFRESH_INTERVAL)
            token_ids = await self._refresh_markets()
            if token_ids:
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "Market"}))
                log.info(f"Refreshed subscription: {len(token_ids)} market(s)")

    async def _stale_position_checker(self):
        """Periodically force-close positions whose markets have gone quiet (expired)."""
        while True:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            for token_id, pos in list(self._state.positions.items()):
                last_tick_times = self._last_tick_time
                last_tick = last_tick_times.get(token_id)
                if last_tick is None:
                    continue
                silence = (now - last_tick).total_seconds()
                if silence >= STALE_POSITION_SECONDS:
                    log.warning(
                        f"[STALE] {pos.asset} {pos.side.upper()} silent for "
                        f"{silence:.0f}s — force-closing at last price"
                    )
                    last_price = (
                        self._state.price_history.get(token_id, [0.5])[-1]
                    )
                    market = {
                        "id": pos.market_id,
                        "question": pos.question,
                        "asset": pos.asset,
                        "side": pos.side,
                        "token_id": token_id,
                    }
                    self._executor.close_position(market, last_price)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 62)
    log.info("  Polymarket Paper Trader  |  15-min Crypto Up/Down  ")
    log.info(f"  Strategy : Mean-Reversion  |  Starting wallet: ${INITIAL_WALLET}")
    log.info(f"  Up  thresholds : buy <{BUY_THRESHOLD_UP}  sell >{SELL_THRESHOLD_UP}")
    log.info(f"  Down thresholds: buy <{BUY_THRESHOLD_DOWN}  sell >{SELL_THRESHOLD_DOWN}")
    log.info(f"  Circuit breaker: {CIRCUIT_BREAKER_LOSSES} consecutive losses")
    log.info("=" * 62)

    state = TradingState()

    if state.circuit_breaker:
        log.warning(
            "Circuit breaker is ACTIVE from a previous session. "
            "Set circuit_breaker=false in data/paper_trades.json to resume."
        )
        return

    feed = PolymarketFeed(state)
    await feed.run()


if __name__ == "__main__":
    asyncio.run(main())
