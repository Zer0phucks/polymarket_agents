#!/usr/bin/env bash
# Deploy latest dashboard/data.json to Vercel (no git required).
# Run hourly via cron. Logs to /tmp/vercel_deploy.log.

set -euo pipefail

REPO_DIR="/home/ubuntu/.openclaw/polymarket_agents"
LOG="/tmp/vercel_deploy.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

cd "$REPO_DIR"

# ── 1. Rebuild dashboard/data.json from both bot state files ──────────────────
python3 - <<'PYEOF'
import json, os

def load(path):
    if not os.path.exists(path):
        return {"wallet": 100.0, "positions": [], "trades": [], "stats": {}, "circuit_breaker": False}
    with open(path) as f:
        return json.load(f)

files = {"15m": "data/paper_trades.json", "5m": "data/paper_trades_5m.json", "1h": "data/paper_trades_1h.json"}
bots = {}
for key, path in files.items():
    bots[key] = load(path)

wins   = sum(bots[k].get("stats", {}).get("wins",   0) for k in bots)
losses = sum(bots[k].get("stats", {}).get("losses", 0) for k in bots)
total  = wins + losses

payload = {
    **bots,
    "combined": {
        "total_pnl":      round(sum(bots[k].get("stats", {}).get("total_pnl", 0.0) for k in bots), 4),
        "win_rate":       round(wins / total * 100, 1) if total > 0 else 0.0,
        "total_trades":   sum(bots[k].get("stats", {}).get("total_trades", 0) for k in bots),
        "wins":           wins,
        "losses":         losses,
        "total_wallet":   round(sum(bots[k].get("wallet", 100.0) for k in bots), 4),
        "initial_wallet": 300.0,
        "open_positions": sum(len(bots[k].get("positions", [])) for k in bots),
    },
}

os.makedirs("dashboard", exist_ok=True)
with open("dashboard/data.json", "w") as f:
    json.dump(payload, f, indent=2)

c = payload["combined"]
print(f"data.json: wallet=${c['total_wallet']:.2f}  pnl=${c['total_pnl']:.2f}  trades={c['total_trades']}")
PYEOF

# ── 2. Deploy dashboard/ directory to Vercel ─────────────────────────────────
log "Deploying to Vercel..."
cd "$REPO_DIR/dashboard"
vercel deploy --prod --yes 2>&1 | tail -3 | while read line; do log "$line"; done

log "Done."
