"""Trailing-stop math on spread structure value V.

Opt-in via ``use_trailing_stop`` on the stored plan (and global
``TRAILING_STOPS_ENABLED``). When active: after +activate_r * risk_share of
profit, drop take-profit and trail stop ``distance_r * risk_share`` under the
peak mark, never below entry (breakeven floor).
"""

from __future__ import annotations

import copy
from typing import Any

from core.config import get_settings


def trail_params_valid(activate_r: float, distance_r: float) -> bool:
    try:
        a = float(activate_r)
        d = float(distance_r)
    except (TypeError, ValueError):
        return False
    return 0 < a <= 5 and 0 < d <= a


def normalize_trail_params(
    use_trailing_stop: bool,
    activate_r: float = 0.5,
    distance_r: float = 0.3,
) -> tuple[bool, float, float]:
    """Return (enabled, activate_r, distance_r); invalid numbers force off."""
    if not use_trailing_stop:
        return False, 0.5, 0.3
    if not trail_params_valid(activate_r, distance_r):
        return False, 0.5, 0.3
    return True, float(activate_r), float(distance_r)


def trailing_globally_enabled() -> bool:
    return bool(get_settings().trailing_stops_enabled)


def plan_uses_trailing(plan: dict[str, Any]) -> bool:
    if not trailing_globally_enabled():
        return False
    return bool(plan.get("use_trailing_stop"))


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def risk_share_from_plan(plan: dict[str, Any], entry: float) -> float:
    orig = _num(plan.get("original_stop_loss_price"))
    if orig is None:
        orig = _num(plan.get("stop_loss_price"))
    if orig is None:
        return 0.0
    return max(entry - orig, 0.0)


def update_trailing_stop(
    plan: dict[str, Any],
    mark: float,
    *,
    entry: float,
) -> tuple[dict[str, Any], bool, bool]:
    """Update trail state in a plan copy.

    Returns ``(new_plan, changed, just_activated)``.
    No-op when trailing is off globally or on the plan.
    """
    if not plan_uses_trailing(plan):
        return plan, False, False

    activate_r = float(plan.get("trail_activate_r") or 0.5)
    distance_r = float(plan.get("trail_distance_r") or 0.3)
    if not trail_params_valid(activate_r, distance_r):
        return plan, False, False

    out = copy.deepcopy(plan)
    if out.get("original_stop_loss_price") is None and out.get("stop_loss_price") is not None:
        out["original_stop_loss_price"] = out["stop_loss_price"]

    rs = risk_share_from_plan(out, entry)
    if rs <= 0:
        return plan, False, False

    peak = _num(out.get("peak_mark"))
    if peak is None:
        peak = mark
    else:
        peak = max(peak, mark)
    out["peak_mark"] = round(peak, 4)

    just_activated = False
    was_active = bool(out.get("trail_active"))
    threshold = entry + activate_r * rs
    if not was_active and mark >= threshold:
        out["trail_active"] = True
        out["take_profit_price"] = None
        just_activated = True

    changed = just_activated or (_num(plan.get("peak_mark")) != out["peak_mark"])

    if out.get("trail_active"):
        trailed = peak - distance_r * rs
        new_sl = round(max(entry, trailed), 4)
        old_sl = _num(out.get("stop_loss_price"))
        if old_sl is None or new_sl > old_sl + 1e-9:
            out["stop_loss_price"] = new_sl
            changed = True

    return out, changed, just_activated


def open_risk_per_contract_usd(plan: dict[str, Any], entry: float) -> float:
    """USD risk still on the table for one contract (structure × 100)."""
    from core.strategy.spreads import CONTRACT_MULTIPLIER

    sl = _num(plan.get("stop_loss_price"))
    if sl is None:
        return (
            max(entry - (_num(plan.get("original_stop_loss_price")) or 0.0), 0.0)
            * CONTRACT_MULTIPLIER
        )
    return max(entry - sl, 0.0) * CONTRACT_MULTIPLIER
