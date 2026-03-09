#!/usr/bin/env python3
"""
Polymarket Paper Trader — Local Dashboard Server
─────────────────────────────────────────────────
Zero-dependency HTTP server (uses only Python stdlib).

Run:
    python scripts/python/dashboard_server.py [--port 8080]

Endpoints:
    GET /              → dashboard HTML
    GET /api/state     → combined state for both bots
    GET /api/15m       → raw 15-min bot state
    GET /api/5m        → raw 5-min bot state
    POST /api/reset/15m|5m  → reset circuit breaker
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_15M       = BASE_DIR / "data" / "paper_trades.json"
DATA_5M        = BASE_DIR / "data" / "paper_trades_5m.json"
DASHBOARD_HTML = BASE_DIR / "dashboard" / "index.html"
INITIAL_WALLET = 100.0


def _load(path: Path) -> dict:
    if not path.exists():
        return {
            "wallet": INITIAL_WALLET,
            "positions": [],
            "trades": [],
            "consecutive_losses": 0,
            "circuit_breaker": False,
            "last_updated": None,
            "stats": {
                "total_pnl": 0.0,
                "win_rate": 0.0,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "initial_wallet": INITIAL_WALLET,
                "current_wallet": INITIAL_WALLET,
            },
        }
    with open(path) as f:
        return json.load(f)


def _combined(s15: dict, s5: dict) -> dict:
    st15 = s15.get("stats", {})
    st5  = s5.get("stats",  {})
    wins   = st15.get("wins",   0) + st5.get("wins",   0)
    losses = st15.get("losses", 0) + st5.get("losses", 0)
    total  = st15.get("total_trades", 0) + st5.get("total_trades", 0)
    pnl    = round(st15.get("total_pnl", 0.0) + st5.get("total_pnl", 0.0), 4)
    wallet = round(s15.get("wallet", INITIAL_WALLET) + s5.get("wallet", INITIAL_WALLET), 4)
    open_pos = len(s15.get("positions", [])) + len(s5.get("positions", []))
    return {
        "total_pnl":      pnl,
        "win_rate":       round(wins / total * 100, 1) if total > 0 else 0.0,
        "total_trades":   total,
        "wins":           wins,
        "losses":         losses,
        "total_wallet":   wallet,
        "initial_wallet": INITIAL_WALLET * 2,
        "open_positions": open_pos,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence per-request noise; errors still print

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        body = json.dumps(data).encode()
        self._send(status, body, "application/json")

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/":
            if not DASHBOARD_HTML.exists():
                self._send(404, b"dashboard/index.html not found", "text/plain")
                return
            body = DASHBOARD_HTML.read_bytes()
            self._send(200, body, "text/html; charset=utf-8")

        elif path == "/api/state":
            s15 = _load(DATA_15M)
            s5  = _load(DATA_5M)
            self._json({"15m": s15, "5m": s5, "combined": _combined(s15, s5)})

        elif path == "/api/15m":
            self._json(_load(DATA_15M))

        elif path == "/api/5m":
            self._json(_load(DATA_5M))

        else:
            self._send(404, b"Not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path

        if path in ("/api/reset/15m", "/api/reset/5m"):
            bot = path.split("/")[-1]
            data_path = DATA_15M if bot == "15m" else DATA_5M
            data = _load(data_path)
            data["circuit_breaker"] = False
            data["consecutive_losses"] = 0
            with open(data_path, "w") as f:
                json.dump(data, f, indent=2)
            self._json({"ok": True, "bot": bot})
        else:
            self._send(404, b"Not found", "text/plain")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description="Polymarket Paper Trader Dashboard")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    server = HTTPServer((args.host, args.port), Handler)
    print(f"Dashboard running at http://{args.host}:{args.port}/")
    print(f"API:  http://{args.host}:{args.port}/api/state")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
