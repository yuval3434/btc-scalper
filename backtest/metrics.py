"""
backtest/metrics.py ? Performance statistics from backtest results.

Calculates the standard set of trading metrics plus a text equity curve.
All calculations are on USDC PnL (dollar terms).
"""

import math
import statistics
from dataclasses import dataclass
from typing import Optional

from backtest.engine import BacktestTrade, MarketResult


@dataclass
class BacktestMetrics:
    # Counts
    total_markets_scanned: int
    markets_with_data: int
    markets_with_entry: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    tp_exits: int
    price_sl_exits: int
    time_sl_exits: int
    expired_exits: int

    # PnL
    total_pnl_usdc: float
    avg_pnl_usdc: float
    avg_win_usdc: float
    avg_loss_usdc: float
    best_trade_usdc: float
    worst_trade_usdc: float

    # Ratios
    win_rate_pct: float
    profit_factor: float        # gross_wins / abs(gross_losses)
    expectancy_usdc: float      # avg expected value per trade

    # Risk
    max_drawdown_usdc: float
    max_consecutive_losses: int
    sharpe_ratio: Optional[float]   # simplified: mean/std of trade PnL

    # Entry quality
    avg_mid_at_entry: float
    avg_imbalance_at_entry: float
    avg_secs_to_expiry_at_entry: float

    # Equity curve
    equity_curve: list[float]   # cumulative PnL after each trade


