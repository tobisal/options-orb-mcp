"""MES 5ORB break-and-retest backtester (futures, $5/point)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import product
from typing import Any

from core.analytics import performance_metrics
from core.models import Bar, Direction
from core.strategy.mes_5orb.asia_range import (
    AsiaSetupState,
    compute_asia_range,
    detect_asia_judas,
)
from core.strategy.mes_5orb.exits import (
    build_exit_levels,
    hit_stop,
    hit_target,
    primary_target,
)
from core.strategy.mes_5orb.opening_range import compute_opening_range, to_et
from core.strategy.mes_5orb.sessions import (
    Mes5OrbConfig,
    apply_mes_opt_params,
    load_mes_5orb_config,
    resolve_session_exits,
    resolve_session_max_entries,
)
from core.strategy.mes_5orb.signals import SetupState, detect_orb_setup
from core.strategy.mes_5orb.trailing_stop import SwingTrailingStop

# Compact grid for dashboard / Discord (keeps runtime reasonable on 5m history).
DEFAULT_MES_5ORB_GRID: dict[str, list[Any]] = {
    "target_r": [1.5, 2.0, 2.5],
    "scale_fraction": [0.5, 1.0],
    "stop_buffer_ticks": [1, 2],
    "tolerance_ticks": [2, 3],
    "require_rejection_candle": [False],
}

# Opens-only optimisation grid (~48 combos).
OPENS_MES_5ORB_GRID: dict[str, list[Any]] = {
    "target_r": [1.5, 2.0, 2.5],
    "scale_fraction": [0.5, 1.0],
    "stop_buffer_ticks": [1],
    "tolerance_ticks": [2, 3],
    "require_rejection_candle": [False],
    "use_hod_lod_target": [True, False],
    "runner_trail": [True, False],
    "allow_reentry": [True, False],
    "max_entries_per_session": [1, 2],
}

# Isolated London / NY open grids (~96 combos each).
LONDON_MES_5ORB_GRID: dict[str, list[Any]] = {
    "target_r": [1.5, 2.0, 2.5],
    "scale_fraction": [0.5, 1.0],
    "stop_buffer_ticks": [1],
    "tolerance_ticks": [2, 3],
    "require_rejection_candle": [False, True],
    "use_hod_lod_target": [True, False],
    "runner_trail": [True],
    "allow_reentry": [True],
    "max_entries_per_session": [1, 2],
}

NY_MES_5ORB_GRID: dict[str, list[Any]] = {
    "target_r": [1.5, 2.0, 2.5],
    "scale_fraction": [0.5, 1.0],
    "stop_buffer_ticks": [1],
    "tolerance_ticks": [2, 3],
    "require_rejection_candle": [False, True],
    "use_hod_lod_target": [True, False],
    "runner_trail": [True],
    "allow_reentry": [True],
    "max_entries_per_session": [1, 2],
}

ASIA_MES_5ORB_GRID: dict[str, list[Any]] = {
    "asia_target_r": [1.5, 2.0, 2.5],
    "asia_scale_fraction": [0.5, 1.0],
}


@dataclass
class MesTradeResult:
    session_name: str
    day: str
    direction: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    exit_reason: str
    stop_initial: float
    stop_final: float
    points: float
    pnl_usd: float
    r_multiple: float
    contracts: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_name": self.session_name,
            "day": self.day,
            "direction": self.direction,
            "entry_time": self.entry_time,
            "entry_price": round(self.entry_price, 4),
            "exit_time": self.exit_time,
            "exit_price": round(self.exit_price, 4),
            "exit_reason": self.exit_reason,
            "stop_initial": round(self.stop_initial, 4),
            "stop_final": round(self.stop_final, 4),
            "points": round(self.points, 4),
            "pnl_usd": round(self.pnl_usd, 2),
            "r_multiple": round(self.r_multiple, 4),
            "contracts": self.contracts,
        }


@dataclass
class MesBacktestResult:
    trades: list[MesTradeResult] = field(default_factory=list)
    config: Mes5OrbConfig | None = None

    @property
    def pnls(self) -> list[float]:
        return [t.pnl_usd for t in self.trades]

    def summary(self) -> dict[str, Any]:
        cfg = self.config or load_mes_5orb_config()
        by_session: dict[str, list[float]] = {}
        for t in self.trades:
            by_session.setdefault(t.session_name, []).append(t.pnl_usd)
        out: dict[str, Any] = {
            "symbol": cfg.symbol,
            "point_value": cfg.point_value,
            "trade_count": len(self.trades),
            "combined": performance_metrics(self.pnls).as_dict(),
            "by_session": {
                name: performance_metrics(pnls).as_dict()
                for name, pnls in sorted(by_session.items())
            },
        }
        return out


def _trading_days(bars: list[Bar]) -> list[date]:
    return sorted({to_et(b.ts).date() for b in bars})


def _bars_on_days(bars: list[Bar], days: set[date]) -> list[Bar]:
    """Bars on ``days``, plus prior-evening Asia range bars (20:00+) for lookback."""
    from datetime import timedelta

    lookback = {d - timedelta(days=1) for d in days}
    out: list[Bar] = []
    for b in bars:
        et = to_et(b.ts)
        d = et.date()
        if d in days:
            out.append(b)
        elif d in lookback and et.time().hour >= 20:
            out.append(b)
    return out


def walk_forward_mes_7030(
    bars: list[Bar],
    *,
    cfg: Mes5OrbConfig | None = None,
    is_fraction: float = 0.70,
) -> dict[str, Any]:
    """Chronological 70/30 walk-forward on trading days.

    First ``is_fraction`` of unique ET trading days = in-sample; the remainder
    = out-of-sample. Same fixed config on both (no grid search in v1) — OOS is
    the honest live-like estimate.
    """
    cfg = cfg or load_mes_5orb_config()
    days = _trading_days(bars)
    if len(days) < 4:
        return {
            "error": f"Need at least 4 trading days for 70/30 walk-forward, have {len(days)}.",
            "is_fraction": is_fraction,
        }

    split = max(1, int(len(days) * is_fraction))
    if split >= len(days):
        split = len(days) - 1
    is_days = set(days[:split])
    oos_days = set(days[split:])

    is_bars = _bars_on_days(bars, is_days)
    oos_bars = _bars_on_days(bars, oos_days)

    is_result = run_mes_5orb_backtest(is_bars, cfg=cfg)
    oos_result = run_mes_5orb_backtest(oos_bars, cfg=cfg)

    def _period(label: str, day_set: set[date], result: MesBacktestResult) -> dict[str, Any]:
        ordered = sorted(day_set)
        return {
            "label": label,
            "days": len(ordered),
            "day_start": ordered[0].isoformat() if ordered else None,
            "day_end": ordered[-1].isoformat() if ordered else None,
            "summary": result.summary(),
            "trade_count": len(result.trades),
        }

    return {
        "method": "chronological_holdout",
        "is_fraction": is_fraction,
        "oos_fraction": round(1.0 - is_fraction, 4),
        "total_trading_days": len(days),
        "in_sample": _period("in_sample", is_days, is_result),
        "out_of_sample": _period("out_of_sample", oos_days, oos_result),
        "walk_forward_note": (
            "Out-of-sample (last 30% of trading days) is the honest estimate of "
            "live-like performance with the fixed mes_5orb config. If OOS is much "
            "worse than in-sample, the edge is overfit or regime-dependent."
        ),
    }


def _manage_scaled_exit(
    *,
    direction: Direction,
    entry_px: float,
    init_stop: float,
    target_px: float,
    target_label: str,
    manage: list[Bar],
    entry_i: int,
    entry_ts: datetime,
    exits,
    lag: int,
    trail_buffer: float,
    tick: float,
    scale_frac: float,
) -> tuple[float, datetime, str, float, int]:
    """Scale at primary TP, runner BE/trail / force_flat / stop. Returns exit fields."""
    scale_hit = False
    scale_px = target_px
    runner_frac = 1.0 - scale_frac if scale_frac < 1.0 else 0.0
    stop = init_stop
    trail: SwingTrailingStop | None = None
    exit_px = entry_px
    exit_reason = "force_flat"
    exit_ts = entry_ts
    exit_bar_i = entry_i
    stop_final = init_stop

    for bi, b in enumerate(manage):
        if hit_stop(direction, b, stop):
            exit_px = stop
            exit_reason = "or_stop" if not scale_hit else (
                "breakeven_stop" if abs(stop - entry_px) < tick else "trailing_stop"
            )
            exit_ts = b.ts
            exit_bar_i = entry_i + 1 + bi
            stop_final = stop
            if scale_hit and runner_frac > 0:
                exit_px = scale_frac * scale_px + runner_frac * stop
                exit_reason = f"scale_{target_label}+{exit_reason}"
            break

        if not scale_hit and hit_target(direction, b, target_px):
            scale_hit = True
            scale_px = target_px
            if scale_frac >= 1.0 - 1e-9:
                exit_px = target_px
                exit_reason = f"target_{target_label}"
                exit_ts = b.ts
                exit_bar_i = entry_i + 1 + bi
                stop_final = stop
                break
            if exits.move_stop_to_be:
                stop = entry_px
            if exits.runner_trail:
                trail = SwingTrailingStop(
                    direction=direction,
                    stop=stop,
                    pivot_lag=lag,
                    buffer=trail_buffer,
                )
            continue

        if scale_hit and trail is not None:
            trail.update(b)
            stop = trail.stop
            if trail.hit(b) or hit_stop(direction, b, stop):
                exit_px = scale_frac * scale_px + runner_frac * stop
                exit_reason = f"scale_{target_label}+trailing_stop"
                exit_ts = b.ts
                exit_bar_i = entry_i + 1 + bi
                stop_final = stop
                break
    else:
        if manage:
            last = manage[-1]
            runner_px = last.close
            exit_ts = last.ts
            exit_bar_i = entry_i + len(manage)
            stop_final = stop if trail is None else trail.stop
            if scale_hit and runner_frac > 0:
                exit_px = scale_frac * scale_px + runner_frac * runner_px
                exit_reason = f"scale_{target_label}+force_flat"
            elif scale_hit:
                exit_px = scale_px
                exit_reason = f"target_{target_label}"
            else:
                exit_px = runner_px
                exit_reason = "force_flat"
        else:
            exit_px = entry_px
            exit_ts = entry_ts
            exit_reason = "force_flat"
            exit_bar_i = entry_i
            stop_final = init_stop

    return exit_px, exit_ts, exit_reason, stop_final, exit_bar_i


def run_mes_5orb_backtest(
    bars: list[Bar],
    *,
    cfg: Mes5OrbConfig | None = None,
) -> MesBacktestResult:
    """Day × session: OR → break → retest → OR stop / 2R|HOD scale → runner trail.

    Supports multiple OR windows per day and same-session re-entry after an exit.
    When ``cfg.asia_range.enabled``, also runs ICT Asian Range Judas sweep+reclaim.
    """
    cfg = cfg or load_mes_5orb_config()
    result = MesBacktestResult(config=cfg)
    contracts = cfg.risk.contracts
    tick = cfg.tick_size
    global_exits = cfg.exits
    lag = cfg.trailing_stop.pivot_lag_bars
    trail_buffer = cfg.trailing_stop.buffer_ticks * tick
    max_open = cfg.risk.max_concurrent
    allow_reentry = cfg.risk.allow_reentry
    asia_cfg = cfg.asia_range

    for day in _trading_days(bars):
        open_count = 0

        # --- ICT Asian Range Judas (London/NY search) ---
        if asia_cfg.enabled and asia_cfg.allows_day(day):
            ar = compute_asia_range(bars, day, asia_cfg)
            if ar is not None and not ar.skipped:
                setup = detect_asia_judas(
                    bars, ar, asia_cfg, tick_size=tick
                )
                if (
                    setup is not None
                    and setup.state is AsiaSetupState.RECLAIMED
                    and setup.entry_bar_index is not None
                    and setup.entry_price is not None
                    and setup.initial_stop is not None
                    and setup.target_price is not None
                    and open_count < max_open
                ):
                    entry_i = setup.entry_bar_index
                    entry_px = float(setup.entry_price)
                    init_stop = float(setup.initial_stop)
                    target_px = float(setup.target_price)
                    risk_pts = abs(entry_px - init_stop)
                    if risk_pts > 0:
                        open_count += 1
                        manage = [
                            b
                            for b in bars[entry_i + 1 :]
                            if to_et(b.ts).date() == day
                            and to_et(b.ts).time() <= asia_cfg.force_flat
                        ]
                        # Asia primer prefers partials; use asia scale_fraction, light trail.
                        asia_scale = asia_cfg.scale_fraction
                        # Temporarily disable aggressive runner trail for Asia if scale < 1
                        # (primer: partials > trail). Still allow BE after scale.
                        from dataclasses import replace

                        asia_exits = replace(
                            global_exits,
                            move_stop_to_be=True,
                            runner_trail=False,
                            scale_fraction=asia_scale,
                        )
                        exit_px, exit_ts, exit_reason, stop_final, _ = _manage_scaled_exit(
                            direction=setup.direction,
                            entry_px=entry_px,
                            init_stop=init_stop,
                            target_px=target_px,
                            target_label=setup.target_label,
                            manage=manage,
                            entry_i=entry_i,
                            entry_ts=bars[entry_i].ts,
                            exits=asia_exits,
                            lag=lag,
                            trail_buffer=trail_buffer,
                            tick=tick,
                            scale_frac=asia_scale,
                        )
                        if setup.direction is Direction.LONG:
                            points = exit_px - entry_px
                        else:
                            points = entry_px - exit_px
                        size = 1
                        _ = contracts
                        pnl = points * cfg.point_value * size
                        r_mult = points / risk_pts if risk_pts else 0.0
                        result.trades.append(
                            MesTradeResult(
                                session_name="asia",
                                day=day.isoformat(),
                                direction=setup.direction.value,
                                entry_time=_iso(bars[entry_i].ts),
                                entry_price=entry_px,
                                exit_time=_iso(exit_ts),
                                exit_price=exit_px,
                                exit_reason=exit_reason,
                                stop_initial=init_stop,
                                stop_final=stop_final,
                                points=points,
                                pnl_usd=pnl,
                                r_multiple=r_mult,
                                contracts=size,
                            )
                        )
                        open_count = max(open_count - 1, 0)

        for session in cfg.sessions:
            if not session.enabled:
                continue
            exits = resolve_session_exits(cfg, session)
            buffer_ticks = exits.stop_buffer_ticks
            scale_frac = exits.scale_fraction
            max_per_session = resolve_session_max_entries(cfg, session)
            orb = compute_opening_range(bars, session, day)
            if orb is None or orb.skipped:
                continue

            entries_this_session = 0
            after_i: int | None = None

            while entries_this_session < max_per_session:
                if open_count >= max_open:
                    break

                setup = detect_orb_setup(
                    bars,
                    session,
                    orb,
                    tick_size=tick,
                    after_bar_index=after_i,
                    stop_buffer_ticks=buffer_ticks,
                    max_stop_points=exits.max_stop_points,
                )
                if setup is None or setup.state is not SetupState.RETESTED:
                    break
                if setup.entry_bar_index is None or setup.entry_price is None:
                    break

                entry_i = setup.entry_bar_index
                entry_px = float(setup.entry_price)
                use_fib_stop = (
                    session.entry.mode == "fib_macd"
                    and setup.initial_stop is not None
                )
                levels = build_exit_levels(
                    setup.direction,
                    entry_px,
                    orb,
                    bars,
                    tick_size=tick,
                    buffer_ticks=buffer_ticks,
                    target_r=exits.target_r,
                    entry_index=entry_i,
                    max_stop_points=exits.max_stop_points,
                    target_mode=exits.target_mode,
                    target_or_fraction=exits.target_or_fraction,
                )
                if levels is None and not use_fib_stop:
                    after_i = entry_i
                    continue

                if use_fib_stop:
                    init_stop = float(setup.initial_stop)
                    risk_pts = abs(entry_px - init_stop)
                    if risk_pts <= 0:
                        after_i = entry_i
                        continue
                    # Prefer day extreme beyond entry as target (author: HOD/LOD).
                    from core.strategy.mes_5orb.exits import day_extremes

                    hod, lod = day_extremes(bars, orb.day, through_index=entry_i)
                    if setup.direction is Direction.LONG:
                        # Aim for at least 2R or prior HOD if beyond entry.
                        t2 = entry_px + 2.0 * risk_pts
                        if hod is not None and hod > entry_px:
                            target_px = max(hod, entry_px + risk_pts)
                            target_label = "HOD" if hod >= entry_px + risk_pts else "1R+"
                        else:
                            target_px, target_label = t2, "2R"
                    else:
                        t2 = entry_px - 2.0 * risk_pts
                        if lod is not None and lod < entry_px:
                            target_px = min(lod, entry_px - risk_pts)
                            target_label = "LOD" if lod <= entry_px - risk_pts else "1R+"
                        else:
                            target_px, target_label = t2, "2R"
                else:
                    assert levels is not None
                    init_stop = levels.stop
                    risk_pts = levels.risk_points
                    tgt_label = (
                        f"{exits.target_or_fraction:g}×OR"
                        if exits.target_mode == "or_fraction"
                        else "2R"
                    )
                    target_px, target_label = primary_target(
                        levels,
                        setup.direction,
                        use_hod_lod=exits.use_hod_lod_target
                        and exits.target_mode != "or_fraction",
                        target_label=tgt_label,
                    )

                open_count += 1
                entries_this_session += 1

                manage = [
                    b
                    for b in bars[entry_i + 1 :]
                    if to_et(b.ts).date() == day
                    and to_et(b.ts).time() <= session.force_flat
                ]

                exit_px, exit_ts, exit_reason, stop_final, exit_bar_i = _manage_scaled_exit(
                    direction=setup.direction,
                    entry_px=entry_px,
                    init_stop=init_stop,
                    target_px=target_px,
                    target_label=target_label,
                    manage=manage,
                    entry_i=entry_i,
                    entry_ts=bars[entry_i].ts,
                    exits=exits,
                    lag=lag,
                    trail_buffer=trail_buffer,
                    tick=tick,
                    scale_frac=scale_frac,
                )

                if setup.direction is Direction.LONG:
                    points = exit_px - entry_px
                else:
                    points = entry_px - exit_px
                # Historical PnL on 1 lot for comparability (live uses risk%).
                size = 1
                _ = contracts
                pnl = points * cfg.point_value * size
                r_mult = points / risk_pts if risk_pts else 0.0

                result.trades.append(
                    MesTradeResult(
                        session_name=session.name,
                        day=day.isoformat(),
                        direction=setup.direction.value,
                        entry_time=_iso(bars[entry_i].ts),
                        entry_price=entry_px,
                        exit_time=_iso(exit_ts),
                        exit_price=exit_px,
                        exit_reason=exit_reason,
                        stop_initial=init_stop,
                        stop_final=stop_final,
                        points=points,
                        pnl_usd=pnl,
                        r_multiple=r_mult,
                        contracts=size,
                    )
                )
                open_count = max(open_count - 1, 0)
                after_i = exit_bar_i
                if not allow_reentry:
                    break

    return result


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def evaluate_mes_signal_live(
    bars: list[Bar],
    *,
    session_name: str | None = None,
    cfg: Mes5OrbConfig | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Live/paper signal snapshot for one active MES session (5ORB or Asia Judas)."""
    from core.strategy.mes_5orb.plan import MesTradePlan

    cfg = cfg or load_mes_5orb_config()
    as_of = as_of or (bars[-1].ts if bars else datetime.utcnow())
    et = to_et(as_of)
    day = et.date()

    # Asia Judas takes priority when enabled and we're in its search window,
    # or when the caller explicitly asks for session "asia".
    asia_cfg = cfg.asia_range
    want_asia = (session_name or "").lower() in ("asia", "asian", "asia_range")
    in_asia_window = (
        asia_cfg.enabled
        and asia_cfg.allows_day(day)
        and asia_cfg.search_start <= et.time() < asia_cfg.force_flat
    )
    if want_asia or (session_name is None and in_asia_window):
        asia_sig = _evaluate_asia_live(bars, cfg=cfg, as_of=as_of)
        if asia_sig.get("ok") or want_asia:
            return asia_sig
        # Fall through to 5ORB if Asia has no setup yet and session wasn't forced.

    # Prefer the most recently started session still active (supports multi-OR day).
    sess = None
    if session_name and not want_asia:
        sess = cfg.session(session_name)
        if sess is not None and not sess.enabled:
            return {
                "ok": False,
                "reason": f"mes_5orb: session {sess.name} disabled (opens only)",
                "session": sess.name,
                "as_of": as_of.isoformat(),
            }
    else:
        active = cfg.active_sessions_at(et.time())
        if active:
            sess = max(active, key=lambda s: s.or_start)
    if sess is None:
        if asia_cfg.enabled and in_asia_window:
            return _evaluate_asia_live(bars, cfg=cfg, as_of=as_of)
        return {
            "ok": False,
            "reason": "mes_5orb: outside session windows",
            "as_of": as_of.isoformat(),
        }

    orb = compute_opening_range(bars, sess, day)
    if orb is None:
        return {
            "ok": False,
            "reason": f"mes_5orb: no OR bars yet for {sess.name}",
            "session": sess.name,
        }
    if orb.skipped:
        return {
            "ok": False,
            "reason": f"mes_5orb: OR skipped — {orb.skip_reason}",
            "session": sess.name,
            "or_high": orb.high,
            "or_low": orb.low,
        }

    if et.time() < sess.or_end:
        return {
            "ok": False,
            "reason": f"mes_5orb: building OR {sess.or_start.strftime('%H:%M')}-{sess.or_end.strftime('%H:%M')} ET",
            "session": sess.name,
            "or_high": orb.high,
            "or_low": orb.low,
        }

    exits = resolve_session_exits(cfg, sess)
    setup = detect_orb_setup(
        bars,
        sess,
        orb,
        tick_size=cfg.tick_size,
        stop_buffer_ticks=exits.stop_buffer_ticks,
        max_stop_points=exits.max_stop_points,
    )
    if setup is None:
        return {
            "ok": False,
            "reason": "mes_5orb: no break yet",
            "session": sess.name,
            "or_high": orb.high,
            "or_low": orb.low,
        }
    if setup.state is not SetupState.RETESTED:
        return {
            "ok": False,
            "reason": f"mes_5orb: {setup.notes or setup.state.value}",
            "session": sess.name,
            "or_high": orb.high,
            "or_low": orb.low,
            "break_level": setup.break_level,
            "direction": setup.direction.value,
            "state": setup.state.value,
        }

    entry_i = int(setup.entry_bar_index)  # type: ignore[arg-type]
    entry_px = float(setup.entry_price)
    levels = build_exit_levels(
        setup.direction,
        entry_px,
        orb,
        bars,
        tick_size=cfg.tick_size,
        buffer_ticks=exits.stop_buffer_ticks,
        target_r=exits.target_r,
        entry_index=entry_i,
        max_stop_points=exits.max_stop_points,
        target_mode=exits.target_mode,
        target_or_fraction=exits.target_or_fraction,
    )
    if levels is None:
        return {
            "ok": False,
            "reason": "mes_5orb: invalid OR stop / risk",
            "session": sess.name,
        }
    tgt_label = (
        f"{exits.target_or_fraction:g}×OR"
        if exits.target_mode == "or_fraction"
        else "2R"
    )
    target_px, target_label = primary_target(
        levels,
        setup.direction,
        use_hod_lod=exits.use_hod_lod_target
        and exits.target_mode != "or_fraction",
        target_label=tgt_label,
    )

    plan = MesTradePlan(
        symbol=cfg.symbol,
        session_name=sess.name,
        direction=setup.direction,
        entry_price=entry_px,
        stop_price=levels.stop,
        contracts=cfg.risk.contracts,
        break_level=setup.break_level,
        or_high=orb.high,
        or_low=orb.low,
        point_value=cfg.point_value,
        tick_size=cfg.tick_size,
        as_of=as_of,
        notes=setup.notes,
        target_price=target_px,
        target_label=target_label,
        target_r=exits.target_r,
        scale_fraction=exits.scale_fraction,
        use_trailing_stop=exits.runner_trail,
        trail_pivot_lag=cfg.trailing_stop.pivot_lag_bars,
        trail_buffer_ticks=cfg.trailing_stop.buffer_ticks,
        stop_mode=exits.stop_mode,
    )
    return {
        "ok": True,
        "session": sess.name,
        "or_high": orb.high,
        "or_low": orb.low,
        "state": setup.state.value,
        "plan": plan.as_dict(),
        "mes_plan": plan,
    }


