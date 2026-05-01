"""
backtest/grid_search.py — Parameter grid search for the scalping strategy.

Usage (from btc_scalper/):
    python -m backtest.grid_search
"""

import asyncio
import logging
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from backtest.data_fetcher import find_resolved_btc_markets, fetch_all_trade_histories
from backtest.engine import BacktestEngine, StrategyParams
from backtest.metrics import compute_metrics

logging.basicConfig(level=logging.WARNING)

DAYS = 7
MAX_MARKETS = 300
SAFE_HEADERS = {"Accept-Encoding": "gzip, deflate"}

GRID = {
    "mid_low":      [0.20, 0.25, 0.30, 0.35],
    "sl_time":      [20, 30, 45, 60],
    "min_imbalance":[1.00, 1.05, 1.20],
    "buy_offset":   [0.03, 0.05],
    "tp_offset":    [0.05, 0.06, 0.08],
}


async def load_markets():
    async with aiohttp.ClientSession(headers=SAFE_HEADERS) as session:
        markets = await find_resolved_btc_markets(session, days_back=DAYS, max_markets=MAX_MARKETS)
        await fetch_all_trade_histories(session, markets, concurrency=8)
    return markets


def run_config(markets, mid_low, sl_time, min_imbalance, buy_offset, tp_offset):
    p = StrategyParams()
    p.MID_LOW = mid_low
    p.MID_HIGH = round(1.0 - mid_low, 2)
    p.SL_TIME_SECONDS = sl_time
    p.IMBALANCE_MIN_RATIO = min_imbalance
    p.BUY_OFFSET = buy_offset
    p.TP_OFFSET = tp_offset

    engine = BacktestEngine(p)
    results = engine.run_all(markets)
    m = compute_metrics(results)
    return m


def main():
    print("Loading market data (uses cache if available)...")
    markets = asyncio.run(load_markets())
    print(f"Loaded {len(markets)} markets with {sum(len(m.ticks) for m in markets)} ticks.")
    print()

    header = (
        f"{'mid_lo':>6} {'sl_t':>5} {'imb':>5} {'buy':>5} {'tp':>5} | "
        f"{'mkts':>5} {'trades':>6} {'wr%':>6} {'PnL':>8} {'PF':>7} {'Sharpe':>7}"
    )
    print(header)
    print("-" * len(header))

    results_all = []
    for mid_low in GRID["mid_low"]:
        for sl_time in GRID["sl_time"]:
            for imb in GRID["min_imbalance"]:
                for buy_off in GRID["buy_offset"]:
                    for tp_off in GRID["tp_offset"]:
                        m = run_config(markets, mid_low, sl_time, imb, buy_off, tp_off)
                        sharpe_s = f"{m.sharpe_ratio:.3f}" if m.sharpe_ratio else "  N/A"
                        row = (
                            f"{mid_low:>6.2f} {sl_time:>5d} {imb:>5.2f} "
                            f"{buy_off:>5.2f} {tp_off:>5.2f} | "
                            f"{m.markets_with_entry:>5} {m.total_trades:>6} "
                            f"{m.win_rate_pct:>5.1f}% "
                            f"{m.total_pnl_usdc:>+8.2f} {m.profit_factor:>7.3f} "
                            f"{sharpe_s:>7}"
                        )
                        print(row)
                        results_all.append((m.total_pnl_usdc, m.sharpe_ratio or 0, m.profit_factor,
                                            mid_low, sl_time, imb, buy_off, tp_off, m))

    print()
    print("=== TOP 10 BY SHARPE RATIO ===")
    results_all.sort(key=lambda x: x[1], reverse=True)
    print(header)
    print("-" * len(header))
    for _, _, _, mid_low, sl_time, imb, buy_off, tp_off, m in results_all[:10]:
        sharpe_s = f"{m.sharpe_ratio:.3f}" if m.sharpe_ratio else "  N/A"
        print(
            f"{mid_low:>6.2f} {sl_time:>5d} {imb:>5.2f} "
            f"{buy_off:>5.2f} {tp_off:>5.2f} | "
            f"{m.markets_with_entry:>5} {m.total_trades:>6} "
            f"{m.win_rate_pct:>5.1f}% "
            f"{m.total_pnl_usdc:>+8.2f} {m.profit_factor:>7.3f} "
            f"{sharpe_s:>7}"
        )

    print()
    print("=== TOP 10 BY TOTAL PnL ===")
    results_all.sort(key=lambda x: x[0], reverse=True)
    print(header)
    print("-" * len(header))
    for _, _, _, mid_low, sl_time, imb, buy_off, tp_off, m in results_all[:10]:
        sharpe_s = f"{m.sharpe_ratio:.3f}" if m.sharpe_ratio else "  N/A"
        print(
            f"{mid_low:>6.2f} {sl_time:>5d} {imb:>5.2f} "
            f"{buy_off:>5.2f} {tp_off:>5.2f} | "
            f"{m.markets_with_entry:>5} {m.total_trades:>6} "
            f"{m.win_rate_pct:>5.1f}% "
            f"{m.total_pnl_usdc:>+8.2f} {m.profit_factor:>7.3f} "
            f"{sharpe_s:>7}"
        )


if __name__ == "__main__":
    main()
