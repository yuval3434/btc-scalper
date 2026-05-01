"""
notifier.py -- Phone notifications via Telegram Bot API.

Setup (one-time):
  1. Open Telegram, search @BotFather, send /newbot, follow the prompts.
     Copy the token it gives you (looks like 123456:ABC-DEF...).
  2. Search @userinfobot in Telegram and send /start -- it replies with your
     numeric chat ID (e.g. 987654321).
  3. Add to your .env:
       TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
       TELEGRAM_CHAT_ID=987654321
  4. Message your new bot once (just say /start) so it can send you messages.

If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set, all calls silently no-op.
"""

import logging
import aiohttp

import config

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot"
_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def send(text: str) -> None:
    """Fire-and-forget Telegram message. Errors are logged but never raised."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    url = f"{_API_BASE}{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Telegram notification failed (%d): %s", resp.status, body[:200])
    except Exception as exc:
        logger.warning("Telegram notification error: %s", exc)
