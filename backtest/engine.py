"""
backtest/engine.py — Strategy simulation on historical tick data.

Replays each market's trade history tick-by-tick and applies the exact same
rules as the live strategy:

  Golden Zone:    0.35 ≤ mid ≤ 0.65
  Imbalance:      buy_vol > sell_vol × 1.20  (rolling window)
  Limit buy:      mid − BUY_OFFSET  (post-only → fill when price ≤ our level)
  Take-profit:    fill_price + TP_OFFSET   (fill when price ≥ our level)
  Price SL:       fill_price − SL_PRICE_OFFSET  (trigger when price ≤ level)
  Time SL:        trigger when time_to_expiry ≤ SL_TIME_SECONDS

Because we don't have historical order book depth, imbalance is approximated
from rolling trade flow (buy-initiated vs sell-initiated volume).

Fill assumptions:
  - Limit buy:    filled when a trade occurs at price ≤ our limit price.
  - Limit sell:   filled when a trade occurs at price ≥ our TP price.
  - Market sell:  filled at the next trade price after trigger.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from backtest.data_fetcher import HistoricalMarket, TradeTick

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy parameters (mirrors config.py defaults)
# ---------------------------------------------------------------------------

class StrategyParams:
    MID_LOW: float = 0.35
    MID_HIGH: float = 0.65
    BUY_OFFSET: float = 0.05
    TP_OFFSET: float = 0.06
    SL_PRICE_OFFSET: float = 0.08
    SL_TIME_SECONDS: int = 60
    IMBALANCE_MIN_RATIO: float = 1.20
    IMBALANCE_WINDOW_TRADES: int = 20   # rolling window for imbalance approx
    POSITION_SIZE_USDC: float = 10.0
    MIN_SPREAD_BUFFER: float = 0.01     # synthetic spread around trade price


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    market_slug: str
    market_start: datetime
    market_end: datetime

    entry_time: datetime
    exit_time: datetime
    entry_price: float          # price we placed the limit buy at
    fill_price: float           # actual fill (≤ entry_price)
    exit_price: float
    exit_reason: str            # "tp" | "price_sl" | "time_sl" | "expired"

    shares: float
    cost_usdc: float            # fill_price × shares
    proceeds_usdc: float        # exit_price × shares
    pnl_usdc: float             # proceeds − cost (before fees)
    pnl_pct: float              # pnl / cost

    # Market context at entry
    mid_at_entry: float
    imbalance_at_entry: float
    seconds_to_expiry_at_entry: float


@dataclass
class MarketResult:
    market: HistoricalMarket
    trades: list[BacktestTrade] = field(default_factory=list)
    skipped_reason: str = ""    # if no trade attempted


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """
    Simulates the scalping strategy on historical tick data.

    One trade attempt per market (same as live bot: one open position at a time).
    """

    def __init__(self, params: Optional[StrategyParams] = None) -> None:
        self.p = params or StrategyParams()

    def run_all(self, markets: list[HistoricalMarket]) -> list[MarketResult]:
        results = []
        for mkt in markets:
            results.append(self._run_market(mkt))
        return results

    def _run_market(self, market: HistoricalMarket) -> MarketResult:
        result = MarketResult(market=market)

        if not market.ticks:
            result.skipped_reason = "no_tick_data"
            return result

        ticks = market.ticks
        n = len(ticks)

        # State machine: IDLE → BUY_ORDER_PLACED → FILLED → CLOSED
        state = "IDLE"
        limit_buy_price: float = 0.0
        fill_price: float = 0.0
        fill_time: Optional[datetime] = None
        tp_price: float = 0.0
        sl_price: float = 0.0
        mid_at_entry: float = 0.0
        imbalance_at_entry: float = 0.0
        secs_at_entry: float = 0.0
        shares: float = 0.0

        # Diagnostic: track why each tick was skipped
        ever_in_zone = False
        ever_imbalance_ok = False

        for i, tick in enumerate(ticks):
            secs_to_end = (market.end_time - tick.timestamp).total_seconds()

            # Synthetic best bid/ask from trade price (±half spread).
            half_spread = self.p.MIN_SPREAD_BUFFER / 2.0
            bid = round(tick.price - half_spread, 4)
            ask = round(tick.price + half_spread, 4)
            mid = (bid + ask) / 2.0

            # Rolling imbalance proxy.
            window = ticks[max(0, i - self.p.IMBALANCE_WINDOW_TRADES): i + 1]
            buy_vol = sum(t.size for t in window if t.side == "BUY")
            sell_vol = sum(t.size for t in window if t.side == "SELL")
            imbalance = buy_vol / sell_vol if sell_vol > 0 else float("inf")

            in_zone = self.p.MID_LOW <= mid <= self.p.MID_HIGH
            imb_ok = imbalance >= self.p.IMBALANCE_MIN_RATIO

            if in_zone:
                ever_in_zone = True
            if in_zone and imb_ok:
                ever_imbalance_ok = True

            # ----------------------------------------------------------------
            if state == "IDLE":
                # Entry condition checks.
                if secs_to_end <= self.p.SL_TIME_SECONDS + 10:
                    continue
                if not in_zone:
                    continue
                if not imb_ok:
                    continue

                # Place limit buy at mid − BUY_OFFSET.
                limit_buy_price = round(mid - self.p.BUY_OFFSET, 4)
                if limit_buy_price <= 0:
                    continue

                shares = round(self.p.POSITION_SIZE_USDC / limit_buy_price, 4)
                mid_at_entry = mid
                imbalance_at_entry = imbalance
                secs_at_entry = secs_to_end
                state = "BUY_ORDER_PLACED"

            elif state == "BUY_ORDER_PLACED":
                # Fill when price trades at or below our limit.
                if tick.price <= limit_buy_price:
                    fill_price = tick.price
                    fill_time = tick.timestamp
                    tp_price = round(fill_price + self.p.TP_OFFSET, 4)
                    sl_price = round(fill_price - self.p.SL_PRICE_OFFSET, 4)
                    state = "FILLED"

                # Cancel if conditions no longer valid.
                elif not (self.p.MID_LOW <= mid <= self.p.MID_HIGH):
                    state = "IDLE"
                    limit_buy_price = 0.0

            elif state == "FILLED":
                # TP fill — limit sell at tp_price, so exit at tp_price regardless
                # of where the triggering tick landed (no price improvement on limit).
                if tick.price >= tp_price:
                    result.trades.append(self._make_trade(
                        market, "tp",
                        fill_time, tick.timestamp,
                        limit_buy_price, fill_price, tp_price,
                        shares, mid_at_entry, imbalance_at_entry, secs_at_entry,
                    ))
                    state = "IDLE"
                    limit_buy_price = fill_price = 0.0

                # Price stop-loss — market sell, fills at current tick price.
                elif tick.price <= sl_price:
                    result.trades.append(self._make_trade(
                        market, "price_sl",
                        fill_time, tick.timestamp,
                        limit_buy_price, fill_price, tick.price,
                        shares, mid_at_entry, imbalance_at_entry, secs_at_entry,
                    ))
                    state = "IDLE"
                    limit_buy_price = fill_price = 0.0

                # Time stop-loss.
                elif secs_to_end <= self.p.SL_TIME_SECONDS:
                    result.trades.append(self._make_trade(
                        market, "time_sl",
                        fill_time, tick.timestamp,
                        limit_buy_price, fill_price, tick.price,
                        shares, mid_at_entry, imbalance_at_entry, secs_at_entry,
                    ))
                    state = "IDLE"
                    limit_buy_price = fill_price = 0.0

        # Market expired with open position.
        if state == "FILLED" and fill_time is not None:
            last = ticks[-1]
            result.trades.append(self._make_trade(
                market, "expired",
                fill_time, last.timestamp,
                limit_buy_price, fill_price, last.price,
                shares, mid_at_entry, imbalance_at_entry, secs_at_entry,
            ))

        if not result.trades:
            if not ever_in_zone:
                result.skipped_reason = "price_never_in_zone"
            elif not ever_imbalance_ok:
                result.skipped_reason = "imbalance_never_met"
            elif state == "BUY_ORDER_PLACED":
                result.skipped_reason = "limit_never_filled"
            else:
                result.skipped_reason = "conditions_met_too_late"
        return result

    def _make_trade(
        self,
        market: HistoricalMarket,
        reason: str,
        entry_time: datetime,
        exit_time: datetime,
        entry_price: float,
        fill_price: float,
        exit_price: float,
        shares: float,
        mid: float,
        imbalance: float,
        secs: float,
    ) -> BacktestTrade:
        cost = fill_price * shares
        proceeds = exit_price * shares
        pnl = proceeds - cost

        return BacktestTrade(
            market_slug=market.slug,
            market_start=market.start_time,
            market_end=market.end_time,
            entry_time=entry_time,
            exit_time=exit_time,
            entry_price=entry_price,
            fill_price=fill_price,
            exit_price=exit_price,
            exit_reason=reason,
            shares=shares,
            cost_usdc=round(cost, 4),
            proceeds_usdc=round(proceeds, 4),
            pnl_usdc=round(pnl, 4),
            pnl_pct=round(pnl / cost * 100, 2) if cost > 0 else 0.0,
            mid_at_entry=mid,
            imbalance_at_entry=round(imbalance, 3),
            seconds_to_expiry_at_entry=round(secs, 1),
        )
