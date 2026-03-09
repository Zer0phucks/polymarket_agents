#!/usr/bin/env python3
"""
Polymarket Paper Trading Bot — 1-hour Crypto Markets
──────────────────────────────────────────────────────
Targets hourly "Up or Down" crypto markets titled like
"Bitcoin Up or Down - March 8, 11PM ET".

Each bot has its own isolated wallet (starts at $100).

Strategy: identical mean-reversion to the 5m/15m bots.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
import websockets
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

GAMMA_URL    = "https://gamma-api.polymarket.com"
CLOB_WS_URL  = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

BUY_THRESHOLD_UP    = 0.40
SELL_THRESHOLD_UP   = 0.60
BUY_THRESHOLD_DOWN  = 0.40
SELL_THRESHOLD_DOWN = 0.60

INITIAL_WALLET          = 100.0
MAX_POSITION_PCT        = 0.10
MAX_POSITION_USD        = 25.0
MIN_HOLD_SECONDS        = 300     # 5 min minimum hold in a 1h market
STALE_POSITION_SECONDS  = 5400    # 90 min silence → market resolved
CIRCUIT_BREAKER_LOSSES  = 3
TREND_WINDOW            = 10
MARKET_REFRESH_INTERVAL = 1800    # re-scan every 30 min

DATA_FILE = "data/paper_trades_1h.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [1h] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class Position:
    trade_id: str
    market_id: str
    question: str
    asset: str
    side: str
    entry_price: float
    size_usd: float
    shares: float
    token_id: str
    opened_at: str


# ── Market discovery ──────────────────────────────────────────────────────────

def _detect_asset(question: str) -> Optional[str]:
    q = question.lower()
    if "bitcoin" in q or "btc" in q:   return "BTC"
    if "ethereum" in q or "eth" in q:  return "ETH"
    if "solana" in q or "sol" in q:    return "SOL"
    if "xrp" in q or "ripple" in q:   return "XRP"
    return None


def _is_1h_market(question: str) -> bool:
    """
    Match hourly markets: "Bitcoin Up or Down - March 8, 11PM ET"
    Exclude ranged markets: "Bitcoin Up or Down - March 8, 7:00PM-7:15PM ET"
    """
    import re
    # If there's a time range (HH:MM AM/PM - HH:MM AM/PM), it's a 5/15-min market
    if re.search(r"\d+:\d+\s*(?:AM|PM)\s*[-–]", question, re.I):
        return False
    # Single hour stamp at end: "11PM ET", "2AM ET", "12PM ET"
    return bool(re.search(r"\b\d{1,2}\s*(?:AM|PM)\s+ET\b", question, re.I))


def fetch_crypto_1h_markets() -> List[dict]:
    """Scan top-2000 markets by volume for hourly crypto Up/Down markets."""
    log.info("Scanning Gamma API for 1h crypto markets…")
    all_markets: List[dict] = []
    now_utc = datetime.now(timezone.utc)

    with httpx.Client(timeout=30) as client:
        for offset in range(0, 2000, 100):
            try:
                res = client.get(
                    f"{GAMMA_URL}/markets",
                    params={
                        "active": "true", "closed": "false", "archived": "false",
                        "order": "volume", "ascending": "false",
                        "limit": 100, "offset": offset,
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

    log.info(f"Fetched {len(all_markets)} markets total; filtering for 1h windows…")

    matched: List[dict] = []
    for m in all_markets:
        q = m.get("question", "")
        ql = q.lower()

        # Skip expired markets
        end_date_str = m.get("endDateIso") or m.get("endDate")
        if end_date_str:
            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if end_dt < now_utc:
                    continue
            except Exception:
                pass

        asset = _detect_asset(ql)
        if not asset:
            continue
        if "up or down" not in ql:
            continue
        if not _is_1h_market(q):
            continue

        raw_ids = m.get("clobTokenIds", "[]")
        token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else (raw_ids or [])
        if not token_ids:
            continue

        matched.append({"id": str(m.get("id","")) + "_up",   "question": q,
                         "asset": asset, "side": "up",   "token_id": token_ids[0]})
        if len(token_ids) > 1:
            matched.append({"id": str(m.get("id","")) + "_down",
                             "question": q + " [DOWN token]",
                             "asset": asset, "side": "down", "token_id": token_ids[1]})

    log.info(f"Found {len(matched)} matching 1h crypto Up/Down market(s)")
    for m in matched:
        log.info(f"  [{m['asset']}] {m['question']}")
    return matched


# ── Combined dashboard writer ─────────────────────────────────────────────────

def _write_combined_dashboard():
    files = {
        "15m": "data/paper_trades.json",
        "5m":  "data/paper_trades_5m.json",
        "1h":  "data/paper_trades_1h.json",
    }
    bots = {}
    for key, path in files.items():
        try:
            with open(path) as f:
                bots[key] = json.load(f)
        except Exception:
            bots[key] = {"wallet": 100.0, "positions": [], "trades": [],
                         "stats": {}, "circuit_breaker": False}

    wins = losses = total_pnl = 0
    total_trades = open_pos = 0
    total_wallet = 0.0
    for d in bots.values():
        s = d.get("stats", {})
        wins        += s.get("wins",   0)
        losses      += s.get("losses", 0)
        total_pnl   += s.get("total_pnl", 0.0)
        total_trades += s.get("total_trades", 0)
        open_pos    += len(d.get("positions", []))
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
    with open("dashboard/data.json", "w") as f:
        json.dump(payload, f, indent=2)


# ── Persistent state ──────────────────────────────────────────────────────────

class TradingState:
    def __init__(self):
        self.wallet: float = INITIAL_WALLET
        self.positions: Dict[str, Position] = {}
        self.trades: List[dict] = []
        self.consecutive_losses: int = 0
        self.circuit_breaker: bool = False
        self.price_history: Dict[str, List[float]] = {}
        self._load()

    def _load(self):
        os.makedirs("data", exist_ok=True)
        if not os.path.exists(DATA_FILE):
            self.save()
            return
        try:
            with open(DATA_FILE) as f:
                s = json.load(f)
            self.wallet             = s.get("wallet", INITIAL_WALLET)
            self.trades             = s.get("trades", [])
            self.consecutive_losses = s.get("consecutive_losses", 0)
            self.circuit_breaker    = s.get("circuit_breaker", False)
            for p in s.get("positions", []):
                pos = Position(**p)
                self.positions[pos.token_id] = pos
            log.info(f"Loaded state: wallet=${self.wallet:.2f}, "
                     f"{len(self.positions)} open, {len(self.trades)} records")
        except Exception as exc:
            log.warning(f"Could not load state ({exc}) — starting fresh")

    def save(self):
        state = {
            "wallet":             round(self.wallet, 6),
            "positions":          [asdict(p) for p in self.positions.values()],
            "trades":             self.trades,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker":    self.circuit_breaker,
            "last_updated":       datetime.now(timezone.utc).isoformat(),
            "stats":              self.compute_stats(),
        }
        with open(DATA_FILE, "w") as f:
            json.dump(state, f, indent=2)
        _write_combined_dashboard()

    def compute_stats(self) -> dict:
        closed = [t for t in self.trades if t.get("pnl") is not None]
        wins   = [t for t in closed if t["pnl"] > 0]
        return {
            "total_pnl":      round(sum(t["pnl"] for t in closed), 4) if closed else 0.0,
            "win_rate":       round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "total_trades":   len(closed),
            "wins":           len(wins),
            "losses":         len(closed) - len(wins),
            "initial_wallet": INITIAL_WALLET,
            "current_wallet": round(self.wallet, 4),
        }


# ── Mean-reversion strategy ───────────────────────────────────────────────────

class MeanReversionStrategy:
    def __init__(self, state: TradingState):
        self._state = state

    def _trend(self, token_id: str) -> Optional[str]:
        prices = self._state.price_history.get(token_id, [])
        if len(prices) < TREND_WINDOW:
            return None
        recent = prices[-TREND_WINDOW:]
        half   = TREND_WINDOW // 2
        early  = sum(recent[:half]) / half
        late   = sum(recent[half:]) / (TREND_WINDOW - half)
        if late > early * 1.005: return "up"
        if late < early * 0.995: return "down"
        return "neutral"

    def signal(self, market: dict, price: float) -> Optional[str]:
        if self._state.circuit_breaker:
            return None
        token_id   = market["token_id"]
        side       = market["side"]
        trend      = self._trend(token_id)
        in_position = token_id in self._state.positions

        if not in_position:
            if side == "up"   and trend == "up" and price < BUY_THRESHOLD_UP:   return "open"
            if side == "down" and trend == "up" and price < BUY_THRESHOLD_DOWN: return "open"
        else:
            pos = self._state.positions.get(token_id)
            if pos:
                held = (datetime.now(timezone.utc) - datetime.fromisoformat(pos.opened_at)).total_seconds()
                if held < MIN_HOLD_SECONDS:
                    return None
            if side == "up"   and price > SELL_THRESHOLD_UP:   return "close"
            if side == "down" and price > SELL_THRESHOLD_DOWN: return "close"
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
        shares   = round(size / price, 6)
        trade_id = uuid.uuid4().hex[:8]
        pos = Position(
            trade_id=trade_id, market_id=market["id"], question=market["question"],
            asset=market["asset"], side=market["side"], entry_price=price,
            size_usd=size, shares=shares, token_id=market["token_id"],
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        self._state.positions[market["token_id"]] = pos
        self._state.wallet -= size
        self._state.trades.append({
            "id": trade_id, "market_id": market["id"], "question": market["question"],
            "asset": market["asset"], "side": market["side"], "action": "open",
            "price": price, "size_usd": size, "shares": shares,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._state.save()
        log.info(f"[OPEN ] {market['asset']} {market['side'].upper()} "
                 f"price={price:.4f}  size=${size:.2f}  wallet=${self._state.wallet:.2f}")

    def close_position(self, market: dict, price: float):
        token_id = market["token_id"]
        pos = self._state.positions.get(token_id)
        if pos is None:
            return
        proceeds = round(pos.shares * price, 6)
        pnl      = round(proceeds - pos.size_usd, 6)
        if pnl < 0:
            self._state.consecutive_losses += 1
            if self._state.consecutive_losses >= CIRCUIT_BREAKER_LOSSES:
                self._state.circuit_breaker = True
                log.warning(f"CIRCUIT BREAKER — {self._state.consecutive_losses} consecutive losses.")
        else:
            self._state.consecutive_losses = 0
        self._state.wallet = round(self._state.wallet + proceeds, 6)
        del self._state.positions[token_id]
        pnl_tag = f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}"
        self._state.trades.append({
            "id": uuid.uuid4().hex[:8], "market_id": market["id"],
            "question": market["question"], "asset": market["asset"],
            "side": market["side"], "action": "close", "price": price,
            "size_usd": proceeds, "shares": pos.shares, "entry_price": pos.entry_price,
            "timestamp": datetime.now(timezone.utc).isoformat(), "pnl": pnl,
        })
        self._state.save()
        log.info(f"[CLOSE] {market['asset']} {market['side'].upper()} "
                 f"price={price:.4f}  pnl={pnl_tag}  wallet=${self._state.wallet:.2f}")


# ── WebSocket feed handler ────────────────────────────────────────────────────

class PolymarketFeed:
    def __init__(self, state: TradingState):
        self._state            = state
        self._strategy         = MeanReversionStrategy(state)
        self._executor         = PaperExecutor(state)
        self._market_map: Dict[str, dict] = {}
        self._last_refresh     = 0.0
        self._tick_count       = 0
        self._last_status_log  = time.monotonic()
        self._last_tick_time: Dict[str, datetime] = {}

    def _record_price(self, token_id: str, price: float):
        h = self._state.price_history.setdefault(token_id, [])
        h.append(price)
        if len(h) > 200:
            del h[0]

    def _on_tick(self, tick: dict):
        if "price_changes" in tick:
            for c in tick.get("price_changes", []):
                self._on_tick(c)
            return
        asset_id  = tick.get("asset_id")
        if not asset_id:
            return
        price_raw = tick.get("price") or tick.get("mid_price")
        if price_raw is None:
            price_raw = tick.get("last_trade_price")
        if price_raw is None:
            bids = tick.get("bids", [])
            asks = tick.get("asks", [])
            if bids and asks:
                try:
                    b, a = float(bids[0]["price"]), float(asks[0]["price"])
                    if a - b <= 0.20:
                        price_raw = (b + a) / 2
                except Exception:
                    pass
        if price_raw is None:
            return
        try:
            price = float(price_raw)
        except Exception:
            return
        if price <= 0:
            return

        market = self._market_map.get(asset_id)
        if not market:
            return

        self._record_price(asset_id, price)
        self._last_tick_time[asset_id] = datetime.now(timezone.utc)
        self._tick_count += 1
        log.debug(f"[TICK ] {market['asset']} {market['side'].upper()} price={price:.4f}")

        now = time.monotonic()
        if now - self._last_status_log >= 300:
            self._last_status_log = now
            parts = [
                f"{self._market_map[tid]['asset']} {self._market_map[tid]['side']}="
                f"{round(self._state.price_history[tid][-1],4)}({len(self._state.price_history[tid])}t)"
                for tid in self._market_map if self._state.price_history.get(tid)
            ]
            log.info(f"[STATUS] ticks={self._tick_count}  wallet=${self._state.wallet:.2f}  "
                     f"pos={len(self._state.positions)}  {'  '.join(parts)}")

        sig = self._strategy.signal(market, price)
        if sig == "open":
            self._executor.open_position(market, price)
        elif sig == "close":
            self._executor.close_position(market, price)

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if isinstance(data, list):
            for tick in data:
                self._on_tick(tick)
        elif isinstance(data, dict):
            self._on_tick(data)

    async def _refresh_markets(self) -> List[str]:
        markets = await asyncio.get_event_loop().run_in_executor(None, fetch_crypto_1h_markets)
        self._market_map  = {m["token_id"]: m for m in markets}
        self._last_refresh = time.monotonic()
        return list(self._market_map)

    async def run(self):
        backoff = 60
        while True:
            token_ids = await self._refresh_markets()
            if token_ids:
                break
            log.warning(f"No 1h markets found. Retrying in {backoff}s…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 1800)

        ws_backoff = 1
        while True:
            try:
                await self._ws_session(token_ids)
                ws_backoff = 1
            except Exception as exc:
                log.error(f"WS error: {exc}. Reconnecting in {ws_backoff}s…")
                await asyncio.sleep(ws_backoff)
                ws_backoff = min(ws_backoff * 2, 60)
                if time.monotonic() - self._last_refresh >= MARKET_REFRESH_INTERVAL:
                    token_ids = await self._refresh_markets() or token_ids

    async def _ws_session(self, token_ids: List[str]):
        log.info(f"Connecting to {CLOB_WS_URL} …")
        async with websockets.connect(CLOB_WS_URL, ping_interval=20, ping_timeout=30) as ws:
            await ws.send(json.dumps({"assets_ids": token_ids, "type": "Market"}))
            log.info(f"Subscribed to {len(token_ids)} token(s)")
            refresh_task = asyncio.create_task(self._periodic_refresh(ws))
            stale_task   = asyncio.create_task(self._stale_position_checker())
            try:
                async for raw in ws:
                    self._handle_message(raw)
            finally:
                refresh_task.cancel()
                stale_task.cancel()

    async def _periodic_refresh(self, ws):
        while True:
            await asyncio.sleep(MARKET_REFRESH_INTERVAL)
            token_ids = await self._refresh_markets()
            if token_ids:
                await ws.send(json.dumps({"assets_ids": token_ids, "type": "Market"}))
                log.info(f"Refreshed subscription: {len(token_ids)} market(s)")

    async def _stale_position_checker(self):
        while True:
            await asyncio.sleep(120)
            now = datetime.now(timezone.utc)
            for token_id, pos in list(self._state.positions.items()):
                last_tick = self._last_tick_time.get(token_id)
                if last_tick is None:
                    continue
                silence = (now - last_tick).total_seconds()
                if silence >= STALE_POSITION_SECONDS:
                    log.warning(f"[STALE] {pos.asset} {pos.side.upper()} silent {silence:.0f}s — force-closing")
                    last_price = self._state.price_history.get(token_id, [0.5])[-1]
                    self._executor.close_position(
                        {"id": pos.market_id, "question": pos.question,
                         "asset": pos.asset, "side": pos.side, "token_id": token_id},
                        last_price,
                    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    log.info("=" * 62)
    log.info("  Polymarket Paper Trader  |  1h Crypto Up/Down  ")
    log.info(f"  Strategy : Mean-Reversion  |  Starting wallet: ${INITIAL_WALLET}")
    log.info(f"  Thresholds : buy <{BUY_THRESHOLD_UP} / >{BUY_THRESHOLD_DOWN}  "
             f"sell >{SELL_THRESHOLD_UP} / <{SELL_THRESHOLD_DOWN}")
    log.info("=" * 62)

    state = TradingState()
    if state.circuit_breaker:
        log.warning("Circuit breaker ACTIVE. Set circuit_breaker=false in "
                    "data/paper_trades_1h.json to resume.")
        return

    await PolymarketFeed(state).run()


if __name__ == "__main__":
    asyncio.run(main())
