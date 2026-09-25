"""CLI: run MES 5ORB break-and-retest backtest on a CSV of 5-min bars."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest_mes import run_mes_5orb_backtest, walk_forward_mes_7030  # noqa: E402
from core.config import REPO_ROOT  # noqa: E402
from core.models import Bar  # noqa: E402
from core.strategy.mes_5orb.sessions import clear_mes_5orb_config_cache, load_mes_5orb_config  # noqa: E402
from core.timeutils import as_naive_utc  # noqa: E402


def load_csv(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ts_raw = row.get("timestamp") or row.get("ts") or row.get("datetime") or row.get("date")
            if not ts_raw:
                continue
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00").replace(" ", "T"))
            if ts.tzinfo is not None:
                ts = as_naive_utc(ts)
            bars.append(
                Bar(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def main() -> int:
    p = argparse.ArgumentParser(description="Futures 5ORB break/retest backtest")
    p.add_argument(
        "--symbol",
        default="MES",
        help="Futures symbol: MES, MNQ, MYM, M2K, ES, NQ (default MES)",
    )
    p.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="5-min OHLCV CSV (default data/history/{SYMBOL}_5mins.csv)",
    )
    p.add_argument("--out-dir", type=Path, default=REPO_ROOT / "data" / "nightly")
    p.add_argument(
        "--walk-forward",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run chronological 70/30 walk-forward (default: on)",
    )
    p.add_argument(
        "--is-fraction",
        type=float,
        default=0.70,
        help="In-sample fraction of trading days (default 0.70)",
    )
    args = p.parse_args()

    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config(args.symbol)
    csv_path = args.csv or (REPO_ROOT / "data" / "history" / f"{cfg.symbol}_5mins.csv")

    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        print(f"Place {cfg.symbol} 5-min bars there or pass --csv PATH", file=sys.stderr)
        return 1

    bars = load_csv(csv_path)
    print(f"Loaded {len(bars)} bars from {csv_path} ({cfg.symbol})")
    result = run_mes_5orb_backtest(bars, cfg=cfg)
    summary = result.summary()

    wf: dict | None = None
    if args.walk_forward:
        wf = walk_forward_mes_7030(bars, cfg=cfg, is_fraction=args.is_fraction)
        summary["walk_forward_70_30"] = {
            "method": wf.get("method"),
            "is_fraction": wf.get("is_fraction"),
            "oos_fraction": wf.get("oos_fraction"),
            "total_trading_days": wf.get("total_trading_days"),
            "in_sample": wf.get("in_sample"),
            "out_of_sample": wf.get("out_of_sample"),
            "note": wf.get("walk_forward_note") or wf.get("error"),
        }

    day = datetime.utcnow().strftime("%Y-%m-%d")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.out_dir / f"{cfg.symbol.lower()}_5orb_{day}.json"
    trades_path = args.out_dir / f"{cfg.symbol.lower()}_5orb_trades_{day}.csv"

    report = {
        "day": day,
        "csv": str(args.csv),
        "bars": len(bars),
        "summary": summary,
        "walk_forward_70_30": wf,
        "trades": [t.as_dict() for t in result.trades],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if result.trades:
        fieldnames = list(result.trades[0].as_dict().keys())
        with trades_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            w.writeheader()
            for t in result.trades:
                w.writerow(t.as_dict())

    print(json.dumps(summary, indent=2))
    if wf and "out_of_sample" in wf:
        oos = wf["out_of_sample"]
        print(
            f"\n70/30 WF — IS days={wf['in_sample']['days']} "
            f"trades={wf['in_sample']['trade_count']} | "
            f"OOS days={oos['days']} trades={oos['trade_count']} "
            f"OOS total_pnl={oos['summary']['combined'].get('total_pnl')}"
        )
    print(f"Wrote {report_path}")
    if result.trades:
        print(f"Wrote {trades_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
