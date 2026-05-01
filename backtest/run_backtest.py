"""
backtest/run_backtest.py — CLI entry point for the backtesting system.

Usage:
    # From inside btc_scalper/ directory:
    python -m backtest.run_backtest
    python -m backtest.run_backtest --days 14 --position-size 25 --markets 200
    python -m backtest.run_backtest --days 3 --no-cache --no-trades-log

Options:
    --days N            How many days back to look for resolved markets (default: 7)
    --markets N         Max resolved markets to backtest (default: 300)
    --position-size N   USDC per trade (default: 10)
    --min-imbalance N   Imbalance ratio threshold (default: 1.20)
    --no-cache          Ignore cached data and re-fetch everything
    --no-trades-log     Skip the per-trade table in the report
    --csv PATH          Also write results to a CSV file
    --include-15m       Include 15-minute BTC markets (default: 5m only)

Output includes:
    - Market/trade counts and skip reasons
    - PnL statistics (total, per-trade, win/loss breakdown)
    - Risk metrics (drawdown, Sharpe, consecutive losses)
    - ASCII equity curve
    - Per-trade log (unless --no-trades-log)
"""

import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path

import aiohttp

# Ensure btc_scalper root is on the path when running as a module.
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backtest.data_fetcher import (
    find_resolved_btc_markets,
    fetch_all_trade_histories,
)
from backtest.engine import BacktestEngine, StrategyParams
from backtest.metrics import compute_metrics, print_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("backtest")

SAFE_HEADERS = {"Accept-Encoding": "gzip, deflate"}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Backtest the Polymarket BTC 5-min scalping strategy.",
    )
    p.add_argument("--days", type=int, default=7,
                   help="Days of history to fetch (default: 7)")
    p.add_argument("--markets", type=int, default=300,
                   help="Max resolved markets to include (default: 300)")
    p.add_argument("--position-size", type=float, default=10.0,
                   help="USDC per trade (default: 10)")
    p.add_argument("--min-imbalance", type=float, default=1.00,
                   help="Imbalance ratio threshold (default: 1.00)")
    p.add_argument("--mid-low", type=float, default=0.25,
                   help="Golden Zone lower bound (default: 0.25)")
    p.add_argument("--mid-high", type=float, default=0.75,
                   help="Golden Zone upper bound (default: 0.75)")
    p.add_argument("--buy-offset", type=float, default=0.03,
                   help="Buy limit offset below mid (default: 0.03)")
    p.add_argument("--tp-offset", type=float, default=0.08,
                   help="Take-profit offset above fill (default: 0.08)")
    p.add_argument("--sl-offset", type=float, default=0.08,
                   help="Price stop-loss offset below fill (default: 0.08)")
    p.add_argument("--sl-time", type=int, default=45,
                   help="Time stop-loss seconds before expiry (default: 45)")
    p.add_argument("--no-cache", action="store_true",
                   help="Ignore cache and re-fetch everything")
    p.add_argument("--no-trades-log", action="store_true",
                   help="Skip the per-trade table in the report")
    p.add_argument("--csv", type=str, default=None,
                   help="Write trade results to a CSV file")
    p.add_argument("--include-15m", action="store_true",
                   help="Also include 15-minute BTC markets")
    return p


def _clear_cache() -> None:
    cache_dir = Path(__file__).parent / ".cache"
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            f.unlink()
        logger.info("Cache cleared (%s).", cache_dir)


