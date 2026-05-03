"""
backtest/data_fetcher.py — Historical market and price data fetching.

Data sources (in order of preference):
  1. data-api.polymarket.com/trades  — tick-level trade history per market
  2. gamma-api.polymarket.com/events — resolved BTC 5m market discovery
  3. Local JSON cache                — avoids re-fetching on repeated runs

All requests use Accept-Encoding: gzip, deflate to avoid the Brotli issue.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CLOB_API  = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").rstrip("/")

SAFE_HEADERS = {"Accept-Encoding": "gzip, deflate"}

# Cache directory for raw API responses.
CACHE_DIR = Path(__file__).parent / ".cache"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TradeTick:
    """Single trade (fill) from the market's trade history."""
    timestamp: datetime
    price: float
    size: float
    side: str           # "BUY" (taker bought) | "SELL" (taker sold)


@dataclass
class HistoricalMarket:
    """All data needed to replay strategy logic for one resolved market."""
    slug: str
    condition_id: str
    token_id: str           # "Up"/"Yes" token
    start_time: datetime
    end_time: datetime
    tick_size: float
    resolved_up: Optional[bool]   # True if "Up"/"Yes" won; None if unknown
    ticks: list[TradeTick] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def _load_cache(key: str) -> Optional[list | dict]:
    p = _cache_path(key)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return None


def _save_cache(key: str, data: list | dict) -> None:
    try:
        _cache_path(key).write_text(json.dumps(data, default=str))
    except Exception as exc:
        logger.debug("Cache write failed: %s", exc)


# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------

