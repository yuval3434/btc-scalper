# BTC Scalper

An automated trading bot for Polymarket's 5-minute Bitcoin up/down markets. It finds newly opened markets, watches the live order book over WebSocket, enters when the book shows clear buying pressure, and manages every position with a take-profit, a price stop and a time stop.

The interesting part of this project was never the strategy. It was everything around it: reconnecting when the WebSocket drops, not trading blind after a disconnect, stopping itself after a losing streak, and never placing a real order by accident.

## How it works

```
market_finder ──► market_data (WebSocket order book)
      │                    │
      │                    ▼
      │               strategy ──► order_executor ──► Polymarket CLOB
      │                    ▲              │
      └──── state ◄────────┴── user_events (fills, WebSocket)
              │
              ├──► trade_log
              └──► notifier (Telegram)
```

| Module | Responsibility |
|---|---|
| `market_finder.py` | Polls the Gamma API every 30s for newly opened 5-minute BTC markets |
| `market_data.py` | Subscribes to the market WebSocket and keeps a live order book |
| `strategy.py` | Decides when to enter and exit (rules below) |
| `order_executor.py` | Signs and sends orders to the Polymarket CLOB |
| `user_events.py` | Listens for the bot's own fills on the user WebSocket |
| `state.py` | Tracks open positions and the kill-switch state |
| `trade_log.py` | Records every trade for later analysis |
| `notifier.py` | Optional Telegram alerts |
| `backtest/` | Backtesting the strategy parameters on historical data |

## Strategy

- **Entry zone** — only trade markets whose mid price is between 0.25 and 0.75. Outside that band the outcome is close to decided and there is little room to move.
- **Entry signal** — order book imbalance. Enter when bid volume over the top 5 levels is at least 1.3× the ask volume.
- **Take profit** — sell 0.12 above the fill price.
- **Stop loss** — exit if the best bid drops 0.08 below the fill price.
- **Time stop** — exit if less than 45 seconds remain in the market.

The stop-loss width came from testing: a tighter 0.05 stop triggered on normal noise and badly hurt the win rate. All parameters live in `config.py` with a comment explaining each one.

## Reliability

- **Dry run by default.** `DRY_RUN` is `true` unless explicitly turned off, so a fresh setup never sends a real order.
- **Kill switch.** After 3 consecutive losses the bot pauses trading for an hour.
- **WebSocket reconnection** with exponential backoff (1s up to 30s) and a heartbeat every 10s.
- **Position limits.** At most 3 markets at once, with a fixed size per trade.
- **No secrets in code.** All keys are read from environment variables.

## Setup

```bash
git clone https://github.com/yuval3434/btc-scalper.git
cd btc-scalper
pip install -r requirements.txt
```

Create a `.env` file (it is ignored by git):

```
PRIVATE_KEY=
FUNDER_ADDRESS=
API_KEY=
API_SECRET=
API_PASSPHRASE=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DRY_RUN=true
```

Run:

```bash
python main.py
```

See `RUNBOOK.md` for operational details.

## Tech stack

Python · asyncio · WebSockets · Polymarket CLOB API · Polygon · Telegram Bot API

## What I'd do differently

- Add unit tests for the strategy logic instead of validating it only through paper trading and backtests.
- Move the one-off diagnostic and setup scripts into a separate `scripts/` folder.
- Store trade history in a database instead of CSV files.

## Disclaimer

Personal project built to learn. Not financial advice.
