"""Session-by-session ORB spread backtester.

Honest scope: this models vertical-spread P&L from the **underlying price path**
plus an implied-volatility proxy (Black-Scholes), holding time-to-expiry roughly
constant within a session so that P&L is driven by directional moves through the
spread's strikes. It is a decision-quality approximation for ranking parameter
sets, not a tick-accurate fill simulator. Confirm survivors on a paper account.

Assumptions:
- One trade per session (first qualifying breakout after the opening range).
- Entry/exit at bar close on the spread's theoretical structure value.
- Fixed IV and time-to-expiry across the holding period (intraday).
- No commissions/slippage by default (add a per-trade cost to stress it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product

from core.analytics import summarize
from core.models import Bar, Direction, Regime, SessionWindow, SpreadType
from core.pricing import vertical_spread_value
from core.sessions import get_window_config, group_by_session, opening_range_of
from core.strategy.orb import average_true_range
from core.strategy.spreads import CONTRACT_MULTIPLIER

# Shared with the dashboard optimiser, MCP, and the nightly selector.
DEFAULT_ORB_GRID: dict[str, list] = {
    "opening_range_minutes": [10, 15, 20, 30, 45],
    "breakout_buffer_atr": [0.02, 0.05, 0.10, 0.20],
    "min_strength": [0.10, 0.15, 0.25, 0.40],
}


@dataclass
class BacktestParams:
    opening_range_minutes: int = 30
    breakout_buffer_atr: float = 0.10
    min_strength: float = 0.25
    target_r: float = 1.5
    stop_r: float = 1.0
    use_trailing_stop: bool = False
    trail_activate_r: float = 0.5
    trail_distance_r: float = 0.3
    iv: float = 0.25
    dte: int = 7
    strike_increment: float = 1.0
    cost_per_trade: float = 0.0  # currency per contract, models commission/slippage

    def as_dict(self) -> dict:
        return {
            "opening_range_minutes": self.opening_range_minutes,
            "breakout_buffer_atr": self.breakout_buffer_atr,
            "min_strength": self.min_strength,
            "target_r": self.target_r,
            "stop_r": self.stop_r,
            "use_trailing_stop": self.use_trailing_stop,
            "trail_activate_r": self.trail_activate_r,
            "trail_distance_r": self.trail_distance_r,
            "iv": self.iv,
            "dte": self.dte,
            "strike_increment": self.strike_increment,
            "cost_per_trade": self.cost_per_trade,
        }


@dataclass
class BacktestResult:
    window: SessionWindow
    params: BacktestParams
    pnls: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    def summary(self, include_monte_carlo: bool = True) -> dict:
        return {
            "window": self.window.value,
            "params": self.params.as_dict(),
            "num_trades": len(self.pnls),
            "metrics": summarize(self.pnls, include_monte_carlo=include_monte_carlo),
            "trades": self.trades,
        }


def _efficiency_regime(bars: list[Bar]) -> Regime:
    if len(bars) < 3:
        return Regime.UNCERTAIN
    net = abs(bars[-1].close - bars[0].open)
    path = sum(abs(b.close - b.open) for b in bars) + sum(
        abs(c.open - p.close) for p, c in zip(bars[:-1], bars[1:])
    )
    if path <= 0:
        return Regime.UNCERTAIN
    eff = net / path
    if eff >= 0.4:
        return Regime.TREND
    if eff <= 0.2:
        return Regime.RANGE
    return Regime.UNCERTAIN


def _round_to_increment(value: float, inc: float) -> float:
    return round(value / inc) * inc


def run_backtest(
    bars: list[Bar], window: SessionWindow, params: BacktestParams
) -> BacktestResult:
    """Run the ORB spread strategy session-by-session over ``bars``."""
    cfg = get_window_config(window)
    result = BacktestResult(window=window, params=params)
    atr_all = average_true_range(bars)

    for session_key, sbars in group_by_session(bars, cfg):
        orb, post = opening_range_of(sbars, params.opening_range_minutes)
        if not orb or len(post) < 2:
            continue
        range_high = max(b.high for b in orb)
        range_low = min(b.low for b in orb)
        width_range = max(range_high - range_low, 1e-9)
        buffer = params.breakout_buffer_atr * (atr_all or width_range)

        # Find first qualifying breakout in post-range bars.
        entry_idx = None
        direction = Direction.NEUTRAL
        for i, b in enumerate(post):
            if b.close > range_high + buffer:
                strength = (b.close - range_high) / width_range
                if strength >= params.min_strength:
                    entry_idx, direction = i, Direction.LONG
                    break
            elif b.close < range_low - buffer:
                strength = (range_low - b.close) / width_range
                if strength >= params.min_strength:
                    entry_idx, direction = i, Direction.SHORT
                    break
        if entry_idx is None:
            continue

        entry_bar = post[entry_idx]
        spot = entry_bar.close
        regime = _efficiency_regime(post[: entry_idx + 1])

        trade = _simulate_trade(
            spot=spot,
            direction=direction,
            regime=regime,
            forward_bars=post[entry_idx + 1 :],
            params=params,
        )
        if trade is None:
            continue
        trade["session"] = session_key
        result.pnls.append(trade["pnl"])
        result.trades.append(trade)

    return result


def _simulate_trade(
    *,
    spot: float,
    direction: Direction,
    regime: Regime,
    forward_bars: list[Bar],
    params: BacktestParams,
) -> dict | None:
    inc = params.strike_increment
    width = inc
    t = max(params.dte, 0.0001) / 365.0
    iv = params.iv

    spread_type = _pick_spread_type(direction, regime)
    right, long_strike, short_strike, band_low, band_high = _strikes_for(
        spread_type, spot, inc, width
    )

    def structure_value(s: float) -> float:
        return vertical_spread_value(s, long_strike, short_strike, t, iv, right)

    v0 = min(max(structure_value(spot), band_low), band_high)
    max_loss_share = v0 - band_low
    max_profit_share = band_high - v0
    if max_loss_share <= 0 or max_profit_share <= 0:
        return None

    stop_r = min(max(params.stop_r, 0.0), 1.0)
    risk_share = stop_r * max_loss_share
    reward_share = min(params.target_r * risk_share, max_profit_share)
    tp = v0 + reward_share
    sl = v0 - risk_share
    original_sl = sl
    use_trail = bool(params.use_trailing_stop)
    activate_r = float(params.trail_activate_r)
    distance_r = float(params.trail_distance_r)
    if use_trail and not (0 < activate_r <= 5 and 0 < distance_r <= activate_r):
        use_trail = False
    peak = v0
    trail_active = False

    exit_v = None
    exit_reason = "session_end"
    for b in forward_bars:
        v = min(max(structure_value(b.close), band_low), band_high)
        if use_trail:
            peak = max(peak, v)
            if not trail_active and v >= v0 + activate_r * risk_share:
                trail_active = True
            if trail_active:
                sl = max(v0, peak - distance_r * risk_share)
                if v <= sl:
                    exit_v, exit_reason = sl, "trailing_stop"
                    break
                continue
        if not trail_active and v >= tp:
            exit_v, exit_reason = tp, "take_profit"
            break
        if v <= sl:
            exit_v, exit_reason = sl, "stop_loss"
            break
    if exit_v is None:
        exit_v = min(max(structure_value(forward_bars[-1].close), band_low), band_high) if forward_bars else v0

    pnl = (exit_v - v0) * CONTRACT_MULTIPLIER - params.cost_per_trade
    return {
        "spread_type": spread_type.value,
        "direction": direction.value,
        "regime": regime.value,
        "long_strike": long_strike,
        "short_strike": short_strike,
        "entry_value": round(v0, 4),
        "exit_value": round(exit_v, 4),
        "tp": round(tp, 4),
        "sl": round(original_sl if not trail_active else sl, 4),
        "exit_reason": exit_reason,
        "pnl": round(pnl, 2),
        "trail_active": trail_active,
    }


def _pick_spread_type(direction: Direction, regime: Regime) -> SpreadType:
    if regime is Regime.RANGE:
        return SpreadType.BULL_PUT if direction is not Direction.SHORT else SpreadType.BEAR_CALL
    return SpreadType.BULL_CALL if direction is Direction.LONG else SpreadType.BEAR_PUT


def _strikes_for(spread_type: SpreadType, spot: float, inc: float, width: float):
    from core.models import OptionRight

    if spread_type is SpreadType.BULL_CALL:
        long_strike = _round_to_increment(spot, inc)
        short_strike = long_strike + width
        return OptionRight.CALL, long_strike, short_strike, 0.0, width
    if spread_type is SpreadType.BEAR_PUT:
        long_strike = _round_to_increment(spot, inc)
        short_strike = long_strike - width
        return OptionRight.PUT, long_strike, short_strike, 0.0, width
    if spread_type is SpreadType.BULL_PUT:  # credit
        short_strike = _round_to_increment(spot, inc)
        long_strike = short_strike - width
        return OptionRight.PUT, long_strike, short_strike, -width, 0.0
    short_strike = _round_to_increment(spot, inc)  # BEAR_CALL credit
    long_strike = short_strike + width
    from core.models import OptionRight as _OR

    return _OR.CALL, long_strike, short_strike, -width, 0.0


# --- optimisation ----------------------------------------------------------

def _score(metrics: dict) -> float:
    """Balanced score: expectancy scaled by profit factor and win rate, penalised
    by drawdown. Rewards likelihood of winning *and* reward:risk together."""
    m = metrics["metrics"] if "metrics" in metrics else metrics
    exp = m.get("expectancy", 0.0)
    pf = min(m.get("profit_factor", 0.0), 5.0)
    wr = m.get("win_rate", 0.0)
    dd_pct = m.get("max_drawdown_pct", 0.0)  # scale-free (0..1)
    if m.get("trades", 0) < 5:
        return -1e9
    # Reward expectancy weighted by win likelihood and reward:risk (profit
    # factor), penalised by the depth of the worst drawdown.
    return exp * (0.5 + 0.5 * wr) * (0.5 + 0.1 * pf) * (1.0 - 0.5 * dd_pct)


def dedupe_by_outcome(ranked: list[dict]) -> list[dict]:
    """Collapse parameter sets that produce an identical simulated outcome.

    Some parameters (e.g. target_r/stop_r when trades exit at session end) do
    not change the result, so a raw grid can surface many rows with identical
    metrics. Keeping only the first (best-scoring) representative of each
    distinct outcome makes the leaderboard meaningful. Assumes ``ranked`` is
    already sorted best-first.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in ranked:
        m = r.get("metrics", {})
        key = (
            m.get("trades"),
            round(m.get("total_pnl", 0.0), 2),
            round(m.get("max_drawdown", 0.0), 2),
            round(m.get("win_rate", 0.0), 4),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def grid_search(
    bars: list[Bar], window: SessionWindow, grid: dict[str, list], base: BacktestParams | None = None
) -> list[dict]:
    """Evaluate every combination in ``grid`` and return results sorted by score."""
    base = base or BacktestParams()
    keys = list(grid.keys())
    combos = list(product(*[grid[k] for k in keys]))
    evaluated: list[dict] = []
    for combo in combos:
        params = BacktestParams(**{**base.as_dict(), **dict(zip(keys, combo))})
        res = run_backtest(bars, window, params).summary(include_monte_carlo=False)
        res["score"] = round(_score(res), 4)
        evaluated.append(res)
    evaluated.sort(key=lambda r: r["score"], reverse=True)
    return evaluated


def walk_forward(
    bars: list[Bar],
    window: SessionWindow,
    grid: dict[str, list],
    *,
    folds: int = 3,
) -> dict:
    """Walk-forward: optimise on each in-sample fold, test on the next out-of-sample
    segment. Guards against curve-fitting by scoring on unseen data."""
    cfg = get_window_config(window)
    sessions = group_by_session(bars, cfg)
    if len(sessions) < folds + 1:
        return {"error": f"Need at least {folds + 1} sessions, have {len(sessions)}."}

    seg = len(sessions) // (folds + 1)
    fold_reports: list[dict] = []
    oos_pnls: list[float] = []

    for f in range(folds):
        is_sessions = sessions[: seg * (f + 1)]
        oos_sessions = sessions[seg * (f + 1) : seg * (f + 2)]
        if not oos_sessions:
            break
        is_bars = [b for _, sb in is_sessions for b in sb]
        oos_bars = [b for _, sb in oos_sessions for b in sb]

        ranked = grid_search(is_bars, window, grid)
        best = ranked[0] if ranked else None
        if best is None:
            continue
        best_params = BacktestParams(**{**BacktestParams().as_dict(), **best["params"]})
        oos = run_backtest(oos_bars, window, best_params).summary(include_monte_carlo=False)
        oos_pnls.extend([t["pnl"] for t in oos["trades"]])
        fold_reports.append(
            {
                "fold": f + 1,
                "in_sample_score": best["score"],
                "in_sample_metrics": best["metrics"]["metrics"] if "metrics" in best["metrics"] else best["metrics"],
                "chosen_params": best["params"],
                "out_of_sample_metrics": oos["metrics"],
                "out_of_sample_trades": oos["num_trades"],
            }
        )

    return {
        "window": window.value,
        "folds": len(fold_reports),
        "fold_reports": fold_reports,
        "aggregate_out_of_sample": summarize(oos_pnls),
        "walk_forward_note": (
            "Out-of-sample aggregate is the honest estimate of live-like performance. "
            "If it is much worse than in-sample, the parameters are overfit."
        ),
    }
