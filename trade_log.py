"""
trade_log.py -- Appends every closed trade to trades.csv for later analysis.

Each row captures both the outcome (PnL, exit reason) and the entry context
(imbalance, mid price, time to expiry) so you can find what conditions
predict wins vs losses.
"""

import csv
import os
from datetime import datetime, timezone

TRADES_CSV = os.path.join(os.path.dirname(__file__), "trades.csv")

FIELDS = [
    "timestamp_entry",
    "timestamp_exit",
    "market_slug",
    "token_id",
    "fill_price",
    "shares",
    "position_usdc",
    "tp_price",
    "sl_price",
    "exit_price",
    "exit_reason",       # "tp" | "price" | "time"
    "pnl",
    "session_pnl",
    "win",               # 1 or 0
    "dry_run",           # 1 = paper, 0 = live
    "imbalance_at_entry",
    "mid_at_entry",
    "secs_to_expiry_at_entry",
]


def record(
    *,
    market_slug: str,
    token_id: str,
    fill_price: float,
    shares: float,
    position_usdc: float,
    tp_price: float,
    sl_price: float,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    session_pnl: float,
    entry_time: datetime,
    dry_run: bool,
    imbalance_at_entry: float,
    mid_at_entry: float,
    secs_to_expiry_at_entry: float,
) -> None:
    """Append one trade to trades.csv. Thread-safe for single-process use."""
    now = datetime.now(tz=timezone.utc)
    file_exists = os.path.exists(TRADES_CSV)

    row = {
        "timestamp_entry": entry_time.isoformat() if entry_time else "",
        "timestamp_exit": now.isoformat(),
        "market_slug": market_slug,
        "token_id": token_id,
        "fill_price": round(fill_price, 4),
        "shares": round(shares, 2),
        "position_usdc": round(position_usdc, 2),
        "tp_price": round(tp_price, 4),
        "sl_price": round(sl_price, 4),
        "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason,
        "pnl": round(pnl, 4),
        "session_pnl": round(session_pnl, 4),
        "win": 1 if pnl > 0 else 0,
        "dry_run": 1 if dry_run else 0,
        "imbalance_at_entry": round(imbalance_at_entry, 3),
        "mid_at_entry": round(mid_at_entry, 4),
        "secs_to_expiry_at_entry": round(secs_to_expiry_at_entry, 1),
    }

    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