def _evaluate_asia_live(
    bars: list[Bar],
    *,
    cfg: Mes5OrbConfig,
    as_of: datetime,
) -> dict[str, Any]:
    """Live snapshot for ICT Asian Range Judas sweep + reclaim."""
    from core.strategy.mes_5orb.plan import MesTradePlan

    asia_cfg = cfg.asia_range
    et = to_et(as_of)
    day = et.date()
    if not asia_cfg.enabled:
        return {"ok": False, "reason": "asia_range: disabled", "session": "asia"}
    if not asia_cfg.allows_day(day):
        return {
            "ok": False,
            "reason": f"asia_range: weekday {day.weekday()} not allowed",
            "session": "asia",
        }

    ar = compute_asia_range(bars, day, asia_cfg)
    if ar is None:
        return {
            "ok": False,
            "reason": "asia_range: no Asia bars yet (need prior 20:00–00:00 ET)",
            "session": "asia",
        }

    def _levels() -> dict[str, Any]:
        out: dict[str, Any] = {
            "or_high": ar.high,
            "or_low": ar.low,
            "asia_eq": ar.eq,
            "ny_high": ar.ny_high,
            "ny_low": ar.ny_low,
            "pd_high": ar.pd_high,
            "pd_low": ar.pd_low,
            "prev_session_high": ar.prev_session_high,
            "prev_session_low": ar.prev_session_low,
            "midnight_open": ar.midnight_open,
        }
        return out

    if ar.skipped:
        return {
            "ok": False,
            "reason": f"asia_range: skipped — {ar.skip_reason}",
            "session": "asia",
            **_levels(),
        }

    if et.time() < asia_cfg.search_start:
        ny_bit = ""
        if ar.ny_high is not None and ar.ny_low is not None:
            ny_bit += f" NYH={ar.ny_high:.2f} NYL={ar.ny_low:.2f}"
        if ar.pd_high is not None and ar.pd_low is not None:
            ny_bit += f" PDH={ar.pd_high:.2f} PDL={ar.pd_low:.2f}"
        if ar.prev_session_high is not None and ar.prev_session_low is not None:
            ny_bit += (
                f" PSH={ar.prev_session_high:.2f} PSL={ar.prev_session_low:.2f}"
            )
        return {
            "ok": False,
            "reason": (
                f"asia_range: waiting for search "
                f"{asia_cfg.search_start.strftime('%H:%M')} ET "
                f"(ARH={ar.high:.2f} ARL={ar.low:.2f} EQ={ar.eq:.2f}{ny_bit})"
            ),
            "session": "asia",
            **_levels(),
        }

    setup = detect_asia_judas(bars, ar, asia_cfg, tick_size=cfg.tick_size)
    if setup is None or setup.state is not AsiaSetupState.RECLAIMED:
        return {
            "ok": False,
            "reason": "asia_range: waiting for Judas sweep + reclaim (Asia/NY/PD/prev-session)",
            "session": "asia",
            **_levels(),
        }

    entry_px = float(setup.entry_price)  # type: ignore[arg-type]
    stop_px = float(setup.initial_stop)  # type: ignore[arg-type]
    target_px = float(setup.target_price)  # type: ignore[arg-type]
    plan = MesTradePlan(
        symbol=cfg.symbol,
        session_name="asia",
        direction=setup.direction,
        entry_price=entry_px,
        stop_price=stop_px,
        contracts=cfg.risk.contracts,
        break_level=setup.sweep_extreme,
        or_high=ar.high,
        or_low=ar.low,
        point_value=cfg.point_value,
        tick_size=cfg.tick_size,
        as_of=as_of,
        notes=setup.notes,
        target_price=target_px,
        target_label=setup.target_label,
        target_r=asia_cfg.target_r,
        scale_fraction=asia_cfg.scale_fraction,
        use_trailing_stop=False,
        trail_pivot_lag=cfg.trailing_stop.pivot_lag_bars,
        trail_buffer_ticks=cfg.trailing_stop.buffer_ticks,
        stop_mode="asia_sweep",
    )
    return {
        "ok": True,
        "session": "asia",
        **_levels(),
        "sweep_levels": list(setup.sweep_levels),
        "state": setup.state.value,
        "plan": plan.as_dict(),
        "mes_plan": plan,
        "entry_model": "asia_judas",
    }