def compute_metrics(
    results: list[MarketResult],
) -> BacktestMetrics:
    all_trades: list[BacktestTrade] = [t for r in results for t in r.trades]

    markets_with_data = sum(1 for r in results if r.market.ticks)
    markets_with_entry = sum(1 for r in results if r.trades)

    wins = [t for t in all_trades if t.pnl_usdc > 0]
    losses = [t for t in all_trades if t.pnl_usdc <= 0]

    tp_exits = sum(1 for t in all_trades if t.exit_reason == "tp")
    price_sl = sum(1 for t in all_trades if t.exit_reason == "price_sl")
    time_sl = sum(1 for t in all_trades if t.exit_reason == "time_sl")
    expired = sum(1 for t in all_trades if t.exit_reason == "expired")

    total_pnl = sum(t.pnl_usdc for t in all_trades)
    avg_pnl = total_pnl / len(all_trades) if all_trades else 0.0
    avg_win = sum(t.pnl_usdc for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_usdc for t in losses) / len(losses) if losses else 0.0

    gross_wins = sum(t.pnl_usdc for t in wins)
    gross_losses = abs(sum(t.pnl_usdc for t in losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0.0
    expectancy = win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss

    # Equity curve.
    equity: list[float] = []
    cumulative = 0.0
    for t in all_trades:
        cumulative += t.pnl_usdc
        equity.append(round(cumulative, 4))

    # Max drawdown (peak-to-trough on equity curve).
    max_drawdown = 0.0
    peak = 0.0
    for val in equity:
        if val > peak:
            peak = val
        dd = peak - val
        if dd > max_drawdown:
            max_drawdown = dd

    # Max consecutive losses.
    max_consec = 0
    current_consec = 0
    for t in all_trades:
        if t.pnl_usdc <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    # Sharpe (simplified: mean/std of trade-level PnL, not annualised).
    sharpe = None
    if len(all_trades) >= 2:
        pnl_list = [t.pnl_usdc for t in all_trades]
        std = statistics.stdev(pnl_list)
        if std > 0:
            sharpe = round(statistics.mean(pnl_list) / std, 3)

    # Entry quality averages.
    avg_mid = sum(t.mid_at_entry for t in all_trades) / len(all_trades) if all_trades else 0.0
    avg_imb = sum(t.imbalance_at_entry for t in all_trades) / len(all_trades) if all_trades else 0.0
    avg_secs = sum(t.seconds_to_expiry_at_entry for t in all_trades) / len(all_trades) if all_trades else 0.0

    return BacktestMetrics(
        total_markets_scanned=len(results),
        markets_with_data=markets_with_data,
        markets_with_entry=markets_with_entry,
        total_trades=len(all_trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        tp_exits=tp_exits,
        price_sl_exits=price_sl,
        time_sl_exits=time_sl,
        expired_exits=expired,
        total_pnl_usdc=round(total_pnl, 4),
        avg_pnl_usdc=round(avg_pnl, 4),
        avg_win_usdc=round(avg_win, 4),
        avg_loss_usdc=round(avg_loss, 4),
        best_trade_usdc=round(max((t.pnl_usdc for t in all_trades), default=0.0), 4),
        worst_trade_usdc=round(min((t.pnl_usdc for t in all_trades), default=0.0), 4),
        win_rate_pct=round(win_rate, 2),
        profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else 999.0,
        expectancy_usdc=round(expectancy, 4),
        max_drawdown_usdc=round(max_drawdown, 4),
        max_consecutive_losses=max_consec,
        sharpe_ratio=sharpe,
        avg_mid_at_entry=round(avg_mid, 4),
        avg_imbalance_at_entry=round(avg_imb, 3),
        avg_secs_to_expiry_at_entry=round(avg_secs, 1),
        equity_curve=equity,
    )


def print_report(
    metrics: BacktestMetrics,
    trades: list[BacktestTrade],
    show_trades: bool = True,
    equity_width: int = 60,
) -> None:
    """Print a formatted backtest report to stdout."""
    sep = "-" * 65

    print(f"\n{'='*65}")
    print("  POLYMARKET BTC 5-MIN SCALPING -- BACKTEST REPORT")
    print(f"{'='*65}")

    print(f"\n{'MARKETS':}")
    print(f"  Scanned:        {metrics.total_markets_scanned:>8}")
    print(f"  With tick data: {metrics.markets_with_data:>8}")
    print(f"  With entries:   {metrics.markets_with_entry:>8}")

    print(f"\n{'TRADES':}")
    print(f"  Total:          {metrics.total_trades:>8}")
    print(f"  Wins:           {metrics.winning_trades:>8}  ({metrics.win_rate_pct:.1f}%)")
    print(f"  Losses:         {metrics.losing_trades:>8}")
    print(f"    + TP exits:     {metrics.tp_exits:>6}")
    print(f"    + Price SL:     {metrics.price_sl_exits:>6}")
    print(f"    + Time SL:      {metrics.time_sl_exits:>6}")
    print(f"    + Expired:      {metrics.expired_exits:>6}")

    print(f"\n{'PnL (USDC)':}")
    print(f"  Total:          {metrics.total_pnl_usdc:>+10.4f}")
    print(f"  Avg per trade:  {metrics.avg_pnl_usdc:>+10.4f}")
    print(f"  Avg win:        {metrics.avg_win_usdc:>+10.4f}")
    print(f"  Avg loss:       {metrics.avg_loss_usdc:>+10.4f}")
    print(f"  Best trade:     {metrics.best_trade_usdc:>+10.4f}")
    print(f"  Worst trade:    {metrics.worst_trade_usdc:>+10.4f}")

    print(f"\n{'RISK':}")
    print(f"  Profit factor:  {metrics.profit_factor:>10.3f}")
    print(f"  Expectancy:     {metrics.expectancy_usdc:>+10.4f}  USDC/trade")
    print(f"  Max drawdown:   {metrics.max_drawdown_usdc:>10.4f}  USDC")
    print(f"  Max consec loss:{metrics.max_consecutive_losses:>10}")
    sharpe_str = f"{metrics.sharpe_ratio:.3f}" if metrics.sharpe_ratio is not None else "N/A"
    print(f"  Sharpe ratio:   {sharpe_str:>10}")

    print(f"\n{'ENTRY QUALITY':}")
    print(f"  Avg mid at entry:    {metrics.avg_mid_at_entry:.4f}")
    print(f"  Avg imbalance:       {metrics.avg_imbalance_at_entry:.3f}")
    print(f"  Avg secs to expiry:  {metrics.avg_secs_to_expiry_at_entry:.0f}s")

    # ASCII equity curve
    if metrics.equity_curve:
        print(f"\n{'EQUITY CURVE':}")
        _print_equity_curve(metrics.equity_curve, width=equity_width)

    # Per-trade log
    if show_trades and trades:
        print(f"\n{sep}")
        print(f"  {'#':>3}  {'MARKET':<28}  {'ENTRY':>6}  {'EXIT':>6}  "
              f"{'REASON':<9}  {'PnL':>8}")
        print(sep)
        for i, t in enumerate(trades, 1):
            slug_short = t.market_slug[-24:] if len(t.market_slug) > 24 else t.market_slug
            pnl_sign = "+" if t.pnl_usdc >= 0 else ""
            print(
                f"  {i:>3}  {slug_short:<28}  "
                f"{t.fill_price:>6.4f}  {t.exit_price:>6.4f}  "
                f"{t.exit_reason:<9}  {pnl_sign}{t.pnl_usdc:>7.4f}"
            )
        print(sep)

    print(f"{'='*65}\n")


def _print_equity_curve(curve: list[float], width: int = 60) -> None:
    """Simple ASCII chart of cumulative PnL."""
    if not curve:
        return
    mn, mx = min(curve), max(curve)
    rng = mx - mn or 1.0
    height = 8
    rows = [[" "] * width for _ in range(height)]
    zero_row = height - 1 - int((0 - mn) / rng * (height - 1))
    zero_row = max(0, min(height - 1, zero_row))

    for x, val in enumerate(curve):
        col = int(x / len(curve) * (width - 1))
        row = height - 1 - int((val - mn) / rng * (height - 1))
        row = max(0, min(height - 1, row))
        rows[row][col] = "#" if val >= 0 else "#"

    print(f"  {mx:>+8.3f} ?")
    for r, row in enumerate(rows):
        prefix = "  0.000   +" if r == zero_row else "          |"
        print(prefix + "".join(row))
    print(f"  {mn:>+8.3f} ?")
    print(f"  {'start':^{width//2}}{'? end':^{width//2}}")
