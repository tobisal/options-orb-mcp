"""Download IBKR historical bars (chunked) into data/history/.

Example:  python -m scripts.fetch_history --symbol SPY --days 365
"""

from __future__ import annotations

import argparse
import asyncio

from core.ibkr_client import IBKRUnavailable
from core.marketdata import fetch_bars_range, history_cache_path


async def run(symbol: str, days: int, bar_size: str) -> int:
    print(f"Fetching {days}d of {bar_size} bars for {symbol} (cache {history_cache_path(symbol, bar_size)})")
    try:
        bars = await fetch_bars_range(
            symbol, days=days, bar_size=bar_size, progress=print
        )
    except IBKRUnavailable as exc:
        print(f"FAILED: {exc}")
        print("Start IB Gateway/TWS, then retry. See docs/IBKR_SETUP.md.")
        return 1
    if not bars:
        print("No bars returned.")
        return 1
    print(
        f"OK: {len(bars)} bars from {bars[0].ts.isoformat()} GMT "
        f"to {bars[-1].ts.isoformat()} GMT"
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch IBKR historical bars for backtests.")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--bar-size", default="5 mins")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.symbol, args.days, args.bar_size)))


if __name__ == "__main__":
    main()
