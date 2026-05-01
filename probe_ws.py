"""
probe_ws.py — One-shot WebSocket probe for diagnosing the Polymarket Market Channel.

Connects to the market WS, subscribes to the first BTC token found via
market_finder, and prints the first 10 raw frames to stdout.

Usage:
    python probe_ws.py

No credentials needed — the market channel is public.
"""

import asyncio
import json
import sys

import aiohttp
import websockets

sys.path.insert(0, ".")  # ensure local modules take precedence
from dotenv import load_dotenv
load_dotenv()

import config
from market_finder import find_active_btc_5min_markets

MARKET_WS = config.MARKET_WS


async def _dump_raw_gamma() -> None:
    """Print the raw Gamma API response for the first BTC event so we can inspect field names."""
    import aiohttp as _aiohttp
    url = "https://gamma-api.polymarket.com/events"
    params = {"active": "true", "closed": "false", "limit": 5}
    async with _aiohttp.ClientSession() as s:
        async with s.get(url, params=params, headers={"Accept-Encoding": "gzip, deflate"}) as r:
            data = await r.json(content_type=None)
    for ev in data:
        title = (ev.get("title") or ev.get("slug") or "").lower()
        if any(kw in title for kw in ("btc", "bitcoin")):
            print("=== FIRST BTC EVENT (raw) ===")
            print(json.dumps(ev, indent=2, default=str)[:4000])
            print()
            if ev.get("markets"):
                print("=== FIRST MARKET (raw) ===")
                print(json.dumps(ev["markets"][0], indent=2, default=str)[:2000])
            return
    print("No BTC event in first 5 results. Printing first event instead:")
    print(json.dumps(data[0] if data else {}, indent=2, default=str)[:3000])


async def main() -> None:
    print("=== RAW GAMMA API DUMP ===")
    await _dump_raw_gamma()
    print()

    # Also print all BTC-slug markets near expiry to confirm token ordering
    print("=== SEARCHING RAW GAMMA FOR BTC UP/DOWN MARKETS ===")
    async with aiohttp.ClientSession() as _s:
        params = {"active": "true", "closed": "false", "limit": 100}
        async with _s.get(
            "https://gamma-api.polymarket.com/events",
            params=params,
            headers={"Accept-Encoding": "gzip, deflate"},
        ) as r:
            events = await r.json(content_type=None)
        for ev in events:
            slug = (ev.get("slug") or ev.get("ticker") or "").lower()
            if "btc" not in slug and "bitcoin" not in slug:
                continue
            if "updown" not in slug:
                continue
            for mkt in (ev.get("markets") or []):
                end_raw = mkt.get("endDate") or ev.get("endDate")
                if not end_raw:
                    continue
                from datetime import datetime, timezone as _tz
                end_str = end_raw.rstrip("Z").replace(" ", "T")
                end_dt = datetime.fromisoformat(end_str).replace(tzinfo=_tz.utc)
                rem = (end_dt - datetime.now(tz=_tz.utc)).total_seconds()
                if not (0 < rem < 900):
                    continue
                print(f"\nslug={slug}  ends_in={rem:.0f}s")
                print(f"  outcomes:     {mkt.get('outcomes')}")
                print(f"  clobTokenIds: {mkt.get('clobTokenIds')}")
                print(f"  outcomePrices:{mkt.get('outcomePrices')}")

    print()
    print(f"Probing {MARKET_WS}\n")

    async with aiohttp.ClientSession() as session:
        markets = await find_active_btc_5min_markets(session)

    if not markets:
        print("No active BTC 5-min markets found via Gamma API.")
        print("Check that the Gamma API is returning results and that the BTC filter matches.")
        return

    market = markets[0]
    token_id = market["token_id"]
    print(f"Subscribing to token_id: {token_id}")
    print(f"  slug:     {market['slug']}")
    print(f"  end_time: {market['end_time']}\n")

    sub_msg = json.dumps({
        "assets_ids": [token_id],
        "type": "market",
        "custom_feature_enabled": True,
    })

    print(f"Subscription message: {sub_msg}\n")
    print("--- Raw frames (first 10) ---")

    try:
        async with websockets.connect(MARKET_WS, ping_interval=None) as ws:
            await ws.send(sub_msg)
            frame_count = 0
            async for raw in ws:
                frame_count += 1
                print(f"\n[Frame {frame_count}]")
                if isinstance(raw, bytes):
                    raw = raw.decode()
                print(raw[:1000])

                if frame_count >= 10:
                    break
    except Exception as exc:
        print(f"\nWebSocket error: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
