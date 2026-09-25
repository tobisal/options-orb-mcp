"""Compare baseline ORB vs VWAP+regime+volume filters on Barchart 5m.

Does NOT write active_strategy / live params. Writes a JSON report under
data/nightly/ for review before any promote.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import (  # noqa: E402
    DEFAULT_ORB_GRID,
    BacktestParams,
    grid_search,
    walk_forward,
)
from core.config import REPO_ROOT  # noqa: E402
from core.models import Bar, SessionWindow  # noqa: E402
from core.timeutils import as_naive_utc  # noqa: E402

CSV_PATH = REPO_ROOT / "data" / "history" / "SPY_5mins_barchart.csv"
OUT_DIR = REPO_ROOT / "data" / "nightly"
FOLDS = 3

FILTER_BASE = BacktestParams(
    require_vwap_align=True,
    require_trend_regime=True,
    volume_confirm_mult=1.0,
)


def load_barchart(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append(
                Bar(
                    ts=as_naive_utc(datetime.fromisoformat(row["ts"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def _metrics_brief(m: dict) -> dict:
    return {
        "trades": m.get("trades"),
        "total_pnl": round(float(m.get("total_pnl") or 0), 2),
        "expectancy": round(float(m.get("expectancy") or 0), 4),
        "win_rate": round(float(m.get("win_rate") or 0), 4),
        "profit_factor": round(float(m.get("profit_factor") or 0), 4),
        "max_drawdown": round(float(m.get("max_drawdown") or 0), 2),
    }


def _oos_brief(wf: dict) -> dict:
    agg = wf.get("aggregate_out_of_sample") or {}
    return {
        "folds": wf.get("folds"),
        "oos": _metrics_brief(agg),
        "fold_chosen": [
            {
                "fold": fr["fold"],
                "params": {
                    k: fr["chosen_params"][k]
                    for k in (
                        "opening_range_minutes",
                        "breakout_buffer_atr",
                        "min_strength",
                    )
                    if k in fr["chosen_params"]
                },
                "oos_trades": fr["out_of_sample_trades"],
                "oos_total": round(
                    float((fr.get("out_of_sample_metrics") or {}).get("total_pnl") or 0), 2
                ),
            }
            for fr in wf.get("fold_reports") or []
        ],
    }


def eval_window(bars: list[Bar], window: SessionWindow) -> dict:
    print(f"=== {window.value} baseline grid+WF ===", flush=True)
    base_ranked = grid_search(bars, window, DEFAULT_ORB_GRID, base=BacktestParams())
    base_best = base_ranked[0]
    base_wf = walk_forward(bars, window, DEFAULT_ORB_GRID, folds=FOLDS)

    print(f"=== {window.value} filtered grid+WF ===", flush=True)
    filt_ranked = grid_search(bars, window, DEFAULT_ORB_GRID, base=FILTER_BASE)
    filt_best = filt_ranked[0]
    filt_wf = walk_forward(bars, window, DEFAULT_ORB_GRID, folds=FOLDS, base=FILTER_BASE)

    return {
        "window": window.value,
        "baseline": {
            "best_params": base_best["params"],
            "best_score": base_best["score"],
            "in_sample": _metrics_brief(base_best["metrics"]),
            "walk_forward": _oos_brief(base_wf),
        },
        "filtered": {
            "flags": {
                "require_vwap_align": True,
                "require_trend_regime": True,
                "volume_confirm_mult": 1.0,
            },
            "best_params": filt_best["params"],
            "best_score": filt_best["score"],
            "in_sample": _metrics_brief(filt_best["metrics"]),
            "walk_forward": _oos_brief(filt_wf),
        },
    }


def main() -> int:
    if not CSV_PATH.exists():
        print(f"Missing {CSV_PATH}", file=sys.stderr)
        return 1
    bars = load_barchart(CSV_PATH)
    print(
        json.dumps(
            {
                "bars": len(bars),
                "from": str(bars[0].ts),
                "to": str(bars[-1].ts),
                "grid_size": 80,
            }
        ),
        flush=True,
    )

    report = {
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": str(CSV_PATH.name),
        "bars": len(bars),
        "from": str(bars[0].ts),
        "to": str(bars[-1].ts),
        "live_params_changed": False,
        "windows": {},
    }
    for win in (SessionWindow.NEW_YORK, SessionWindow.LONDON):
        report["windows"][win.value] = eval_window(bars, win)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"entry_filters_wf_{report['day']}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out}", flush=True)
    print(json.dumps(report["windows"], indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
