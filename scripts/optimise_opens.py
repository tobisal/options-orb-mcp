"""Walk-forward optimise opens-only MES logic; print best vs baseline."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from core.backtest_mes import (
    ASIA_MES_5ORB_GRID,
    OPENS_MES_5ORB_GRID,
    optimise_mes_5orb,
    run_mes_5orb_backtest,
)
from core.ibkr_client import IBKRClient
from core.strategy.mes_5orb.sessions import (
    apply_mes_opt_params,
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
)


async def main() -> int:
    clear_mes_5orb_config_cache()
    cfg = load_mes_5orb_config("MES")
    sessions = tuple(
        replace(s, enabled=s.name in ("london", "new_york")) for s in cfg.sessions
    )
    cfg = replace(cfg, sessions=sessions, asia_range=replace(cfg.asia_range, enabled=True))

    bars = []
    async with IBKRClient() as ib:
        for dur in ("1 M", "30 D", "20 D"):
            bars = await ib.historical_bars_future(
                "MES", duration=dur, bar_size="5 mins"
            )
            if bars:
                break
    if not bars:
        print("no bars", flush=True)
        return 1

    n_combos = 1
    for v in OPENS_MES_5ORB_GRID.values():
        n_combos *= len(v)
    print(f"bars={len(bars)} pass1_combos={n_combos}", flush=True)

    base_bt = run_mes_5orb_backtest(bars, cfg=cfg).summary()
    print(
        f"baseline full PnL={base_bt['combined']['total_pnl']} "
        f"trades={base_bt['trade_count']} PF={base_bt['combined']['profit_factor']}",
        flush=True,
    )

    report = optimise_mes_5orb(
        bars,
        cfg=cfg,
        grid=OPENS_MES_5ORB_GRID,
        is_fraction=0.70,
        top_is=12,
        top_n=8,
    )
    if report.get("error"):
        print(json.dumps(report, indent=2), flush=True)
        return 1

    best = report["best"]
    params = dict(best["params"])
    print(f"pass1 best OOS score={best['oos_score']} params={params}", flush=True)

    cfg2 = apply_mes_opt_params(cfg, params)
    locked = {
        k: [params[k]]
        for k in (
            "target_r",
            "scale_fraction",
            "stop_buffer_ticks",
            "tolerance_ticks",
            "require_rejection_candle",
            "use_hod_lod_target",
            "runner_trail",
            "allow_reentry",
            "max_entries_per_session",
        )
        if k in params
    }
    asia_report = optimise_mes_5orb(
        bars,
        cfg=cfg2,
        grid={**locked, **ASIA_MES_5ORB_GRID},
        is_fraction=0.70,
        top_is=6,
        top_n=5,
    )
    if not asia_report.get("error") and asia_report.get("best"):
        asia_best = asia_report["best"]
        if asia_best["oos_score"] >= best["oos_score"]:
            params = dict(asia_best["params"])
            best = asia_best
            print(f"pass2 asia OOS={best['oos_score']} params={params}", flush=True)
        else:
            print(f"pass2 asia no improvement OOS={asia_best['oos_score']}", flush=True)

    opt_cfg = apply_mes_opt_params(cfg, params)
    opt_bt = run_mes_5orb_backtest(bars, cfg=opt_cfg).summary()
    out = {
        "baseline_full": base_bt["combined"],
        "baseline_trades": base_bt["trade_count"],
        "optimised_full": opt_bt["combined"],
        "optimised_trades": opt_bt["trade_count"],
        "params": params,
        "oos_score": best["oos_score"],
        "is_score": best["is_score"],
        "oos_metrics": best["oos_metrics"],
        "improved_pnl": float(opt_bt["combined"]["total_pnl"])
        > float(base_bt["combined"]["total_pnl"]),
        "improved_oos": float(best["oos_metrics"].get("total_pnl") or 0)
        >= float((report.get("baseline") or {}).get("oos_metrics", {}).get("total_pnl") or -1e9),
    }
    path = "/tmp/mes_opens_optimise.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
