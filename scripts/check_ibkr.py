"""Quick IBKR connectivity check for the paper (or live) account.

Run after starting IB Gateway/TWS to confirm the API is reachable and market
data flows:  python -m scripts.check_ibkr
"""

from __future__ import annotations

import asyncio

from core.config import get_settings
from core.ibkr_client import IBKRClient, IBKRUnavailable


async def run() -> int:
    s = get_settings()
    print(
        f"Environment: {s.trading_environment()} | host={s.ibkr_host} port={s.ibkr_port} "
        f"clientId={s.ibkr_client_id} | market_data_type={s.ibkr_market_data_type}"
    )
    symbol = s.default_symbol
    try:
        async with IBKRClient() as ib:
            print("Connected to IBKR.")
            quote = await ib.quote(symbol)
            print(f"Quote {symbol}: {quote}")
            try:
                acct = await ib.account_summary()
                print(f"Account: {acct}")
            except Exception as exc:  # non-fatal for a data check
                print(f"(account summary unavailable: {exc})")
        print("\nOK - IBKR is reachable and returning data.")
        return 0
    except IBKRUnavailable as exc:
        print(f"\nNOT CONNECTED: {exc}")
        print(
            "Start IB Gateway/TWS, log into the PAPER account, enable the API "
            "(port 4002 for Gateway, 7497 for TWS), then retry. See docs/IBKR_SETUP.md."
        )
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
