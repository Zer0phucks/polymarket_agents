# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Polymarket Agents is a Python 3.9 AI-powered trading agent framework for [Polymarket](https://polymarket.com) prediction markets. It combines on-chain trading (Polygon blockchain), LLM decision-making (OpenAI GPT), and RAG over market data to enable autonomous trading.

## Environment Setup

```bash
virtualenv --python=python3.9 .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in required keys
```

Required environment variables:
- `POLYGON_WALLET_PRIVATE_KEY` — wallet signing key for on-chain trades
- `OPENAI_API_KEY` — GPT model access
- `TAVILY_API_KEY` — web search
- `NEWSAPI_API_KEY` — news articles

## Commands

```bash
# CLI (always set PYTHONPATH first)
export PYTHONPATH="."
python scripts/python/cli.py --help
python scripts/python/cli.py get-all-markets --limit 10 --sort-by spread
python scripts/python/cli.py run-autonomous-trader

# Run tests
python -m unittest discover

# Run a single test file
python -m unittest tests/test_foo.py

# Start API server
uvicorn scripts.python.server:app --reload

# Docker
./scripts/bash/build-docker.sh
./scripts/bash/run-docker-dev.sh
```

CI/CD (GitHub Actions) runs `black` linting and `unittest` on push/PR to main. Pre-commit hooks also enforce `black` (Python 3.9).

## Architecture

### Data Flow — Autonomous Trading Pipeline

1. Fetch all tradeable events from Gamma API (`GammaMarketClient`)
2. Filter events via vector similarity search (`PolymarketRAG` / Chroma)
3. Map filtered events to their markets
4. Use `Executor` (LangChain + GPT) to select trades
5. Execute orders on-chain via CLOB client (`Polymarket`)

> **Note:** Trade execution is commented out in `trade.py` — it requires accepting Polymarket TOS first.

### Key Modules

| Module | Responsibility |
|---|---|
| `agents/polymarket/polymarket.py` | CLOB client, order building/signing, Polygon Web3, USDC/CTF approvals |
| `agents/polymarket/gamma.py` | Gamma API client — fetches market/event metadata |
| `agents/application/executor.py` | LangChain LLM orchestration, token limit management, prompt composition |
| `agents/application/trade.py` | Autonomous trading strategy — orchestrates the full pipeline |
| `agents/application/creator.py` | Suggests new markets to create |
| `agents/connectors/chroma.py` | Chroma vector DB for RAG over market embeddings |
| `agents/connectors/news.py` | NewsAPI integration |
| `agents/connectors/search.py` | Tavily web search integration |
| `agents/application/prompts.py` | LLM prompt templates |
| `agents/utils/objects.py` | Pydantic data models (`Trade`, `Market`, `PolymarketEvent`, etc.) |
| `scripts/python/cli.py` | Typer CLI — user-facing entry point for all commands |
| `scripts/python/server.py` | FastAPI server skeleton |

### LLM Token Limits

`Executor` selects the model based on context size:
- `gpt-3.5-turbo-16k` → 15k token limit
- `gpt-4-1106-preview` → 95k token limit

### Data Models

Pydantic models in `agents/utils/objects.py` represent the core domain. Some fields (e.g., `outcomePrices`, `clobTokenIds`) arrive from the Gamma API as JSON-stringified strings and are parsed at the model level.
