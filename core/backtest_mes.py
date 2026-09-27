"""MES 5ORB break-and-retest backtester (futures, $5/point)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from core.analytics import performance_metrics
from core.models import Bar, Direction
from core.strategy.mes_5orb.opening_range import compute_opening_range, to_et
from core.strategy.mes_5orb.sessions import Mes5OrbConfig, load_mes_5orb_config
from core.strategy.mes_5orb.signals import SetupState, detect_break_retest
from core.strategy.mes_5orb.trailing_stop import SwingTrailingStop


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
    return [b for b in bars if to_et(b.ts).date() in days]


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


def _force_flat_bars(
    bars: list[Bar], day: date, force_flat_time
) -> list[Bar]:
    out: list[Bar] = []
    for b in bars:
        et = to_et(b.ts)
        if et.date() != day:
            continue
        if et.time() <= force_flat_time:
            out.append(b)
    return sorted(out, key=lambda x: x.ts)


def run_mes_5orb_backtest(
    bars: list[Bar],
    *,
    cfg: Mes5OrbConfig | None = None,
) -> MesBacktestResult:
    """Day × session event loop: OR → break → retest → trail → force_flat.

    Supports multiple OR windows per day and same-session re-entry after an exit.
    """
    cfg = cfg or load_mes_5orb_config()
    result = MesBacktestResult(config=cfg)
    contracts = cfg.risk.contracts
    tick = cfg.tick_size
    buffer = cfg.trailing_stop.buffer_ticks * tick
    lag = cfg.trailing_stop.pivot_lag_bars
    max_open = cfg.risk.max_concurrent
    allow_reentry = cfg.risk.allow_reentry
    max_per_session = cfg.risk.max_entries_per_session

    for day in _trading_days(bars):
        open_count = 0
        for session in cfg.sessions:
            orb = compute_opening_range(bars, session, day)
            if orb is None or orb.skipped:
                continue

            entries_this_session = 0
            after_i: int | None = None

            while entries_this_session < max_per_session:
                if open_count >= max_open:
                    break

                setup = detect_break_retest(
                    bars,
                    session,
                    orb,
                    tick_size=tick,
                    after_bar_index=after_i,
                )
                if setup is None or setup.state is not SetupState.RETESTED:
                    break
                if setup.entry_bar_index is None or setup.entry_price is None:
                    break

                entry_bar = bars[setup.entry_bar_index]
                if setup.direction is Direction.LONG:
                    init_stop = entry_bar.low - buffer
                else:
                    init_stop = entry_bar.high + buffer

                entry_i = setup.entry_bar_index
                entry_px = float(setup.entry_price)
                risk_pts = abs(entry_px - init_stop)
                if risk_pts <= 0:
                    after_i = entry_i
                    continue

                trail = SwingTrailingStop(
                    direction=setup.direction,
                    stop=init_stop,
                    pivot_lag=lag,
                    buffer=buffer,
                )
                open_count += 1
                entries_this_session += 1

                exit_px = entry_px
                exit_reason = "force_flat"
                exit_ts = bars[entry_i].ts
                manage = [
                    b
                    for b in bars[entry_i + 1 :]
                    if to_et(b.ts).date() == day
                    and to_et(b.ts).time() <= session.force_flat
                ]
                exit_bar_i = entry_i
                for bi, b in enumerate(manage):
                    trail.update(b)
                    if trail.hit(b) or (
                        (setup.direction is Direction.LONG and b.close < trail.stop)
                        or (setup.direction is Direction.SHORT and b.close > trail.stop)
                    ):
                        exit_px = trail.stop
                        exit_reason = "trailing_stop"
                        exit_ts = b.ts
                        exit_bar_i = entry_i + 1 + bi
                        break
                else:
                    if manage:
                        exit_px = manage[-1].close
                        exit_ts = manage[-1].ts
                        exit_reason = "force_flat"
                        exit_bar_i = entry_i + len(manage)
                    else:
                        exit_px = entry_px
                        exit_ts = bars[entry_i].ts
                        exit_reason = "force_flat"
                        exit_bar_i = entry_i

                if setup.direction is Direction.LONG:
                    points = exit_px - entry_px
                else:
                    points = entry_px - exit_px
                # Size uses config max contracts; live path applies risk%.
                size = max(int(contracts), 1)
                # Cap display size for backtest to 1 lot when contracts is a ceiling
                # of 20 — use 1 for historical PnL comparability unless risk_pct sizing
                # is wired into backtest later.
                size = 1
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
                        stop_final=trail.stop,
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
    """Live/paper signal snapshot for one active MES session."""
    from core.strategy.mes_5orb.plan import MesTradePlan

    cfg = cfg or load_mes_5orb_config()
    as_of = as_of or (bars[-1].ts if bars else datetime.utcnow())
    et = to_et(as_of)
    day = et.date()

    # Prefer the most recently started session still active (supports multi-OR day).
    sess = None
    if session_name:
        sess = cfg.session(session_name)
    else:
        active = cfg.active_sessions_at(et.time())
        if active:
            sess = max(active, key=lambda s: s.or_start)
    if sess is None:
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

    setup = detect_break_retest(bars, sess, orb, tick_size=cfg.tick_size)
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

    buffer = cfg.trailing_stop.buffer_ticks * cfg.tick_size
    entry_bar = bars[setup.entry_bar_index]  # type: ignore[index]
    if setup.direction is Direction.LONG:
        stop = entry_bar.low - buffer
    else:
        stop = entry_bar.high + buffer

    plan = MesTradePlan(
        symbol=cfg.symbol,
        session_name=sess.name,
        direction=setup.direction,
        entry_price=float(setup.entry_price),
        stop_price=stop,
        contracts=cfg.risk.contracts,
        break_level=setup.break_level,
        or_high=orb.high,
        or_low=orb.low,
        point_value=cfg.point_value,
        tick_size=cfg.tick_size,
        as_of=as_of,
        notes=setup.notes,
        trail_pivot_lag=cfg.trailing_stop.pivot_lag_bars,
        trail_buffer_ticks=cfg.trailing_stop.buffer_ticks,
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
