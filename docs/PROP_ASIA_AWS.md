# Prop Asia AWS (Tradovate multi-account)

Branch `prop-asia-aws` runs **Asia Judas only** on Tradovate-backed prop
accounts, placing the same MES bracket on every configured account from AWS.

## Requirements

- Prop firm that allows **bot trading from cloud** (not TopstepX — VPS/AWS
  order origin is banned there and the factory refuses `topstep`/`projectx`
  backends on AWS hosts).
- Tradovate credentials with API access for those accounts.
- Confirm current firm ToS before funding.

## Configure

In `.env` on the EC2 host:

```env
EXECUTION_BACKEND=tradovate_prop
PROP_ASIA_ONLY=1
PROP_DEFAULT_CONTRACTS=6
TRADOVATE_USER=...
TRADOVATE_PASSWORD=...
TRADOVATE_ENV=demo
PROP_ACCOUNTS_JSON=[{"id":"111","name":"eval-a","enabled":true,"size_scale":1.0},{"id":"222","name":"eval-b","enabled":true,"size_scale":1.0}]
AUTO_TRADE_AUTOSTART=1
AUTO_TRADE_INTERVAL=10
```

Lead account is the **first** entry. Lead place failure aborts; follower
failures are retried once and logged on the journal plan as `prop_copies`.

## Run on AWS

```bash
docker compose --profile prop-asia up -d --build
# Dashboard (SSH tunnel): http://127.0.0.1:8788
```

IB Gateway is **not** required for this profile.

## Status

`GET /api/autotrade/status` includes `execution_backend`, `prop_asia_only`,
`prop_default_contracts`, and `prop_account_count`.
