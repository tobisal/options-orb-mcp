"""Isolated walk-forward optimise for London and NY opens; apply OOS winners."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from core.backtest_mes import (
    LONDON_MES_5ORB_GRID,
    NY_MES_5ORB_GRID,
    _bars_on_days,
    _trading_days,
    optimise_mes_5orb,
    run_mes_5orb_backtest,
)
from core.config import REPO_ROOT
from core.ibkr_client import IBKRClient
from core.strategy.mes_5orb.sessions import (
    apply_mes_opt_params,
    clear_mes_5orb_config_cache,
    load_mes_5orb_config,
)

# Minimum OOS trades before accepting a session winner over baseline.
MIN_OOS_TRADES = 2


def _session_only(cfg, name: str):
    sessions = tuple(replace(s, enabled=(s.name == name)) for s in cfg.sessions)
    return replace(
        cfg,
        sessions=sessions,
        asia_range=replace(cfg.asia_range, enabled=False),
    )


def _prefix_params(session_name: str, params: dict) -> dict:
    """Convert global opt knobs → london_/ny_ scoped keys for apply_mes_opt_params."""
    prefix = "london_" if session_name == "london" else "ny_"
    out: dict = {}
    for k, v in params.items():
        if k in ("allow_reentry",):
            out[k] = v
            continue
        if k == "max_entries_per_session":
            out[f"{prefix}max_entries"] = v
            continue
        out[f"{prefix}{k}"] = v
    return out


def _exits_block(params: dict) -> dict:
    return {
        "stop_mode": "or_extreme",
        "stop_buffer_ticks": int(params.get("stop_buffer_ticks", 1)),
        "target_r": float(params.get("target_r", 2.5)),
        "target_mode": "r_multiple",
        "scale_fraction": float(params.get("scale_fraction", 1.0)),
        "use_hod_lod_target": bool(params.get("use_hod_lod_target", True)),
        "move_stop_to_be": True,
        "runner_trail": bool(params.get("runner_trail", True)),
    }


def _apply_london_filter(
    cfg,
    *,
    directions: str | None = None,
    max_or: float | None = None,
    weekdays: tuple[int, ...] | None = None,
):
    """Mutate London session entry / OR filter; leave other sessions untouched."""
    updated = []
    for s in cfg.sessions:
        if s.name != "london":
            updated.append(s)
            continue
        sess = s
        if directions is not None:
            sess = replace(
                sess,
                entry=replace(sess.entry, allowed_directions=directions),
            )
        if weekdays is not None:
            sess = replace(
                sess,
                entry=replace(sess.entry, allowed_weekdays=weekdays),
            )
        if max_or is not None:
            sess = replace(
                sess,
                opening_range=replace(
                    sess.opening_range, max_range_points=float(max_or)
                ),
            )
        updated.append(sess)
    return replace(cfg, sessions=tuple(updated))


def _wf_session_scores(bars, cfg, is_fraction: float = 0.70) -> tuple[dict, dict]:
    days = _trading_days(bars)
    split = max(1, int(len(days) * is_fraction))
    if split >= len(days):
        split = len(days) - 1
    is_m = run_mes_5orb_backtest(
        _bars_on_days(bars, set(days[:split])), cfg=cfg
    ).summary()["combined"]
    oos_m = run_mes_5orb_backtest(
        _bars_on_days(bars, set(days[split:])), cfg=cfg
    ).summary()["combined"]
    return is_m, oos_m


def _write_config_winners(
    *,
    london_params: dict | None,
    ny_params: dict | None,
    london_filter: dict | None = None,
) -> Path:
    path = REPO_ROOT / "configs" / "mes_5orb.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    sessions = raw.setdefault("sessions", {})
    if london_params:
        ldn = sessions.setdefault("london", {})
        ldn["exits"] = _exits_block(london_params)
        ldn["max_entries"] = int(london_params.get("max_entries_per_session", 2))
        rt = ldn.setdefault("retest", {})
        rt["tolerance_ticks"] = int(london_params.get("tolerance_ticks", 3))
        rt["require_rejection_candle"] = bool(
            london_params.get("require_rejection_candle", False)
        )
    if london_filter:
        ldn = sessions.setdefault("london", {})
        if london_filter.get("allowed_directions"):
            en = ldn.setdefault("entry", {})
            en["allowed_directions"] = london_filter["allowed_directions"]
        if london_filter.get("allowed_weekdays") is not None:
            en = ldn.setdefault("entry", {})
            en["allowed_weekdays"] = list(london_filter["allowed_weekdays"])
        if london_filter.get("max_range_points") is not None:
            oran = ldn.setdefault("opening_range", {})
            oran["max_range_points"] = float(london_filter["max_range_points"])
    if ny_params:
        ny = sessions.setdefault("new_york", {})
        ny["exits"] = _exits_block(ny_params)
        ny["max_entries"] = int(ny_params.get("max_entries_per_session", 2))
        rt = ny.setdefault("retest", {})
        rt["tolerance_ticks"] = int(ny_params.get("tolerance_ticks", 3))
        rt["require_rejection_candle"] = bool(
            ny_params.get("require_rejection_candle", False)
        )
    path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return path


async def main() -> int:
    clear_mes_5orb_config_cache()
    base_cfg = load_mes_5orb_config("MES")

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

    results: dict = {"bars": len(bars), "sessions": {}, "filters": []}
    apply_scoped: dict = {}
    winners: dict[str, dict | None] = {"london": None, "new_york": None}
    london_filter: dict | None = None

    for name, grid in (
        ("london", LONDON_MES_5ORB_GRID),
        ("new_york", NY_MES_5ORB_GRID),
    ):
        cfg = _session_only(base_cfg, name)
        n_combos = 1
        for v in grid.values():
            n_combos *= len(v)
        print(f"=== {name} combos={n_combos} ===", flush=True)

        base_bt = run_mes_5orb_backtest(bars, cfg=cfg).summary()
        base_pnl = float(base_bt["combined"]["total_pnl"])
        print(
            f"{name} baseline PnL={base_pnl} trades={base_bt['trade_count']} "
            f"PF={base_bt['combined'].get('profit_factor')}",
            flush=True,
        )

        report = optimise_mes_5orb(
            bars,
            cfg=cfg,
            grid=grid,
            is_fraction=0.70,
            top_is=10,
            top_n=6,
        )
        if report.get("error"):
            results["sessions"][name] = {
                "error": report["error"],
                "baseline_pnl": base_pnl,
            }
            continue

        best = report["best"]
        params = dict(best["params"])
        oos_trades = int(best["oos_metrics"].get("trades") or 0)
        oos_pnl = float(best["oos_metrics"].get("total_pnl") or 0)
        base_oos = float(
            (report.get("baseline") or {})
            .get("oos_metrics", {})
            .get("total_pnl")
            or -1e9
        )
        opt_cfg = apply_mes_opt_params(cfg, params)
        opt_full = run_mes_5orb_backtest(bars, cfg=opt_cfg).summary()
        opt_pnl = float(opt_full["combined"]["total_pnl"])

        accept = (
            oos_trades >= MIN_OOS_TRADES
            and oos_pnl >= base_oos
            and opt_pnl > base_pnl
        )
        print(
            f"{name} best OOS={best['oos_score']} oos_pnl={oos_pnl} "
            f"oos_trades={oos_trades} full={opt_pnl} accept={accept} params={params}",
            flush=True,
        )

        results["sessions"][name] = {
            "baseline_pnl": base_pnl,
            "baseline_trades": base_bt["trade_count"],
            "optimised_pnl": opt_pnl,
            "optimised_trades": opt_full["trade_count"],
            "params": params,
            "oos_score": best["oos_score"],
            "oos_metrics": best["oos_metrics"],
            "oos_trades": oos_trades,
            "accepted": accept,
        }
        if accept:
            winners[name] = params
            apply_scoped.update(_prefix_params(name, params))

    # Filter pass (London): shorts dominate; wide OR 6+ and Thu drag
    print("=== london filter pass ===", flush=True)
    ldn_base = _session_only(base_cfg, "london")
    _, base_oos_m = _wf_session_scores(bars, ldn_base)
    base_oos_pnl = float(base_oos_m.get("total_pnl") or 0)
    base_full_pnl = float(
        run_mes_5orb_backtest(bars, cfg=ldn_base).summary()["combined"]["total_pnl"]
    )

    filter_candidates = [
        {"label": "london_short_only", "directions": "short"},
        {"label": "london_max_or_6", "max_or": 6.0},
        {"label": "london_skip_thu", "weekdays": (0, 1, 2, 4)},
        {"label": "london_short_max_or_6", "directions": "short", "max_or": 6.0},
        {
            "label": "london_short_skip_thu",
            "directions": "short",
            "weekdays": (0, 1, 2, 4),
        },
        {
            "label": "london_short_max_or_6_skip_thu",
            "directions": "short",
            "max_or": 6.0,
            "weekdays": (0, 1, 2, 4),
        },
    ]
    best_filter = None
    best_filter_oos = base_oos_pnl
    best_filter_full = base_full_pnl
    for cand in filter_candidates:
        trial = _apply_london_filter(
            ldn_base,
            directions=cand.get("directions"),
            max_or=cand.get("max_or"),
            weekdays=cand.get("weekdays"),
        )
        _, oos_m = _wf_session_scores(bars, trial)
        full_m = run_mes_5orb_backtest(bars, cfg=trial).summary()["combined"]
        oos_pnl = float(oos_m.get("total_pnl") or 0)
        oos_trades = int(oos_m.get("trades") or 0)
        full_pnl = float(full_m.get("total_pnl") or 0)
        row = {
            "label": cand["label"],
            "oos_pnl": oos_pnl,
            "oos_trades": oos_trades,
            "full_pnl": full_pnl,
            "full_trades": int(full_m.get("trades") or 0),
            "accepted": False,
        }
        accept = (
            oos_trades >= MIN_OOS_TRADES
            and oos_pnl > best_filter_oos
            and full_pnl > best_filter_full
        )
        if accept:
            row["accepted"] = True
            best_filter = cand
            best_filter_oos = oos_pnl
            best_filter_full = full_pnl
        results["filters"].append(row)
        print(
            f"  {cand['label']}: oos={oos_pnl} full={full_pnl} "
            f"trades={row['full_trades']} accept={row['accepted']}",
            flush=True,
        )

    if best_filter:
        london_filter = {
            k: v
            for k, v in {
                "allowed_directions": best_filter.get("directions"),
                "max_range_points": best_filter.get("max_or"),
                "allowed_weekdays": best_filter.get("weekdays"),
            }.items()
            if v is not None
        }
        print(
            f"london filter winner: {best_filter['label']} → {london_filter}",
            flush=True,
        )

    clear_mes_5orb_config_cache()
    live = load_mes_5orb_config("MES")
    baseline_full = run_mes_5orb_backtest(bars, cfg=live).summary()
    if any(winners.values()) or london_filter:
        config_path = _write_config_winners(
            london_params=winners["london"],
            ny_params=winners["new_york"],
            london_filter=london_filter,
        )
        clear_mes_5orb_config_cache()
        opt_live = load_mes_5orb_config("MES")
        if apply_scoped:
            opt_live = apply_mes_opt_params(opt_live, apply_scoped)
        opt_full = run_mes_5orb_backtest(bars, cfg=opt_live).summary()
        results["config_path"] = str(config_path)
        results["london_filter"] = london_filter
    else:
        opt_full = baseline_full
        results["config_path"] = None
        results["london_filter"] = None

    results["full_stack"] = {
        "baseline_pnl": baseline_full["combined"]["total_pnl"],
        "baseline_trades": baseline_full["trade_count"],
        "optimised_pnl": opt_full["combined"]["total_pnl"],
        "optimised_trades": opt_full["trade_count"],
        "asia_unchanged_weekdays": list(live.asia_range.allowed_weekdays or ()),
    }
    results["winners"] = {k: v for k, v in winners.items() if v is not None}

    path = "/tmp/mes_london_ny_optimise.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(results, indent=2, fp=fh, default=str)
    print(json.dumps(results, indent=2, default=str), flush=True)
    print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
