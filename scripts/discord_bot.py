"""Discord remote control for the Options ORB dashboard.

Talks to the local dashboard HTTP API so auto-trade stays in one process.
Slash commands report back immediately; a log pump streams auto-trader events
into DISCORD_LOG_CHANNEL_ID.

    python -m scripts.discord_bot

Create a bot at https://discord.com/developers/applications — enable
Privileged Gateway Intent is not required. Invite with `bot` +
`applications.commands`. Put the token and your Discord user id in `.env`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from typing import Any
from urllib.parse import urlencode

import aiohttp
import certifi

from core.config import get_settings
from core.discord_format import (
    clip,
    format_log_line,
    format_nightly,
    format_optimise,
    format_positions,
    format_preview,
    format_signals,
    format_status,
    format_trades,
    should_relay_log,
)
from core.nightly import run_nightly_optimise

log = logging.getLogger("orb.discord")

try:
    import discord
    from discord import app_commands
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "discord.py is not installed. Activate .venv and run: pip install -e ."
    ) from exc


def _ssl_context() -> ssl.SSLContext:
    """Trust Mozilla CAs via certifi, plus the Windows store.

    Python 3.14 on this machine loads only a thin Windows CA set, so Discord's
    Google Trust Services chain fails with CERTIFICATE_VERIFY_FAILED. Loading
    certifi's bundle fixes that without turning verification off.
    """
    cafile = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", cafile)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=cafile)
    return ctx


class DashboardClient:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._request("GET", path, params)

    async def post(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._request("POST", path, params)

    async def _request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Dashboard client is not started.")
        clean = {k: v for k, v in params.items() if v is not None}
        url = self.base + path
        if clean:
            url = f"{url}?{urlencode(clean)}"
        try:
            async with self._session.request(method, url) as resp:
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return {"ok": False, "error": f"Unexpected response ({resp.status})."}
                if resp.status >= 400:
                    data.setdefault("error", f"HTTP {resp.status}")
                return data
        except aiohttp.ClientConnectionError:
            return {
                "ok": False,
                "error": f"Dashboard is not reachable at {self.base}. "
                "Start it with: python -m dashboard.app",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _guild_object(raw: str) -> discord.Object | None:
    raw = (raw or "").strip()
    if raw.isdigit():
        return discord.Object(id=int(raw))
    return None


class OrbDiscord(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.settings = get_settings()
        self.api = DashboardClient(self.settings.discord_dashboard_url)
        self._seen_logs: set[str] = set()
        self._log_primed = False
        self._owner_id: int | None = None
        self._pump_task: asyncio.Task | None = None

    async def login(self, token: str) -> None:
        # TCPConnector needs a running loop, so this cannot live in __init__.
        if not isinstance(self.http.connector, aiohttp.BaseConnector):
            self.http.connector = aiohttp.TCPConnector(ssl=_ssl_context(), limit=0)
        await super().login(token)

    def _allowed(self, user_id: int) -> bool:
        allow = self.settings.discord_allowlist()
        if allow:
            return user_id in allow
        return self._owner_id is not None and user_id == self._owner_id

    async def setup_hook(self) -> None:
        await self.api.start()
        register_commands(self)
        guild = _guild_object(self.settings.discord_guild_id)
        if guild is not None:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %s guild commands", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Synced %s global commands (can take up to an hour)", len(synced))
        self._pump_task = asyncio.create_task(self._log_pump())

    async def close(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
        await self.api.close()
        await super().close()

    async def on_ready(self) -> None:
        app = await self.application_info()
        self._owner_id = app.owner.id if app.owner else None
        log.info("Logged in as %s", self.user)
        if not self.settings.discord_allowlist() and self._owner_id:
            log.info("No DISCORD_ALLOWED_USER_IDS set; allowing bot owner %s only", self._owner_id)

    async def _log_pump(self) -> None:
        await self.wait_until_ready()
        raw = (self.settings.discord_log_channel_id or "").strip()
        if not raw.isdigit():
            return
        channel_id = int(raw)
        verbose = self.settings.discord_log_verbose
        while not self.is_closed():
            try:
                # Prefer shared ops log (trail/session/autotrade); fall back to autotrade.
                status = await self.api.get("/api/ops/logs", limit=80)
                if status.get("error"):
                    status = await self.api.get("/api/autotrade/status")
                if status.get("error"):
                    await asyncio.sleep(15)
                    continue
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)
                logs = list(reversed(status.get("log") or []))
                if not self._log_primed:
                    for entry in logs:
                        if isinstance(entry, dict):
                            self._seen_logs.add(f"{entry.get('t')}|{entry.get('msg')}")
                    self._log_primed = True
                    await asyncio.sleep(8)
                    continue
                for entry in logs:
                    if not isinstance(entry, dict):
                        continue
                    key = f"{entry.get('t')}|{entry.get('msg')}"
                    if key in self._seen_logs:
                        continue
                    self._seen_logs.add(key)
                    if len(self._seen_logs) > 400:
                        self._seen_logs = set(list(self._seen_logs)[-200:])
                    if not should_relay_log(entry, verbose=verbose):
                        continue
                    await channel.send(clip(format_log_line(entry), 1800))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("log pump: %s", exc)
            await asyncio.sleep(8)


def register_commands(bot: OrbDiscord) -> None:
    tree = bot.tree

    async def guard(interaction: discord.Interaction) -> bool:
        if bot._allowed(interaction.user.id):
            return True
        await interaction.response.send_message(
            "You are not on DISCORD_ALLOWED_USER_IDS.", ephemeral=True
        )
        return False

    @tree.command(name="help", description="List ORB Discord commands")
    async def help_cmd(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.send_message(
            clip(
                "**ORB remote** (paper). Dashboard must be running.\n"
                "`/status` account + auto-trade\n"
                "`/signals` current ORB per window\n"
                "`/preview` size a spread, does not place\n"
                "`/positions` open mark-to-market\n"
                "`/trades` journal\n"
                "`/auto start|stop|status` paper auto-trader\n"
                "`/optimise` rank params (does not apply)\n"
                "`/nightly` rank and apply tomorrow's sets\n"
                "Live auto-trade + trail/session events stream to DISCORD_LOG_CHANNEL_ID."
            ),
            ephemeral=True,
        )

    @tree.command(name="status", description="Paper equity, IBKR, auto-trade")
    async def status_cmd(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        summary, auto = await asyncio.gather(
            bot.api.get("/api/summary"),
            bot.api.get("/api/autotrade/status"),
        )
        if summary.get("error"):
            await interaction.followup.send(clip(str(summary["error"])))
            return
        await interaction.followup.send(format_status(summary, auto if not auto.get("error") else None))

    @tree.command(name="signals", description="ORB read for Asia / London / New York")
    @app_commands.describe(symbol="Underlying, default SPY")
    async def signals_cmd(interaction: discord.Interaction, symbol: str = "SPY") -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await bot.api.get("/api/signals", symbol=symbol.upper())
        await interaction.followup.send(format_signals(data))

    @tree.command(name="preview", description="Preview a defined-risk vertical (no order)")
    @app_commands.describe(window="auto, asia, london, or new_york", symbol="Underlying")
    async def preview_cmd(
        interaction: discord.Interaction,
        window: str = "auto",
        symbol: str = "SPY",
    ) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await bot.api.get(
            "/api/preview", symbol=symbol.upper(), window=window.lower()
        )
        await interaction.followup.send(format_preview(data))

    @tree.command(name="positions", description="Open trades with live mark-to-market")
    async def positions_cmd(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await bot.api.get("/api/positions")
        if data.get("error"):
            await interaction.followup.send(clip(str(data["error"])))
            return
        await interaction.followup.send(format_positions(data))

    @tree.command(name="trades", description="Recent paper journal")
    async def trades_cmd(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await bot.api.get("/api/trades", limit="20")
        if data.get("error"):
            await interaction.followup.send(clip(str(data["error"])))
            return
        await interaction.followup.send(format_trades(data))

    auto = app_commands.Group(name="auto", description="Paper auto-trader on the dashboard")

    @auto.command(name="status", description="Is the auto-trader running?")
    async def auto_status(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await bot.api.get("/api/autotrade/status")
        if data.get("error"):
            await interaction.followup.send(clip(str(data["error"])))
            return
        lines = [format_status({}, data)]
        for entry in (data.get("log") or [])[:8]:
            if isinstance(entry, dict):
                lines.append(format_log_line(entry))
        await interaction.followup.send(clip("\n".join(lines)))

    @auto.command(name="start", description="Start paper auto-trade (dashboard process)")
    @app_commands.describe(symbol="Underlying", window="auto / asia / london / new_york")
    async def auto_start(
        interaction: discord.Interaction,
        symbol: str = "SPY",
        window: str = "auto",
    ) -> None:
        if not await guard(interaction):
            return
        if bot.settings.trading_environment() == "LIVE":
            await interaction.response.send_message(
                "Refusing: ACCOUNT_MODE is LIVE. Discord only starts paper auto-trade.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        data = await bot.api.post(
            "/api/autotrade/start",
            symbol=symbol.upper(),
            window=window.lower(),
            demo="false",
            interval="60",
        )
        if not data.get("ok") and data.get("error"):
            await interaction.followup.send(clip(str(data["error"])))
            return
        await interaction.followup.send(
            clip("Auto-trade started.\n" + format_status({}, data))
        )

    @auto.command(name="stop", description="Stop paper auto-trade")
    async def auto_stop(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await bot.api.post("/api/autotrade/stop")
        await interaction.followup.send(clip("Auto-trade stopped.\n" + format_status({}, data)))

    tree.add_command(auto)

    @tree.command(name="optimise", description="Grid-search one window (does not apply)")
    @app_commands.describe(window="asia, london, or new_york", lookback_days="History length")
    async def optimise_cmd(
        interaction: discord.Interaction,
        window: str = "new_york",
        lookback_days: int = 60,
    ) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(
            f"Optimising {window.replace('_', ' ')} over {lookback_days}d — I'll reply when ranked."
        )
        data = await bot.api.get(
            "/api/optimise",
            window=window.lower(),
            lookback_days=str(lookback_days),
            top_n="5",
        )
        await interaction.followup.send(format_optimise(data))

    @tree.command(name="nightly", description="Rank all windows and apply the #1 sets")
    @app_commands.describe(dry_run="Rank only, do not change trading params")
    async def nightly_cmd(interaction: discord.Interaction, dry_run: bool = True) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(
            "Running nightly rank"
            + (" (dry run — not applying)." if dry_run else " and applying winners.")
        )
        report = await run_nightly_optimise(
            apply=not dry_run,
            progress=lambda msg: log.info("%s", msg),
        )
        await interaction.followup.send(format_nightly(report))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    token = (settings.discord_bot_token or "").strip()
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is empty. Create a bot in the Discord developer "
            "portal, then add the token to .env"
        )
    bot = OrbDiscord()
    bot.run(token)


if __name__ == "__main__":
    main()
