"""Swing-low / swing-high trailing stop for MES 5ORB."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.models import Bar, Direction


@dataclass
class SwingTrailingStop:
    """Pivot-based trail. Confirmed after ``pivot_lag`` bars on each side."""

    direction: Direction
    stop: float
    pivot_lag: int = 2
    buffer: float = 0.25  # points
    _lows: list[float] = field(default_factory=list)
    _highs: list[float] = field(default_factory=list)
    _history: list[float] = field(default_factory=list)

    def update(self, bar: Bar) -> float | None:
        """Ingest bar; return new stop if raised/lowered favorably, else None.

        Pivot confirmation uses lag: a swing low at index i is confirmed when
        we have ``pivot_lag`` bars after it and its low is strictly lower than
        the ``pivot_lag`` bars on either side.
        """
        self._lows.append(bar.low)
        self._highs.append(bar.high)
        self._history.append(self.stop)
        n = len(self._lows)
        lag = self.pivot_lag
        # Need lag before + center + lag after
        if n < 2 * lag + 1:
            return None

        center = n - 1 - lag
        if center < lag:
            return None

        changed = False
        if self.direction is Direction.LONG:
            cl = self._lows[center]
            left = self._lows[center - lag : center]
            right = self._lows[center + 1 : center + 1 + lag]
            if left and right and cl < min(left) and cl < min(right):
                candidate = cl - self.buffer
                if candidate > self.stop:
                    self.stop = candidate
                    changed = True
        elif self.direction is Direction.SHORT:
            ch = self._highs[center]
            left = self._highs[center - lag : center]
            right = self._highs[center + 1 : center + 1 + lag]
            if left and right and ch > max(left) and ch > max(right):
                candidate = ch + self.buffer
                if candidate < self.stop:
                    self.stop = candidate
                    changed = True

        return self.stop if changed else None

    def hit(self, bar: Bar) -> bool:
        """True if bar close breaches the trailing stop."""
        if self.direction is Direction.LONG:
            return bar.close < self.stop
        if self.direction is Direction.SHORT:
            return bar.close > self.stop
        return False
