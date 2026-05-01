"""
market_finder.py -- Discovers active 5-minute BTC resolution markets.

Uses Polymarket's Gamma API (REST) rather than the CLOB API because Gamma
exposes market metadata (end_date, slug, tags) that CLOB doesn't surface
easily. No authentication is required for these read-only queries.

Typical call pattern:
    markets = await find_active_btc_5min_markets(session)

The returned dicts are ready to pass directly to MarketDataConsumer and
ScalpingStrategy.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

# Gamma API query params
_GAMMA_EVENTS_URL = f"{config.GAMMA_API}/events"
_GAMMA_MARKETS_URL = f"{config.GAMMA_API}/markets"

# Keywords used to identify BTC markets.
_BTC_KEYWORDS = ("bitcoin", "btc")

# Slug fragments that identify a 5-minute interval market.
_5MIN_MARKERS = ("5m", "5min", "updown-5m", "5-min", "5minute")

# A market is considered eligible if it resolves within this window.
_MAX_DURATION_SECONDS = config.MARKET_MAX_REMAINING_SECONDS  # default 600 s = 10 min


def _is_btc_market(event: dict) -> bool:
    """Returns True if the event title / slug looks like a BTC market."""
    title = (event.get("title") or "").lower()
    slug = (event.get("slug") or "").lower()
    return any(kw in title or kw in slug for kw in _BTC_KEYWORDS)


def _parse_end_time(end_date_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 end_date string from Gamma API into a UTC datetime."""
    if not end_date_str:
        return None
    # Strip trailing 'Z' and parse; assume UTC.
    clean = end_date_str.rstrip("Z").replace(" ", "T")
    try:
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        logger.warning("Could not parse end_date: %s", end_date_str)
        return None


def _parse_json_field(value) -> list:
    """
    Gamma API returns many list fields as JSON-encoded strings.
    e.g.  clobTokenIds = '["id1","id2"]'
          outcomes     = '["Yes","No"]'
    This helper normalises both cases to a plain Python list.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json as _json
        try:
            result = _json.loads(value)
            return result if isinstance(result, list) else []
        except Exception:
            return []
    return []


def _extract_yes_token_id(market: dict) -> Optional[str]:
    """
    Return the token_id for the affirmative outcome ('Yes' / 'Up').
    Index 0 in clobTokenIds corresponds to the first entry in outcomes.
    """
    tokens = _parse_json_field(market.get("clobTokenIds"))
    outcomes = _parse_json_field(market.get("outcomes"))

    if not tokens:
        return None

    # Find the index of the affirmative outcome.
    for i, outcome in enumerate(outcomes):
        if str(outcome).lower() in ("yes", "up") and i < len(tokens):
            token = str(tokens[i]).strip()
            if token:
                return token

    # Fallback: first token.
    return str(tokens[0]).strip() or None


async def find_active_btc_5min_markets(
    session: aiohttp.ClientSession,
) -> list[dict]:
    """
    Query Gamma API for active, unresolved events and return a list of market
    dicts for BTC 5-minute markets that are still open to trade.

    Each returned dict contains:
        condition_id   str   -- used to subscribe on the User WS channel
        token_id       str   -- Yes-token id for the CLOB order book
        end_time       datetime (UTC)
        tick_size      float
        slug           str   -- for logging
    """
    results: list[dict] = []

    params = {
        "active": "true",
        "closed": "false",
        "limit": 100,
        "offset": 0,
    }

    while True:
        try:
            async with session.get(
                _GAMMA_EVENTS_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
                # Explicitly opt out of brotli -- aiohttp doesn't support br by default.
                headers={"Accept-Encoding": "gzip, deflate"},
            ) as resp:
                resp.raise_for_status()
                events: list[dict] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            logger.error("Gamma API request failed: %s", exc)
            break

        if not events:
            break  # no more pages

        now_utc = datetime.now(tz=timezone.utc)

        for event in events:
            if not _is_btc_market(event):
                continue

            event_slug = (event.get("slug") or event.get("ticker") or "").lower()
            event_title = (event.get("title") or "").lower()

            # Require a 5-minute marker in the event slug or title.
            is_5min = any(m in event_slug or m in event_title for m in _5MIN_MARKERS)

            # Each event may contain multiple markets (e.g. "BTC > $X").
            # We want only those resolving within the 5-min window.
            markets_raw = event.get("markets") or []
            for mkt in markets_raw:
                end_time = _parse_end_time(mkt.get("endDate") or event.get("endDate"))
                if end_time is None:
                    continue

                remaining = (end_time - now_utc).total_seconds()

                # For 5m-marker markets, accept anything resolving within 10 min.
                # For other BTC markets (week/month long), accept only if near expiry.
                if is_5min:
                    if not (0 < remaining <= _MAX_DURATION_SECONDS):
                        continue
                else:
                    # Non-5m BTC markets: skip unless very close to resolution
                    # (within 8 min) -- may still hit the Golden Zone.
                    if not (0 < remaining <= 480):
                        continue

                token_id = _extract_yes_token_id(mkt)
                if not token_id:
                    logger.debug("No Yes token found for market %s", mkt.get("id"))
                    continue

                tick_size = float(
                    mkt.get("orderPriceMinTickSize")
                    or mkt.get("minimumTickSize")
                    or 0.01
                )
                condition_id = mkt.get("conditionId") or mkt.get("id") or ""
                slug = event_slug or mkt.get("slug", "")

                results.append(
                    {
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "end_time": end_time,
                        "tick_size": tick_size,
                        "slug": slug,
                        "is_5min": is_5min,
                    }
                )
                logger.info(
                    "Found BTC market: slug=%s 5m=%s token=…%s ends_in=%.0fs",
                    slug,
                    is_5min,
                    token_id[-8:],
                    remaining,
                )

        # Paginate if we got a full page.
        if len(events) < params["limit"]:
            break
        params["offset"] += params["limit"]

    logger.info("Market discovery complete -- %d tradeable markets found.", len(results))
    return results
