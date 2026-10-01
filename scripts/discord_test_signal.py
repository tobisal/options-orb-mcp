"""Post a one-shot test SIGNAL line to DISCORD_LOG_CHANNEL_ID.

Usage (from repo root, with .env loaded):
  python -m scripts.discord_test_signal
"""

from __future__ import annotations

import asyncio
import os
import ssl

import aiohttp
import certifi

from core.config import get_settings


def _ssl_context() -> ssl.SSLContext:
    cafile = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", cafile)
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=cafile)
    return ctx


async def main() -> None:
    settings = get_settings()
    token = (settings.discord_bot_token or "").strip()
    channel_raw = (settings.discord_log_channel_id or "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is empty in .env")
    if not channel_raw.isdigit():
        raise SystemExit(
            f"DISCORD_LOG_CHANNEL_ID must be a numeric channel id, got {channel_raw!r}"
        )
    channel_id = int(channel_raw)
    msg = (
        "SIGNAL [ASIA] MES LONG entry 7740.25 SL 7711.75 pd_high 7767.75 "
        "(test — ignore)"
    )
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    connector = aiohttp.TCPConnector(ssl=_ssl_context())
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.post(url, headers=headers, json={"content": msg}) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise SystemExit(f"Discord HTTP {resp.status}: {body}")
            print(f"Posted to channel {channel_id}: {msg}")


if __name__ == "__main__":
    asyncio.run(main())