def mes_opt_score(metrics: dict[str, Any]) -> float:
    """Rank score used by the futures grid optimiser."""
    trades = int(metrics.get("trades") or 0)
    if trades < 2:
        return -999.0
    exp = float(metrics.get("expectancy") or 0)
    pf = min(float(metrics.get("profit_factor") or 0), 5.0)
    dd = abs(float(metrics.get("max_drawdown") or 0))
    return exp * (trades**0.5) * (0.5 + 0.5 * min(pf, 3.0) / 3.0) - dd * 0.01


def _mes_knob_dict(**knobs: Any) -> dict[str, Any]:
    """Normalize optimiser knobs for persistence / equality checks."""
    out: dict[str, Any] = {
        "target_r": float(knobs["target_r"]),
        "scale_fraction": float(knobs["scale_fraction"]),
        "stop_buffer_ticks": int(knobs["stop_buffer_ticks"]),
        "tolerance_ticks": int(knobs["tolerance_ticks"]),
        "require_rejection_candle": bool(knobs["require_rejection_candle"]),
    }
    for key in (
        "use_hod_lod_target",
        "move_stop_to_be",
        "runner_trail",
        "allow_reentry",
        "max_entries_per_session",
        "asia_target_r",
        "asia_scale_fraction",
        "timeout_bars",
    ):
        if key not in knobs or knobs[key] is None:
            continue
        if key in ("max_entries_per_session", "timeout_bars"):
            out[key] = int(knobs[key])
        elif key in ("asia_target_r", "asia_scale_fraction"):
            out[key] = float(knobs[key])
        else:
            out[key] = bool(knobs[key])
    return out


