# Options ORB MCP System

A **demo-first** system of [MCP](https://modelcontextprotocol.io) servers that expose
market-analysis, research, optimisation, and execution tools to an LLM client
(Cursor / Claude Desktop). It trades **defined-risk options vertical spreads**
driven by an **Opening Range Breakout (ORB)** signal, optimised per session
window (Asia / London / New York), with strict risk controls sized for ~£1000
of capital.

> **Risk notice.** This is educational software, not financial advice. Trading
> options risks loss of capital. The system defaults to a **paper account** and
> refuses to place live orders unless you deliberately flip two independent
> safety switches. "Steady returns" is a design *goal*, never a guarantee.

## Why the design looks like this

- Your brief described ORB, session windows, SL/TP and MetaTrader. Real options
  (strikes/expiries/Greeks) don't live on MT5, so this uses **Interactive
  Brokers** (paper API + UK access + US options).
- £1000 means **defined-risk spreads only** (verticals). The ORB breakout on the
  *underlying* is the directional signal; the executor places the spread.
- Session windows are reframed for US options: **Asia** = overnight globex range,
  **London** = EU/pre-market, **New York** = the classic US-open ORB.

## Architecture

```
LLM client (Cursor / Claude)
      |  MCP (stdio)
      +-- market-data-mcp   (ORB signal, regime, option chain, IV)
      +-- research-mcp      (trade journal, performance, learning)
      +-- optimiser-mcp     (backtest, walk-forward, compare strategies)
      +-- execution-mcp     (preview/place spreads with bracket SL/TP)
                 |
              core/ library  <----  dashboard/ (read-only web GUI, port 8787)
                 |
        IB Gateway / TWS  (paper first)
```

## Prerequisites

1. **Python 3.11+** (3.13 tested).
2. A virtual environment with `pip` (steps below). [uv](https://docs.astral.sh/uv/)
   is supported as an optional alternative if you have it.
3. **Interactive Brokers** account with **paper trading** enabled, plus
   **TWS** or **IB Gateway** running with the API enabled
   (Configure -> API -> Settings -> "Enable ActiveX and Socket Clients").
   - Paper defaults: TWS `7497`, IB Gateway `4002`.
   - New to this? Follow the step-by-step [IBKR setup guide](docs/IBKR_SETUP.md)
     (install Gateway, log into paper, enable the API, free delayed data).

Paper trading uses **live IBKR data** by default; synthetic/"Demo data" is only
an explicit offline toggle. The backtester and pricing tools work **without**
IBKR - only live market data and order placement need Gateway/TWS running.
Paper accounts get **free 15-minute delayed data**, and the system falls back to
it automatically when you lack a real-time subscription.

## Setup

Run these from the repo root. The commands assume your virtual environment is
**activated** (so `python`/`pytest` resolve to the `.venv`).

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate            # PowerShell / CMD
# source .venv/Scripts/activate   # Git Bash on Windows
# source .venv/bin/activate       # macOS / Linux

# 2. Install the project + dev tools (pytest, ruff)
pip install -e ".[dev]"

# 3. Configure
copy .env.example .env            # Windows;  Unix: cp .env.example .env
# then edit IBKR_PORT (IB Gateway paper = 4002, TWS paper = 7497)

# 4. Register the MCP servers with your client (writes .cursor/mcp.json)
python -m scripts.setup

# 5. Sanity check the tests (no IBKR needed)
pytest -q

# 6. Watch the whole loop run offline on synthetic data (no IBKR needed)
python -m scripts.demo

# 7. Open the GUI dashboard (historic trades, strategy used, live positions)
python -m dashboard.app           # then open http://127.0.0.1:8787

# (once IB Gateway is running) confirm the live connection + a sample quote
python -m scripts.check_ibkr
```

The demo exercises every agent end to end: it finds an ORB breakout, risk-sizes a
defined-risk spread, simulates a paper fill, logs it, then backtests, optimises
and walk-forward-validates the strategy - all offline.

<details>
<summary>Using <code>uv</code> instead of pip (optional)</summary>

```bash
uv sync --extra dev            # install
uv run orb-setup               # = python -m scripts.setup
uv run pytest -q               # = pytest -q
uv run python -m scripts.demo  # = python -m scripts.demo
uv run orb-dashboard           # = python -m dashboard.app
uv run python -m scripts.check_ibkr
```

`orb-setup` and `orb-dashboard` are the console-script entry points defined in
`pyproject.toml`; they're available on your `PATH` after `pip install -e .` too.
</details>

## Dashboard (GUI)

A read-only web dashboard gives a clean view of everything at a glance:

```bash
python -m dashboard.app       # (uv: uv run orb-dashboard)
# open http://127.0.0.1:8787
```

It shows:

- **Account cards** - environment (PAPER/LIVE badge), IBKR connection, daily P&L,
  per-trade risk budget, open positions, and the daily kill-switch status.
- **Equity curve** - cumulative P&L of closed trades from your starting capital.
- **Live signals** - the current ORB read per session window (with a "Demo data"
  toggle so it works without IBKR).
- **Performance by window and by strategy** - win rate, expectancy, profit factor
  and total P&L, so you can see which ORB windows and which spread types work.
- **Auto-trading (play button)** - a start/stop control that runs the ORB entry
  loop automatically: each interval it evaluates the active session window and,
  if a qualifying breakout passes every risk gate, places a risk-sized spread
  (paper or simulated) and logs it. Entries use the parameter set you choose
  (optimiser ranking, optimisation history, or **Use these for trading** on the
  advanced parameters). Until you choose one, ``windows.json`` defaults apply.
  It enters up to 3 trades per session window
  (max 9 per day across Asia / London / New York), respects the per-trade cap
  and the daily kill-switch, and shows a live activity log. **Disabled for LIVE
  accounts** as a safety measure - paper/simulated only.
- **Backtesting & simulation** - pick a symbol, session window and lookback, then
  **Run backtest** to pull historical data (live IBKR history, or demo data
  offline) and simulate the ORB spread strategy. You get a simulated equity
  curve, full metrics (win rate, expectancy, profit factor, drawdown, Sharpe,
  Monte-Carlo) and every simulated trade. **Optimise** grid-searches the
  parameter space, ranks the top sets by the balanced score, and saves the best.
- **Optimisations made** - a history of every optimiser run with its best
  parameters and metrics, so you can see what has been tried and what won.
- **Open positions** - journal trades plus live IBKR positions when connected.
- **Trade history** - every trade with the strategy used (e.g. `bull call debit`,
  `bull put credit`), direction, size, max loss, status and realised P&L.

It refreshes every 8 seconds. Auto-trade (when you Start it) places paper
spreads through the dashboard process; other execution stays with the
execution agent. The view is populated from `data/trades.db`; delete that
file to reset to an empty journal.

## Discord remote control

The bot runs **on this PC** next to the dashboard and talks to
`http://127.0.0.1:8787`. Slash commands from your phone (or any Discord
client) then drive paper auto-trade and stream live events back.

1. Create an application at [discord.com/developers/applications](https://discord.com/developers/applications)
   → **Bot** → copy the token into `.env` as `DISCORD_BOT_TOKEN`.
2. OAuth2 → URL Generator: scopes **`bot`** and **`applications.commands`**,
   permission **Send Messages**. Open the URL and invite the bot to a server
   you own.
3. Discord **User Settings → Advanced → Developer Mode**. Right-click your
   avatar → **Copy User ID** → `DISCORD_ALLOWED_USER_IDS`. Right-click the
   server name → **Copy Server ID** → `DISCORD_GUILD_ID` (slash commands
   appear immediately). Optional: right-click a channel → **Copy Channel ID**
   → `DISCORD_LOG_CHANNEL_ID` for live fill / error / start / stop posts.
4. Keep the dashboard running, then in a second terminal:

```powershell
pip install -e ".[dev]"          # once, so discord.py is in .venv
python -m dashboard.app          # already running is fine; restart it once
python -m scripts.discord_bot    # or: orb-discord
```

Commands: `/help`, `/status`, `/signals`, `/preview`, `/positions`, `/trades`,
`/auto start|stop|status`, `/optimise` (ranks, does not apply), `/nightly`
(default dry-run). `/auto start` is refused if `ACCOUNT_MODE=LIVE`. There is
no Discord command that places a live IBKR order.

## Verifying the IBKR connection

With IB Gateway/TWS running and logged into the **paper** account:

```bash
python -m scripts.check_ibkr
```

It prints the resolved host/port/mode, connects, and fetches a sample quote plus
your account summary. If it fails, it tells you exactly what to check. Full
walkthrough in [docs/IBKR_SETUP.md](docs/IBKR_SETUP.md).

## Historical data (backtests)

IBKR only returns about a month of 5-minute bars per request. To cache a year of
SPY history locally (used by dashboard backtest / optimiser lookback **1 year**):

```bash
python -m scripts.fetch_history --symbol SPY --days 365
```

Bars are written to `data/history/SPY_5mins.csv`. Re-running the command reuses
the cache when it already covers the requested lookback.

## Running a server manually

Each server speaks MCP over stdio and is normally launched by the client, but
you can smoke-test one directly:

```bash
python -m servers.market_data_mcp.server    # (uv: uv run python -m ...)
```

## The four agents (MCP tool groups)

| Server | Purpose | Key tools |
| --- | --- | --- |
| `market-data-mcp` | Market analysis | `get_session_orb`, `classify_regime`, `get_option_chain`, `get_iv` |
| `research-mcp` | Learn from history | `log_trade`, `query_trades`, `performance_report`, `learn_from_history` |
| `optimiser-mcp` | Test & compare strategies | `backtest`, `walk_forward`, `compare` |
| `execution-mcp` | Place trades | `preview_spread`, `place_spread`, `close_position`, `positions`, `account` |

## The trading loop (how the client uses the tools)

1. `market-data-mcp.get_session_orb` -> breakout direction + strength for the active window.
2. `market-data-mcp.classify_regime` -> trend vs range (chooses debit vs credit spread).
3. `research-mcp.learn_from_history` -> does this window/regime have positive expectancy?
4. `execution-mcp.preview_spread` -> defined-risk vertical sized to the risk cap.
5. `execution-mcp.place_spread` -> submits combo order + bracket SL/TP (paper by default).
6. Outcome is logged via `research-mcp.log_trade`; `optimiser-mcp` refines params.

## Going live (deliberately hard)

Live trading requires **both**:

- `ACCOUNT_MODE=live`, and
- `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK`

and pointing `IBKR_PORT` at your live TWS/Gateway port. If only one is set, the
executor refuses to trade. Start on paper for weeks first.

## Docker

The Python stack (dashboard, Discord bot, MCP servers, configs) is one image.
IB Gateway stays a separate community container because it is a Java desktop
app.

```bash
copy .env.example .env            # then set IB_GATEWAY_USER / IB_GATEWAY_PASSWORD
docker compose up -d --build      # dashboard: http://127.0.0.1:8787

# Optional Discord bot (needs DISCORD_* in .env)
docker compose --profile discord up -d
```

Pushing `main` also publishes the image to GitHub Container Registry:

```bash
docker pull ghcr.io/<owner>/options-orb-mcp:latest
```

One-off commands in the image:

```bash
docker compose run --rm dashboard demo     # offline demo (no IBKR)
docker compose run --rm dashboard check    # IBKR connectivity
```

MCP servers still typically run on the host (Cursor/Claude launch them over
stdio). They are installed in the image if you want to exec them:

```bash
docker compose exec dashboard python -m servers.market_data_mcp.server
```

## Another PC (clone this machine)

Do **not** copy `.venv`, `.env`, or `.cursor/mcp.json` — those are tied to this
computer's Python path and secrets. Clone the repo, then on the **new** PC:

```powershell
git clone <your-repo-url> "Options Trading"
cd "Options Trading"
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env
# edit .env: IBKR_PORT=4002, ACCOUNT_MODE=paper
python -m scripts.setup
pytest -q
```

Then on that PC only:

1. Install **IB Gateway**, log into the **same paper** account, enable the API on
   port **4002** ([docs/IBKR_SETUP.md](docs/IBKR_SETUP.md)). The API is
   `127.0.0.1` — Gateway must run on that machine.
2. Optional: copy `data/history/SPY_5mins.csv` from this PC to skip a long
   history download. Otherwise:
   `python -m scripts.fetch_history --symbol SPY --days 365`
3. Leave `data/trades.db` behind unless you want this PC's journal. A missing
   file is a fresh paper ledger.
4. `python -m dashboard.app` → http://127.0.0.1:8787 then **Start** auto-trade.
   After you Start once, the dashboard resumes auto-trade automatically on every
   process restart (Gateway nightly restart / watchdog). Set
   `AUTO_TRADE_AUTOSTART=1` in `.env` to always start on boot even after Stop.
5. Nightly optimiser (23:30 GMT): `python -m scripts.nightly_optimise --install-task`
6. Restart Cursor so MCP servers pick up `.cursor/mcp.json`.

Confirm with `python -m scripts.check_ibkr`. After a Windows DST change, re-run
`--install-task`.

## Repository layout

```
core/            shared library (config, models, db, pricing, risk, strategy, ibkr)
servers/         one MCP server per agent
dashboard/       read-only web GUI (Starlette API + single-page UI)
configs/         per-window ORB parameters
scripts/         setup / demo / check_ibkr / discord_bot / nightly_optimise
tests/           unit tests (pricing, ORB, risk, metrics)
data/            SQLite journal + backtest artifacts (gitignored)
docker/          container entrypoint
Dockerfile       Python stack image (dashboard, Discord, MCP servers)
docker-compose.yml  IB Gateway + dashboard (+ optional Discord)
```
