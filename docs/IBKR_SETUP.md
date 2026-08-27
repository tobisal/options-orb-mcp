# Connecting the paper account to live IBKR data

Paper trading in this system uses **live IBKR market data** (synthetic/"Demo data"
is only an explicit offline toggle). To get real prices flowing you need IB
Gateway (or TWS) running and logged into your **paper** account with the API
enabled. This guide walks through it from scratch.

> You do **not** need a funded account or paid data to try this: IBKR paper
> accounts get **free 15-minute delayed** market data, and the system
> automatically falls back to delayed data when you lack a real-time
> subscription (see [Market data](#market-data)).

## 1. Enable a paper trading account

1. Log in to the [IBKR Client Portal](https://www.interactivebrokers.co.uk/).
2. Go to **Settings -> Account Settings -> Paper Trading Account** and enable it.
3. IBKR emails you a separate **paper username** (usually your live username with
   a suffix). You set its password in the portal. Use these paper credentials
   below - not your live login.

## 2. Install IB Gateway (recommended) or TWS

IB Gateway is a lightweight, headless-ish app that only provides the API (no full
charting UI), so it is the better fit for an automated system.

1. Download **IB Gateway (stable)** from Interactive Brokers
   (search "IB Gateway download"; the stable channel is fine).
2. Install and launch it.
3. On the login screen choose **Paper Trading** (there is a Live/Paper toggle),
   and log in with your **paper** username and password.

TWS works too - it uses port `7497` for paper instead of `4002`.

## 3. Enable and configure the API

In IB Gateway: **Configure -> Settings -> API -> Settings**:

- Tick **Enable ActiveX and Socket Clients**.
- Set **Socket port** to `4002` (paper Gateway). (TWS paper uses `7497`.)
- Add `127.0.0.1` to **Trusted IPs**.
- Leave **Read-Only API** **unticked** so the execution agent can place paper orders.
- Click OK / Apply.

Under **Configure -> Settings -> Lock and Exit**, set the daily maintenance to
**Restart** (not Shut Down) so the Gateway comes back automatically each day.

## 4. Point the system at the Gateway

In your `.env` (copy from `.env.example` if you have not yet):

```env
IBKR_HOST=127.0.0.1
IBKR_PORT=4002            # 7497 if you use TWS paper
IBKR_CLIENT_ID=17
ACCOUNT_MODE=paper
IBKR_MARKET_DATA_TYPE=auto
```

## 5. Verify the connection

With IB Gateway running and logged in:

```bash
python -m dashboard.app         # open http://127.0.0.1:8787
```

The dashboard header should show **IBKR connected**, the account card should show
your paper net-liquidation value, and the Live signals panel (with "Demo data"
off) should return `data source: ibkr`.

You can also verify from the command line:

```bash
python -m scripts.check_ibkr        # prints connection status + a sample quote
```

## Market data

- IBKR **paper accounts include free 15-minute delayed data**. Real-time (live)
  US quotes require a paid market-data subscription on the underlying **live**
  account.
- `IBKR_MARKET_DATA_TYPE=auto` (the default) requests **real-time**, and if IBKR
  reports you are not subscribed, it automatically switches to **delayed** data.
  So it "just works" on a bare paper account and upgrades to live automatically
  once you have subscriptions.
- Other values: `live` (1), `frozen` (2), `delayed` (3), `delayed_frozen` (4).
- Delayed data is perfectly adequate for the ORB signals here; it only means
  quotes lag by ~15 minutes. Historical bars are unaffected.

## Headless / another device (optional)

To run IB Gateway without a desktop (e.g. on a server), use the community
IBC-based image referenced in [../docker-compose.yml](../docker-compose.yml):

1. Add `IB_GATEWAY_USER` and `IB_GATEWAY_PASSWORD` (your paper credentials) to a
   local `.env` (keep them out of git).
2. `docker compose up -d` (container restarts on reboot; Gateway also does a
   daily soft restart at 11:59 PM London so login survives without a new 2FA).
3. Set `IBKR_PORT=4002` in your app `.env` and connect as usual.

First login from a new IP usually needs an IBKR Mobile confirmation. After that,
IBC re-enters the stored username/password on every restart.

## Troubleshooting

- **`API connection failed: ConnectionRefusedError`**: Gateway/TWS is not running,
  not logged in, or the API/port is not enabled. Re-check step 3.
- **Connected but quotes are empty / NaN**: you likely lack a real-time
  subscription; with `IBKR_MARKET_DATA_TYPE=auto` the system falls back to delayed
  data automatically. Confirm the market is open (delayed data still updates).
- **`clientId` already in use**: another session is using `IBKR_CLIENT_ID`. Change
  it in `.env`.
- **Repeated connection errors in the terminal**: set `IBKR_VERBOSE=0` (default)
  to suppress `ib_async`'s own logging; the system retries on a cooldown.