def _parse_json_field(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            r = json.loads(value)
            return r if isinstance(r, list) else []
        except Exception:
            return []
    return []


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.rstrip("Z").replace(" ", "T"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


async def find_resolved_btc_markets(
    session: aiohttp.ClientSession,
    days_back: int = 7,
    max_markets: int = 500,
    require_5min: bool = True,
) -> list[HistoricalMarket]:
    """
    Return resolved BTC Up/Down markets from the past `days_back` days.

    Uses two strategies:
      a) Pagination through Gamma /events ordered by endDate descending.
      b) Slug-based direct lookup for known btc-updown-5m-<timestamp> slugs.
    Strategy (b) is fast and reliable for 5-min markets; (a) catches anything
    that strategy (b) might miss (e.g. 15m markets if require_5min=False).
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days_back)
    markets: dict[str, HistoricalMarket] = {}   # keyed by condition_id

    # --- Strategy A: timestamp-slug enumeration for 5m markets ----------
    if require_5min:
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        # BTC 5m markets resolve at :00, :05, :10, ... UTC
        last_boundary = (now_ts // 300) * 300
        slugs_to_try = [
            f"btc-updown-5m-{last_boundary - i * 300}"
            for i in range(1, days_back * 288 + 1)   # 288 markets per day
        ]

        logger.info("Strategy A: checking %d candidate 5m slugs...", len(slugs_to_try))
        batch_size = 20
        for i in range(0, min(len(slugs_to_try), max_markets * 2), batch_size):
            if len(markets) >= max_markets:
                break
            batch = slugs_to_try[i : i + batch_size]
            tasks = [_fetch_event_by_slug(session, slug) for slug in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for ev in results:
                if isinstance(ev, Exception) or ev is None:
                    continue
                m = _event_to_historical_market(ev)
                if m and m.end_time >= cutoff and m.condition_id not in markets:
                    markets[m.condition_id] = m

        logger.info("Strategy A found %d resolved 5m BTC markets.", len(markets))

    # --- Strategy B: paginate /events ordered by endDate desc -----------
    if len(markets) < max_markets:
        logger.info("Strategy B: paginating Gamma /events...")
        offset = 0
        while len(markets) < max_markets:
            try:
                async with session.get(
                    f"{GAMMA_API}/events",
                    params={
                        "closed": "true",
                        "limit": 200,
                        "offset": offset,
                        "order": "endDate",
                        "ascending": "false",
                    },
                    headers=SAFE_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    resp.raise_for_status()
                    events = await resp.json(content_type=None)
            except Exception as exc:
                logger.warning("Gamma /events page failed: %s", exc)
                break

            if not events:
                break

            earliest_in_page = None
            for ev in events:
                slug = (ev.get("slug") or ev.get("ticker") or "").lower()
                if "btc" not in slug or "updown" not in slug:
                    continue
                if require_5min and "5m" not in slug:
                    continue
                m = _event_to_historical_market(ev)
                if m and m.end_time >= cutoff and m.condition_id not in markets:
                    markets[m.condition_id] = m
                if m:
                    earliest_in_page = m.end_time

            # Stop paginating if we've gone past our cutoff.
            if earliest_in_page and earliest_in_page < cutoff:
                break
            offset += 200

        logger.info("After Strategy B: %d total resolved markets.", len(markets))

    return list(markets.values())


async def _fetch_event_by_slug(
    session: aiohttp.ClientSession,
    slug: str,
) -> Optional[dict]:
    cache_key = f"event_{slug}"
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached[0] if cached else None

    try:
        async with session.get(
            f"{GAMMA_API}/events",
            params={"slug": slug},
            headers=SAFE_HEADERS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 404:
                _save_cache(cache_key, [])
                return None
            resp.raise_for_status()
            events = await resp.json(content_type=None)
            if events:
                _save_cache(cache_key, events[:1])
                return events[0]
            _save_cache(cache_key, [])
    except Exception:
        pass
    return None


def _event_to_historical_market(ev: dict) -> Optional[HistoricalMarket]:
    """Extract HistoricalMarket from a Gamma API event dict."""
    markets_raw = ev.get("markets") or []
    if not markets_raw:
        return None

    mkt = markets_raw[0]

    cid = mkt.get("conditionId") or mkt.get("id") or ""
    if not cid:
        return None

    tokens = _parse_json_field(mkt.get("clobTokenIds"))
    outcomes = _parse_json_field(mkt.get("outcomes"))
    prices = _parse_json_field(mkt.get("outcomePrices"))

    if not tokens:
        return None

    # Pick the "Up"/"Yes" token.
    token_id = None
    resolved_up = None
    for i, outcome in enumerate(outcomes):
        if str(outcome).lower() in ("yes", "up") and i < len(tokens):
            token_id = str(tokens[i]).strip()
            if i < len(prices):
                try:
                    p = float(prices[i])
                    resolved_up = p > 0.5
                except (ValueError, TypeError):
                    pass
            break

    if not token_id:
        token_id = str(tokens[0]).strip()

    end_time = _parse_dt(mkt.get("endDate") or ev.get("endDate"))
    start_time = _parse_dt(mkt.get("startDate") or ev.get("startDate"))
    if not end_time:
        return None
    if not start_time:
        # Infer: 5m market = end - 5 min
        start_time = end_time - timedelta(minutes=5)

    tick_size = float(mkt.get("orderPriceMinTickSize") or mkt.get("minimumTickSize") or 0.01)
    slug = ev.get("slug") or ev.get("ticker") or ""

    return HistoricalMarket(
        slug=slug,
        condition_id=cid,
        token_id=token_id,
        start_time=start_time,
        end_time=end_time,
        tick_size=tick_size,
        resolved_up=resolved_up,
    )


# ---------------------------------------------------------------------------
# Trade history fetching
# ---------------------------------------------------------------------------

async def fetch_trades(
    session: aiohttp.ClientSession,
    market: HistoricalMarket,
    limit: int = 2000,
) -> list[TradeTick]:
    """
    Fetch trade-level price history for a market.

    Tries:
      1. data-api /trades?market=<condition_id>   (tick data, no auth needed)
      2. CLOB /trades?token_id=<token_id>          (requires auth)
    """
    cache_key = f"trades_{market.condition_id}"
    cached = _load_cache(cache_key)
    if cached is not None:
        return _parse_trade_list(cached, market)

    ticks: list[TradeTick] = []

    # Attempt 1: data-api
    try:
        all_raw: list[dict] = []
        next_cursor = None
        while True:
            params: dict = {
                "market": market.condition_id,
                "limit": min(limit, 500),
            }
            if next_cursor:
                params["cursor"] = next_cursor

            async with session.get(
                f"{DATA_API}/trades",
                params=params,
                headers=SAFE_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status not in (200, 201):
                    raise ValueError(f"data-api status {resp.status}")
                data = await resp.json(content_type=None)

            records = (
                data if isinstance(data, list)
                else data.get("data", data.get("results", []))
            )
            all_raw.extend(records)
            next_cursor = data.get("next") if isinstance(data, dict) else None
            if not next_cursor or len(all_raw) >= limit:
                break

        if all_raw:
            _save_cache(cache_key, all_raw)
            return _parse_trade_list(all_raw, market)

    except Exception as exc:
        logger.debug("data-api trades failed for %s: %s", market.slug, exc)

    # Attempt 2: CLOB trades (may need auth — will silently fail if 401)
    try:
        async with session.get(
            f"{CLOB_API}/trades",
            params={"token_id": market.token_id, "limit": 500},
            headers=SAFE_HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                records = data if isinstance(data, list) else data.get("data", [])
                if records:
                    _save_cache(cache_key, records)
                    return _parse_trade_list(records, market)
    except Exception as exc:
        logger.debug("CLOB trades failed for %s: %s", market.slug, exc)

    logger.warning("No trade data found for %s — will use synthetic ticks.", market.slug)
    return []


def _parse_trade_list(
    raw: list[dict],
    market: HistoricalMarket,
) -> list[TradeTick]:
    """Normalise raw API records into TradeTick objects within the market window."""
    ticks: list[TradeTick] = []
    for rec in raw:
        ts_raw = rec.get("timestamp") or rec.get("created_at") or rec.get("time")
        price_raw = rec.get("price") or rec.get("lastPrice")
        size_raw = rec.get("size") or rec.get("amount") or rec.get("quantity")
        side_raw = (rec.get("side") or rec.get("takerSide") or "BUY").upper()

        if ts_raw is None or price_raw is None:
            continue

        # Parse timestamp — could be ISO string or Unix ms/s integer.
        ts: Optional[datetime] = None
        if isinstance(ts_raw, (int, float)):
            epoch = ts_raw / 1000 if ts_raw > 1e12 else ts_raw
            ts = datetime.fromtimestamp(epoch, tz=timezone.utc)
        else:
            ts = _parse_dt(str(ts_raw))

        if ts is None:
            continue

        # Filter to market window only.
        if not (market.start_time <= ts <= market.end_time + timedelta(seconds=30)):
            continue

        try:
            price = float(price_raw)
            size = float(size_raw or 0)
        except (ValueError, TypeError):
            continue

        if not (0 < price < 1):
            continue

        ticks.append(TradeTick(timestamp=ts, price=price, size=size, side=side_raw))

    # Sort chronologically.
    ticks.sort(key=lambda t: t.timestamp)
    return ticks


# ---------------------------------------------------------------------------
# Bulk fetch with concurrency limit
# ---------------------------------------------------------------------------

async def fetch_all_trade_histories(
    session: aiohttp.ClientSession,
    markets: list[HistoricalMarket],
    concurrency: int = 8,
) -> None:
    """
    Populate market.ticks for each market in-place, with bounded concurrency
    to avoid overwhelming the API.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _fetch_one(m: HistoricalMarket) -> None:
        async with sem:
            m.ticks = await fetch_trades(session, m)

    await asyncio.gather(*[_fetch_one(m) for m in markets])
    total_ticks = sum(len(m.ticks) for m in markets)
    logger.info(
        "Fetched trade histories: %d markets, %d total ticks.",
        len(markets),
        total_ticks,
    )