async def run_backtest(args: argparse.Namespace) -> None:
    if args.no_cache:
        _clear_cache()

    params = StrategyParams()
    params.POSITION_SIZE_USDC = args.position_size
    params.IMBALANCE_MIN_RATIO = args.min_imbalance
    params.BUY_OFFSET = args.buy_offset
    params.TP_OFFSET = args.tp_offset
    params.SL_PRICE_OFFSET = args.sl_offset
    params.SL_TIME_SECONDS = args.sl_time
    params.MID_LOW = args.mid_low
    params.MID_HIGH = args.mid_high

    logger.info(
        "Backtest config: days=%d  max_markets=%d  position_size=$%.2f  "
        "imbalance=%.2f  buy_offset=%.2f  tp=%.2f  sl=%.2f  sl_time=%ds",
        args.days, args.markets, params.POSITION_SIZE_USDC,
        params.IMBALANCE_MIN_RATIO, params.BUY_OFFSET,
        params.TP_OFFSET, params.SL_PRICE_OFFSET, params.SL_TIME_SECONDS,
    )

    async with aiohttp.ClientSession(headers=SAFE_HEADERS) as session:
        # Step 1: Market discovery.
        logger.info("Step 1: Discovering resolved BTC markets (days=%d)...", args.days)
        markets = await find_resolved_btc_markets(
            session,
            days_back=args.days,
            max_markets=args.markets,
            require_5min=not args.include_15m,
        )

        if not markets:
            logger.error(
                "No resolved BTC markets found for the past %d days. "
                "Try --include-15m or --days with a larger value.", args.days
            )
            return

        logger.info("Found %d resolved markets.", len(markets))

        # Step 2: Fetch trade histories.
        logger.info("Step 2: Fetching trade histories (concurrency=8)...")
        await fetch_all_trade_histories(session, markets, concurrency=8)

    markets_with_data = [m for m in markets if m.ticks]
    markets_no_data = [m for m in markets if not m.ticks]
    logger.info(
        "Trade data: %d/%d markets have ticks. %d skipped (no data).",
        len(markets_with_data), len(markets), len(markets_no_data),
    )

    if not markets_with_data:
        logger.error(
            "No trade data available for any market. "
            "The data-api endpoint may be temporarily unavailable, or "
            "the markets may be too recent for historical data."
        )
        _suggest_data_debug(markets[:3])
        return

    # Step 3: Run backtest simulation.
    logger.info("Step 3: Running strategy simulation...")
    engine = BacktestEngine(params)
    results = engine.run_all(markets)

    all_trades = [t for r in results for t in r.trades]
    metrics = compute_metrics(results)

    # Step 4: Report.
    print_report(metrics, all_trades, show_trades=not args.no_trades_log)

    # Skip breakdown.
    skip_counts: dict[str, int] = {}
    for r in results:
        if r.skipped_reason:
            skip_counts[r.skipped_reason] = skip_counts.get(r.skipped_reason, 0) + 1
    if skip_counts:
        print("  Skip breakdown:")
        for reason, count in sorted(skip_counts.items(), key=lambda x: -x[1]):
            print(f"    {reason:<25} {count}")
        print()

    # Optional CSV export.
    if args.csv and all_trades:
        _write_csv(all_trades, args.csv)
        logger.info("Trade log written to %s", args.csv)


def _write_csv(trades, path: str) -> None:
    fieldnames = [
        "market_slug", "entry_time", "exit_time",
        "fill_price", "exit_price", "exit_reason",
        "shares", "cost_usdc", "proceeds_usdc", "pnl_usdc", "pnl_pct",
        "mid_at_entry", "imbalance_at_entry", "seconds_to_expiry_at_entry",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            writer.writerow({k: getattr(t, k, "") for k in fieldnames})


def _suggest_data_debug(sample_markets) -> None:
    print("\n  ╔═══ DEBUG SUGGESTION ════════════════════════════════╗")
    print("  ║ The data-api returned no trades. Try:               ║")
    print("  ║   1. Wait a few minutes (data-api has ingestion lag)║")
    print("  ║   2. Run with --include-15m (more markets)          ║")
    print("  ║   3. Run probe_ws.py to verify connectivity         ║")
    print("  ╚══════════════════════════════════════════════════════╝")
    if sample_markets:
        print("\n  Sample condition IDs to verify manually:")
        for m in sample_markets:
            print(f"    {m.slug}  →  {m.condition_id}")
    print()


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    asyncio.run(run_backtest(args))


if __name__ == "__main__":
    main()
