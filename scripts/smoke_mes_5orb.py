"""Paper/smoke check for MES 5ORB: signal idle outside window + synthetic plan path."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytz  # noqa: E402

from core.backtest_mes import evaluate_mes_signal_live  # noqa: E402
from core.engine import build_mes_trade_plan, settle_session_exits  # noqa: E402
from core.models import Bar, Direction  # noqa: E402
from core.strategy.mes_5orb.sessions import load_mes_5orb_config  # noqa: E402
from core.strategy.mes_5orb.trailing_stop import SwingTrailingStop  # noqa: E402


def _et_utc(y, m, d, hh, mm) -> datetime:
    et = pytz.timezone("America/New_York")
    return et.localize(datetime(y, m, d, hh, mm)).astimezone(pytz.utc).replace(tzinfo=None)


def _bar(ts: datetime, o, h, l, c) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=100)


async def main() -> int:
    cfg = load_mes_5orb_config()
    assert cfg.symbol == "MES"

    # Outside session → idle
    idle_bars = [_bar(_et_utc(2026, 1, 6, 12, 0), 100, 101, 99, 100)]
    idle = evaluate_mes_signal_live(idle_bars, cfg=cfg, as_of=idle_bars[-1].ts)
    assert idle.get("ok") is False, idle
    print("OK idle outside window:", idle.get("reason"))

    # Synthetic engine path
    preview = await build_mes_trade_plan("MES", "auto", use_synthetic=True)
    print("OK build_mes_trade_plan synthetic:", preview.get("ok"), preview.get("reason") or "setup/idle")

    # Trail force-flat helper
    trail = SwingTrailingStop(Direction.LONG, stop=100.0, pivot_lag=2, buffer=0.25)
    for i in range(8):
        trail.update(_bar(_et_utc(2026, 1, 6, 10, 0) + timedelta(minutes=5 * i), 101, 102, 100.5, 101.5))
    print("OK trail stop=", trail.stop)

    # settle with empty opens is a no-op
    from core.db import Database

    db = Database(path=ROOT / "data" / "_mes_smoke.db")
    closed = await settle_session_exits(db, "MES", idle_bars)
    print("OK settle empty opens:", len(closed))

    print("MES paper smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
