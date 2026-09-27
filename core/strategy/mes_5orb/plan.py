"""MES futures trade plan (no options legs)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from core.models import Direction


@dataclass
class MesTradePlan:
    symbol: str
    session_name: str
    direction: Direction
    entry_price: float
    stop_price: float
    contracts: int
    break_level: float
    or_high: float
    or_low: float
    point_value: float = 5.0
    tick_size: float = 0.25
    as_of: datetime | None = None
    notes: str = ""
    # Classic ORB exits: SL = OR extreme; primary TP = 2R or HOD; runners trail.
    target_price: float | None = None
    target_label: str = "2R"
    target_r: float = 2.0
    scale_fraction: float = 0.5
    use_trailing_stop: bool = True
    trail_pivot_lag: int = 2
    trail_buffer_ticks: int = 1
    stop_mode: str = "or_extreme"

    @property
    def stop_points(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def max_loss_usd(self) -> float:
        return self.stop_points * self.point_value * self.contracts

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        if self.as_of is not None:
            d["as_of"] = self.as_of.isoformat()
        d["stop_points"] = round(self.stop_points, 4)
        d["max_loss_usd"] = round(self.max_loss_usd, 2)
        if self.target_price is not None:
            d["target_price"] = round(self.target_price, 4)
            d["target_points"] = round(abs(self.target_price - self.entry_price), 4)
        d["instrument"] = "future"
        d["entry_model"] = "mes_5orb"
        return d
