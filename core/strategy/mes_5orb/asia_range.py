"""ICT Asian Range (Market Maker Primer) — Judas sweep + reclaim.

Liquidity pools used for SSL/BSL sweeps:
- Asia range high/low (20:00–00:00 ET)
- Prior New York RTH high/low (default 09:30–16:00 ET)
- Previous day high/low (full ET calendar day)
- Previous session high/low (default prior London 02:00–08:00 ET)

Bias proxy from price vs Asia EQ at London search open.
Bullish: sweep any SSL pool, reclaim close back above → long.
Bearish: sweep any BSL pool, reclaim close back below → short.
On OHLC bars (paper or live without ticks), reclaim also fires when the
liquidity level sits inside the candle H/L and close holds the reclaim side.
Target: furthest opposite liquidity (or 2R). Prefer partials over aggressive trails.

Refs:
- https://forum.ictsharks.com/t/ict-forex-market-maker-primer-course-implementing-the-asian-range/234
- https://innercircletrader.net/tutorials/ict-asian-range/
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum

import pytz

from core.models import Bar, Direction

EASTERN = pytz.timezone("America/New_York")


def _to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = pytz.utc.localize(ts)
    return ts.astimezone(EASTERN)


class AsiaSetupState(str, Enum):
    IDLE = "idle"
    SWEPT = "swept"
    RECLAIMED = "reclaimed"
    INVALID = "invalid"


@dataclass(frozen=True)
class AsiaRangeConfig:
    enabled: bool = False
    # Range build window (America/New_York wall clock).
    range_start: time = time(20, 0)
    range_end: time = time(0, 0)  # midnight — ends at 00:00 of the trade day
    # Prior NY RTH used as additional BSL/SSL for Judas sweeps.
    use_ny_liquidity: bool = True
    ny_session_start: time = time(9, 30)
    ny_session_end: time = time(16, 0)
    # Previous calendar day H/L (PDH/PDL).
    use_pd_liquidity: bool = True
    # Session immediately before Asia on the prior cycle (default: London).
    use_prev_session_liquidity: bool = True
    prev_session_start: time = time(2, 0)
    prev_session_end: time = time(8, 0)
    # Judas search / manage (London → early NY).
    search_start: time = time(2, 0)
    search_end: time = time(11, 0)
    force_flat: time = time(11, 55)
    min_width_points: float = 2.0
    max_width_points: float = 40.0
    stop_buffer_ticks: int = 2
    # "opposite_extreme" = furthest opposite liquidity; "2R" = risk multiple.
    target_mode: str = "opposite_extreme"
    target_r: float = 1.5
    scale_fraction: float = 0.5
    # If True, only take Judas on the EQ-implied side (above EQ → bearish sweep).
    require_eq_bias: bool = True
    # Monday=0 … Sunday=6; None = all days. Used to skip weak Asia weekdays.
    allowed_weekdays: tuple[int, ...] | None = None

    def allows_day(self, day: date) -> bool:
        if self.allowed_weekdays is None:
            return True
        return day.weekday() in self.allowed_weekdays


@dataclass(frozen=True)
class AsiaRange:
    """Asia range that feeds London/NY — ``trade_day`` is the calendar day after range end."""

    trade_day: date
    high: float
    low: float
    eq: float
    midnight_open: float | None
    bar_count: int
    # Prior NY RTH extremes (session day = trade_day - 1).
    ny_high: float | None = None
    ny_low: float | None = None
    # Previous ET calendar day high/low.
    pd_high: float | None = None
    pd_low: float | None = None
    # Previous session (default prior London) high/low.
    prev_session_high: float | None = None
    prev_session_low: float | None = None
    skipped: bool = False
    skip_reason: str = ""

    @property
    def width(self) -> float:
        return self.high - self.low


@dataclass
class AsiaJudasSetup:
    trade_day: date
    direction: Direction
    asia_high: float
    asia_low: float
    asia_eq: float
    sweep_extreme: float
    entry_bar_index: int | None = None
    entry_price: float | None = None
    initial_stop: float | None = None
    target_price: float | None = None
    target_label: str = "asia_opposite"
    sweep_levels: tuple[str, ...] = ()
    ny_high: float | None = None
    ny_low: float | None = None
    pd_high: float | None = None
    pd_low: float | None = None
    prev_session_high: float | None = None
    prev_session_low: float | None = None
    state: AsiaSetupState = AsiaSetupState.IDLE
    notes: str = ""


def compute_session_hl(
    bars: list[Bar],
    session_day: date,
    *,
    start: time,
    end: time,
) -> tuple[float, float] | None:
    """High/low of ``[start, end)`` on ``session_day`` (America/New_York)."""
    sess_bars: list[Bar] = []
    for b in bars:
        et = _to_et(b.ts)
        if et.date() != session_day:
            continue
        if start <= et.time() < end:
            sess_bars.append(b)
    if not sess_bars:
        return None
    return max(b.high for b in sess_bars), min(b.low for b in sess_bars)


def compute_ny_session_hl(
    bars: list[Bar],
    session_day: date,
    *,
    start: time = time(9, 30),
    end: time = time(16, 0),
) -> tuple[float, float] | None:
    """High/low of New York RTH on ``session_day`` (America/New_York)."""
    return compute_session_hl(bars, session_day, start=start, end=end)


def compute_day_hl(
    bars: list[Bar],
    day: date,
) -> tuple[float, float] | None:
    """Full ET calendar-day high/low (PDH/PDL when ``day`` is prior day)."""
    day_bars = [b for b in bars if _to_et(b.ts).date() == day]
    if not day_bars:
        return None
    return max(b.high for b in day_bars), min(b.low for b in day_bars)


def _attach_pools(
    bars: list[Bar],
    prev: date,
    cfg: AsiaRangeConfig,
) -> dict[str, float | None]:
    ny_high = ny_low = pd_high = pd_low = ps_high = ps_low = None
    if cfg.use_ny_liquidity:
        ny = compute_ny_session_hl(
            bars, prev, start=cfg.ny_session_start, end=cfg.ny_session_end
        )
        if ny is not None:
            ny_high, ny_low = ny
    if cfg.use_pd_liquidity:
        pd = compute_day_hl(bars, prev)
        if pd is not None:
            pd_high, pd_low = pd
    if cfg.use_prev_session_liquidity:
        ps = compute_session_hl(
            bars,
            prev,
            start=cfg.prev_session_start,
            end=cfg.prev_session_end,
        )
        if ps is not None:
            ps_high, ps_low = ps
    return {
        "ny_high": ny_high,
        "ny_low": ny_low,
        "pd_high": pd_high,
        "pd_low": pd_low,
        "prev_session_high": ps_high,
        "prev_session_low": ps_low,
    }


def compute_asia_range(
    bars: list[Bar],
    trade_day: date,
    cfg: AsiaRangeConfig,
) -> AsiaRange | None:
    """Build Asia H/L for the window ending at midnight of ``trade_day``.

    Default window: previous calendar day 20:00 ET through 00:00 ET on ``trade_day``
    (midnight open bar is recorded but post-midnight prices are not in the range).
    Also attaches NY RTH, previous-day, and previous-session liquidity when enabled.
    """
    prev = trade_day - timedelta(days=1)
    range_bars: list[Bar] = []
    midnight_open: float | None = None

    for b in bars:
        et = _to_et(b.ts)
        d, t = et.date(), et.time()
        if d == prev and t >= cfg.range_start:
            range_bars.append(b)
        elif d == trade_day and cfg.range_end != time(0, 0) and t < cfg.range_end:
            range_bars.append(b)
        if d == trade_day and midnight_open is None:
            midnight_open = b.open

    if not range_bars:
        return None

    hi = max(b.high for b in range_bars)
    lo = min(b.low for b in range_bars)
    width = hi - lo
    eq = (hi + lo) / 2.0
    pools = _attach_pools(bars, prev, cfg)

    base_kw = dict(
        trade_day=trade_day,
        high=hi,
        low=lo,
        eq=eq,
        midnight_open=midnight_open,
        bar_count=len(range_bars),
        **pools,
    )
    if width < cfg.min_width_points:
        return AsiaRange(
            **base_kw,
            skipped=True,
            skip_reason=f"asia range too narrow ({width:.2f} < {cfg.min_width_points})",
        )
    if width > cfg.max_width_points:
        return AsiaRange(
            **base_kw,
            skipped=True,
            skip_reason=f"asia range too wide ({width:.2f} > {cfg.max_width_points})",
        )
    return AsiaRange(**base_kw)


def _bias_at_search_open(
    bars: list[Bar],
    trade_day: date,
    ar: AsiaRange,
    cfg: AsiaRangeConfig,
) -> Direction:
    """EQ bias: above EQ → expect bearish Judas (sweep high); below → bullish."""
    for b in bars:
        et = _to_et(b.ts)
        if et.date() != trade_day:
            continue
        if et.time() >= cfg.search_start:
            if b.close >= ar.eq:
                return Direction.SHORT
            return Direction.LONG
    return Direction.NEUTRAL


def _ssl_levels(ar: AsiaRange) -> list[tuple[str, float]]:
    """Sell-side liquidity (lows) across Asia / NY / PD / previous session."""
    levels = [("asia_low", ar.low)]
    if ar.ny_low is not None:
        levels.append(("ny_low", ar.ny_low))
    if ar.pd_low is not None:
        levels.append(("pd_low", ar.pd_low))
    if ar.prev_session_low is not None:
        levels.append(("prev_session_low", ar.prev_session_low))
    return levels


def _bsl_levels(ar: AsiaRange) -> list[tuple[str, float]]:
    """Buy-side liquidity (highs) across Asia / NY / PD / previous session."""
    levels = [("asia_high", ar.high)]
    if ar.ny_high is not None:
        levels.append(("ny_high", ar.ny_high))
    if ar.pd_high is not None:
        levels.append(("pd_high", ar.pd_high))
    if ar.prev_session_high is not None:
        levels.append(("prev_session_high", ar.prev_session_high))
    return levels


def _opposite_target(
    direction: Direction,
    ar: AsiaRange,
) -> tuple[float, str]:
    """Furthest opposite liquidity pool for primary target."""
    if direction is Direction.LONG:
        candidates = _bsl_levels(ar)
        name, px = max(candidates, key=lambda x: x[1])
        return px, name
    candidates = _ssl_levels(ar)
    name, px = min(candidates, key=lambda x: x[1])
    return px, name


def detect_asia_judas(
    bars: list[Bar],
    ar: AsiaRange,
    cfg: AsiaRangeConfig,
    *,
    tick_size: float = 0.25,
    after_bar_index: int | None = None,
) -> AsiaJudasSetup | None:
    """Scan search window for liquidity sweep (Asia/NY/PD/prev session) + reclaim."""
    if ar.skipped or ar.high <= ar.low:
        return None

    bias = _bias_at_search_open(bars, ar.trade_day, ar, cfg)
    if cfg.require_eq_bias and bias is Direction.NEUTRAL:
        return None

    buf = cfg.stop_buffer_ticks * tick_size
    post: list[tuple[int, Bar]] = []
    for i, b in enumerate(bars):
        if after_bar_index is not None and i <= after_bar_index:
            continue
        et = _to_et(b.ts)
        if et.date() != ar.trade_day:
            continue
        if cfg.search_start <= et.time() < cfg.search_end:
            post.append((i, b))
    if not post:
        return None

    allow_long = (not cfg.require_eq_bias) or bias is Direction.LONG
    allow_short = (not cfg.require_eq_bias) or bias is Direction.SHORT

    ssl = _ssl_levels(ar)
    bsl = _bsl_levels(ar)

    swept_ssl: set[str] = set()
    swept_bsl: set[str] = set()
    sweep_low = min(lvl for _, lvl in ssl)
    sweep_high = max(lvl for _, lvl in bsl)

    def _setup(
        *,
        direction: Direction,
        entry_i: int,
        entry: float,
        stop: float,
        target: float,
        label: str,
        extreme: float,
        names: tuple[str, ...],
        notes: str,
    ) -> AsiaJudasSetup:
        return AsiaJudasSetup(
            trade_day=ar.trade_day,
            direction=direction,
            asia_high=ar.high,
            asia_low=ar.low,
            asia_eq=ar.eq,
            sweep_extreme=extreme,
            entry_bar_index=entry_i,
            entry_price=entry,
            initial_stop=stop,
            target_price=target,
            target_label=label,
            sweep_levels=names,
            ny_high=ar.ny_high,
            ny_low=ar.ny_low,
            pd_high=ar.pd_high,
            pd_low=ar.pd_low,
            prev_session_high=ar.prev_session_high,
            prev_session_low=ar.prev_session_low,
            state=AsiaSetupState.RECLAIMED,
            notes=notes,
        )

    for i, b in post:
        if allow_long:
            for name, lvl in ssl:
                if b.low < lvl:
                    swept_ssl.add(name)
                    sweep_low = min(sweep_low, b.low)
        if allow_short:
            for name, lvl in bsl:
                if b.high > lvl:
                    swept_bsl.add(name)
                    sweep_high = max(sweep_high, b.high)

        if swept_ssl:
            reclaim = max(lvl for name, lvl in ssl if name in swept_ssl)
            # OHLC proxy: reclaim if close clears the level, or the bar traded
            # back through it (level inside H/L) and closed back on the long side.
            in_bar = b.low <= reclaim <= b.high
            if b.close > reclaim or (in_bar and b.close >= reclaim):
                entry = float(b.close)
                stop = sweep_low - buf
                risk = abs(entry - stop)
                if risk <= 0:
                    continue
                if cfg.target_mode == "2R":
                    target = entry + cfg.target_r * risk
                    label = "2R"
                else:
                    target, label = _opposite_target(Direction.LONG, ar)
                names = tuple(sorted(swept_ssl))
                how = "close" if b.close > reclaim else "OHLC range"
                return _setup(
                    direction=Direction.LONG,
                    entry_i=i,
                    entry=entry,
                    stop=stop,
                    target=target,
                    label=label,
                    extreme=sweep_low,
                    names=names,
                    notes=(
                        f"asia Judas long: SSL sweep ({', '.join(names)}) "
                        f"+ reclaim ({how})"
                    ),
                )
        if swept_bsl:
            reclaim = min(lvl for name, lvl in bsl if name in swept_bsl)
            in_bar = b.low <= reclaim <= b.high
            if b.close < reclaim or (in_bar and b.close <= reclaim):
                entry = float(b.close)
                stop = sweep_high + buf
                risk = abs(entry - stop)
                if risk <= 0:
                    continue
                if cfg.target_mode == "2R":
                    target = entry - cfg.target_r * risk
                    label = "2R"
                else:
                    target, label = _opposite_target(Direction.SHORT, ar)
                names = tuple(sorted(swept_bsl))
                how = "close" if b.close < reclaim else "OHLC range"
                return _setup(
                    direction=Direction.SHORT,
                    entry_i=i,
                    entry=entry,
                    stop=stop,
                    target=target,
                    label=label,
                    extreme=sweep_high,
                    names=names,
                    notes=(
                        f"asia Judas short: BSL sweep ({', '.join(names)}) "
                        f"+ reclaim ({how})"
                    ),
                )

    return None


def asia_config_from_raw(raw: dict | None) -> AsiaRangeConfig:
    raw = raw or {}

    def _t(key: str, default: str) -> time:
        hh, mm = str(raw.get(key, default)).split(":")[:2]
        return time(int(hh), int(mm))

    weekdays_raw = raw.get("allowed_weekdays")
    weekdays: tuple[int, ...] | None = None
    if weekdays_raw is not None:
        weekdays = tuple(int(d) for d in weekdays_raw)
    return AsiaRangeConfig(
        enabled=bool(raw.get("enabled", False)),
        range_start=_t("range_start", "20:00"),
        range_end=_t("range_end", "00:00"),
        use_ny_liquidity=bool(raw.get("use_ny_liquidity", True)),
        ny_session_start=_t("ny_session_start", "09:30"),
        ny_session_end=_t("ny_session_end", "16:00"),
        use_pd_liquidity=bool(raw.get("use_pd_liquidity", True)),
        use_prev_session_liquidity=bool(raw.get("use_prev_session_liquidity", True)),
        prev_session_start=_t("prev_session_start", "02:00"),
        prev_session_end=_t("prev_session_end", "08:00"),
        search_start=_t("search_start", "02:00"),
        search_end=_t("search_end", "11:00"),
        force_flat=_t("force_flat", "11:55"),
        min_width_points=float(raw.get("min_width_points", 2.0)),
        max_width_points=float(raw.get("max_width_points", 40.0)),
        stop_buffer_ticks=int(raw.get("stop_buffer_ticks", 2)),
        target_mode=str(raw.get("target_mode", "opposite_extreme")),
        target_r=float(raw.get("target_r", 1.5)),
        scale_fraction=min(max(float(raw.get("scale_fraction", 0.5)), 0.0), 1.0),
        require_eq_bias=bool(raw.get("require_eq_bias", True)),
        allowed_weekdays=weekdays,
    )