def optimise_mes_5orb(
    bars: list[Bar],
    *,
    cfg: Mes5OrbConfig | None = None,
    grid: dict[str, list[Any]] | None = None,
    is_fraction: float = 0.70,
    top_is: int = 8,
    top_n: int = 8,
) -> dict[str, Any]:
    """Grid-search exit/retest knobs with chronological 70/30 walk-forward.

    1. Evaluate every grid combo on in-sample days → IS score.
    2. Keep the top ``top_is`` by IS score.
    3. Rank those by out-of-sample score (honest selection).
    """
    cfg = cfg or load_mes_5orb_config()
    grid = grid or DEFAULT_MES_5ORB_GRID
    days = _trading_days(bars)
    if len(days) < 4:
        return {
            "error": f"Need at least 4 trading days for walk-forward optimise, have {len(days)}.",
            "is_fraction": is_fraction,
        }

    split = max(1, int(len(days) * is_fraction))
    if split >= len(days):
        split = len(days) - 1
    is_days = set(days[:split])
    oos_days = set(days[split:])
    is_bars = _bars_on_days(bars, is_days)
    oos_bars = _bars_on_days(bars, oos_days)

    keys = list(grid.keys())
    combos = list(product(*[grid[k] for k in keys]))
    rows: list[dict[str, Any]] = []

    for i_combo, combo in enumerate(combos, start=1):
        raw = dict(zip(keys, combo))
        knobs: dict[str, Any] = {
            "target_r": raw.get("target_r", cfg.exits.target_r),
            "scale_fraction": raw.get("scale_fraction", cfg.exits.scale_fraction),
            "stop_buffer_ticks": raw.get(
                "stop_buffer_ticks", cfg.exits.stop_buffer_ticks
            ),
            "tolerance_ticks": raw.get(
                "tolerance_ticks",
                cfg.sessions[0].retest.tolerance_ticks if cfg.sessions else 3,
            ),
            "require_rejection_candle": raw.get(
                "require_rejection_candle",
                cfg.sessions[0].retest.require_rejection_candle
                if cfg.sessions
                else False,
            ),
        }
        for extra in (
            "use_hod_lod_target",
            "move_stop_to_be",
            "runner_trail",
            "allow_reentry",
            "max_entries_per_session",
            "asia_target_r",
            "asia_scale_fraction",
            "timeout_bars",
        ):
            if extra in raw:
                knobs[extra] = raw[extra]
        trial_cfg = apply_mes_opt_params(cfg, knobs)
        is_m = run_mes_5orb_backtest(is_bars, cfg=trial_cfg).summary()["combined"]
        oos_m = run_mes_5orb_backtest(oos_bars, cfg=trial_cfg).summary()["combined"]
        rows.append(
            {
                "params": _mes_knob_dict(**knobs),
                "is_score": round(mes_opt_score(is_m), 4),
                "oos_score": round(mes_opt_score(oos_m), 4),
                "is_metrics": is_m,
                "oos_metrics": oos_m,
            }
        )
        if i_combo == 1 or i_combo % 16 == 0 or i_combo == len(combos):
            print(f"  grid {i_combo}/{len(combos)}", flush=True)

    rows.sort(key=lambda r: r["is_score"], reverse=True)
    shortlist = rows[: max(int(top_is), 1)]
    shortlist.sort(key=lambda r: r["oos_score"], reverse=True)
    ranked = shortlist[: max(int(top_n), 1)]

    best = ranked[0] if ranked else None
    baseline_knobs = _mes_knob_dict(
        target_r=cfg.exits.target_r,
        scale_fraction=cfg.exits.scale_fraction,
        stop_buffer_ticks=cfg.exits.stop_buffer_ticks,
        tolerance_ticks=(
            cfg.sessions[0].retest.tolerance_ticks if cfg.sessions else 3
        ),
        require_rejection_candle=(
            cfg.sessions[0].retest.require_rejection_candle if cfg.sessions else False
        ),
    )
    baseline_row = next(
        (
            r
            for r in rows
            if r["params"] == baseline_knobs
        ),
        None,
    )

    top_payload = []
    for i, r in enumerate(ranked):
        top_payload.append(
            {
                "rank": i + 1,
                "label": (
                    f"{r['params']['target_r']}R · "
                    f"{int(r['params']['scale_fraction'] * 100)}% · "
                    f"buf {r['params']['stop_buffer_ticks']} · "
                    f"tol {r['params']['tolerance_ticks']}"
                ),
                "params": {
                    "entry_model": "mes_5orb",
                    "symbol": cfg.symbol,
                    "optimise": "grid_walk_forward_70_30",
                    **r["params"],
                },
                "score": r["oos_score"],
                "is_score": r["is_score"],
                "metrics": r["oos_metrics"],
                "is_metrics": r["is_metrics"],
            }
        )

    return {
        "method": "grid_walk_forward_70_30",
        "is_fraction": is_fraction,
        "oos_fraction": round(1.0 - is_fraction, 4),
        "total_trading_days": len(days),
        "is_days": len(is_days),
        "oos_days": len(oos_days),
        "day_start": days[0].isoformat(),
        "is_end": days[split - 1].isoformat(),
        "oos_start": days[split].isoformat(),
        "day_end": days[-1].isoformat(),
        "combinations_tested": len(rows),
        "grid": grid,
        "baseline_params": baseline_knobs,
        "baseline": baseline_row,
        "best": best,
        "top": top_payload,
        "walk_forward_note": (
            "Grid fitted on the first 70% of trading days; candidates are ranked "
            "by out-of-sample score on the last 30%. Use 'Use for trading' to "
            "apply the chosen exit/retest knobs."
        ),
    }
