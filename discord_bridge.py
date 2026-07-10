"""
Discord webhook delivery for TNL Trader signals.
Mirrors the exact same content sent to Telegram — no separate message path.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
_CHUNK_LIMIT = 1900  # Discord hard limit is 2000; stay 100 chars under


async def send_to_discord(message: str) -> None:
    """POST message to Discord webhook. Never raises — errors are logged only."""
    url = _DISCORD_WEBHOOK_URL
    if not url:
        return

    chunks = _split(message)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for chunk in chunks:
                await _post_chunk(client, url, chunk)
    except Exception as exc:
        logger.error(f"[discord] unexpected error: {exc}")


def _split(message: str) -> list[str]:
    if len(message) <= _CHUNK_LIMIT:
        return [message]
    return [message[:_CHUNK_LIMIT], message[_CHUNK_LIMIT:]]


async def _post_chunk(client: httpx.AsyncClient, url: str, chunk: str) -> None:
    resp = await client.post(url, json={"content": chunk}, headers={"Content-Type": "application/json"})

    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 1.0)
        logger.warning(f"[discord] rate-limited — waiting {retry_after}s before retry")
        await asyncio.sleep(retry_after)
        resp = await client.post(url, json={"content": chunk}, headers={"Content-Type": "application/json"})

    if resp.status_code not in (200, 204):
        logger.error(f"[discord] non-2xx response {resp.status_code}: {resp.text[:200]}")
