"""Production-standard performance analytics.

These metrics are the common language for comparing strategies: they balance the
*likelihood* of winning (win rate) against the *magnitude* of wins vs losses
(reward:risk), and account for the shape of the equity curve (Sharpe/Sortino,
drawdown) and sequence risk (Monte-Carlo). Used by both the research agent
(learning from realised trades) and the optimiser (evaluating candidates).
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass

# Finite stand-in for "infinite" ratios (no losses) so results stay JSON-safe
# and comparable. Displayed as "inf" in the UI.
_INF_SENTINEL = 999.0


@dataclass
class PerformanceMetrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float  # positive magnitude
    reward_risk: float  # avg_win / avg_loss
    expectancy: float  # mean pnl per trade
    profit_factor: float  # gross_profit / gross_loss
    total_pnl: float
    sharpe: float  # per-trade, annualised-agnostic (mean/std)
    sortino: float
    max_drawdown: float  # currency, on cumulative equity
    max_drawdown_pct: float

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def performance_metrics(pnls: list[float]) -> PerformanceMetrics:
    """Compute the full metric set from a list of per-trade P&L values."""
    n = len(pnls)
    if n == 0:
        return PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # positive
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    total = sum(pnls)
    expectancy = total / n

    mean = expectancy
    var = sum((p - mean) ** 2 for p in pnls) / n
    std = math.sqrt(var)
    sharpe = mean / std if std > 0 else 0.0

    downside = [p for p in pnls if p < 0]
    d_var = sum(p * p for p in downside) / n if downside else 0.0
    d_std = math.sqrt(d_var)
    sortino = mean / d_std if d_std > 0 else (float("inf") if mean > 0 else 0.0)

    max_dd, max_dd_pct = _max_drawdown(pnls)

    return PerformanceMetrics(
        trades=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / n,
        avg_win=avg_win,
        avg_loss=avg_loss,
        reward_risk=(avg_win / avg_loss) if avg_loss > 0 else (_INF_SENTINEL if avg_win > 0 else 0.0),
        expectancy=expectancy,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else (_INF_SENTINEL if gross_profit > 0 else 0.0),
        total_pnl=total,
        sharpe=sharpe,
        sortino=sortino if sortino != float("inf") else _INF_SENTINEL,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
    )


def _max_drawdown(pnls: list[float]) -> tuple[float, float]:
    """Return (currency max drawdown, drawdown fraction in [0, 1]).

    For a P&L stream that starts at zero, the running peak can be a tiny early
    value, which makes ``dd/peak`` explode and become meaningless. We therefore
    measure the fraction against the *global* peak equity and clamp it to [0, 1]
    so it is a well-behaved, comparable quantity.
    """
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    global_peak = 0.0
    equity = 0.0
    for p in pnls:
        equity += p
        global_peak = max(global_peak, equity)
    max_dd_pct = min(max_dd / global_peak, 1.0) if global_peak > 0 else (1.0 if max_dd > 0 else 0.0)
    return max_dd, max_dd_pct


def monte_carlo(pnls: list[float], *, iterations: int = 2000, seed: int = 7) -> dict:
    """Bootstrap resample (with replacement) to estimate sequence risk.

    Resampling the same number of trades with replacement produces a
    distribution of plausible outcomes and drawdowns, which a single backtest
    equity curve hides. (Merely reordering trades cannot change their sum, so we
    resample rather than shuffle.)
    """
    if not pnls:
        return {"iterations": 0}
    rng = random.Random(seed)
    n = len(pnls)
    finals: list[float] = []
    max_dds: list[float] = []
    for _ in range(iterations):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        finals.append(sum(sample))
        dd, _ = _max_drawdown(sample)
        max_dds.append(dd)
    finals.sort()
    max_dds.sort()

    def pct(sorted_vals: list[float], q: float) -> float:
        if not sorted_vals:
            return 0.0
        idx = min(int(q * (len(sorted_vals) - 1)), len(sorted_vals) - 1)
        return sorted_vals[idx]

    return {
        "iterations": iterations,
        "final_pnl_p05": round(pct(finals, 0.05), 2),
        "final_pnl_p50": round(pct(finals, 0.50), 2),
        "final_pnl_p95": round(pct(finals, 0.95), 2),
        "prob_profit": round(sum(1 for f in finals if f > 0) / len(finals), 4),
        "max_drawdown_p95": round(pct(max_dds, 0.95), 2),
    }


def summarize(pnls: list[float], *, include_monte_carlo: bool = True) -> dict:
    """Convenience bundle of metrics (+ optional Monte-Carlo)."""
    out = performance_metrics(pnls).as_dict()
    if include_monte_carlo:
        out["monte_carlo"] = monte_carlo(pnls)
    return out
