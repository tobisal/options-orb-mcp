"""5ORB session windows and per-symbol futures config loader."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import time
from functools import lru_cache
from typing import Any

from core.config import REPO_ROOT
from core.strategy.mes_5orb.asia_range import AsiaRangeConfig, asia_config_from_raw
from core.strategy.mes_5orb.regime import RegimeFilterConfig, regime_config_from_raw
from core.strategy.mes_5orb.markets import (
    DEFAULT_FUTURES_SYMBOL,
    coerce_futures_symbol,
    get_futures_market,
    is_supported_futures,
    normalize_futures_symbol,
)

_LEGACY_CONFIG_PATH = REPO_ROOT / "configs" / "mes_5orb.json"


def _config_path_for(symbol: str):
    return REPO_ROOT / "configs" / f"{symbol.lower()}_5orb.json"


def _parse_hhmm(s: str) -> time:
    hh, mm = str(s).strip().split(":")[:2]
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class OpeningRangeFilter:
    min_range_points: float = 0.75
    max_range_points: float = 6.0
    # Optional: skip if OR width / mid > this fraction (Edgeful-style % filter).
    max_range_pct: float | None = None


@dataclass(frozen=True)
class RetestConfig:
    tolerance_ticks: int = 2
    timeout_bars: int = 12
    require_rejection_candle: bool = True


@dataclass(frozen=True)
class EntryConfig:
    """Entry model + direction / calendar filters (Edgeful-inspired knobs)."""

    # retest | close_break | fib_macd
    mode: str = "retest"
    # both | long | short
    allowed_directions: str = "both"
    # If True: first opposite close vs allowed direction invalidates the window.
    skip_if_opposite_first: bool = False
    # Monday=0 … Sunday=6; None = all days.
    allowed_weekdays: tuple[int, ...] | None = None


@dataclass(frozen=True)
class TrailingStopConfig:
    method: str = "swing_low"
    pivot_lag_bars: int = 2
    buffer_ticks: int = 1


@dataclass(frozen=True)
class ExitPolicyConfig:
    """Classic 5m ORB: SL = break of OR; TP = 2R or HOD with runners."""

    stop_mode: str = "or_extreme"
    stop_buffer_ticks: int = 1
    target_r: float = 2.5
    # r_multiple = target_r × risk; or_fraction = target_or_fraction × OR width.
    target_mode: str = "r_multiple"
    target_or_fraction: float = 0.5
    # Cap stop distance from entry (points). None = full OR extreme.
    max_stop_points: float | None = None
    # Fraction closed at primary target (2R/HOD); remainder is the runner.
    scale_fraction: float = 1.0
    use_hod_lod_target: bool = True
    # After scale: move stop to breakeven, trail runner with swings.
    move_stop_to_be: bool = True
    runner_trail: bool = True
    # Soft trail tiers: once price reaches ``arm`` of entry→target, ratchet
    # stop to lock ``lock`` of that same span (BE + that %). Empty disables.
    # Legacy ``profit_lock_arm`` / ``profit_lock_fraction`` seed the first tier
    # when ``profit_lock_tiers`` is omitted.
    profit_lock_arm: float = 0.75
    profit_lock_fraction: float = 0.35
    profit_lock_tiers: tuple[tuple[float, float], ...] = (
        (0.75, 0.35),
        (0.80, 0.50),
        (0.90, 0.75),
        (0.95, 0.85),
        (0.975, 0.90),
    )


@dataclass(frozen=True)
class MesSession:
    name: str
    or_start: time
    or_end: time
    search_end: time
    force_flat: time
    opening_range: OpeningRangeFilter = field(default_factory=OpeningRangeFilter)
    retest: RetestConfig = field(default_factory=RetestConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    enabled: bool = True
    # Optional per-session exit override; None → Mes5OrbConfig.exits.
    exits: ExitPolicyConfig | None = None
    # Optional override of risk.max_entries_per_session for this OR window.
    max_entries: int | None = None


@dataclass(frozen=True)
class MesRiskConfig:
    contracts: int = 1
    max_concurrent: int = 2
    # Percent of capital per trade (1–5). None → Settings.MAX_RISK_PER_TRADE.
    risk_pct: float | None = None
    # After a stop/force_flat, allow another break/retest in the same session.
    allow_reentry: bool = True
    max_entries_per_session: int = 2


@dataclass(frozen=True)
class Mes5OrbConfig:
    symbol: str = DEFAULT_FUTURES_SYMBOL
    point_value: float = 5.0
    tick_size: float = 0.25
    exchange: str = "CME"
    timezone: str = "America/New_York"
    sessions: tuple[MesSession, ...] = ()
    trailing_stop: TrailingStopConfig = field(default_factory=TrailingStopConfig)
    exits: ExitPolicyConfig = field(default_factory=ExitPolicyConfig)
    risk: MesRiskConfig = field(default_factory=MesRiskConfig)
    asia_range: AsiaRangeConfig = field(default_factory=AsiaRangeConfig)
    regime: RegimeFilterConfig = field(default_factory=RegimeFilterConfig)

    def session(self, name: str) -> MesSession | None:
        key = name.lower().replace(" ", "_")
        for s in self.sessions:
            if s.name == key or s.name.replace("_", "") == key.replace("_", ""):
                return s
        if key in ("ny", "newyork", "new_york"):
            return next((s for s in self.sessions if s.name == "new_york"), None)
        if key == "london":
            return next((s for s in self.sessions if s.name == "london"), None)
        return None

    def active_sessions_at(self, t: time) -> list[MesSession]:
        """Enabled sessions whose OR has started and force_flat has not yet passed."""
        return [
            s
            for s in self.sessions
            if s.enabled and s.or_start <= t < s.force_flat
        ]


def _exit_policy_from_raw(
    exits_raw: dict[str, Any] | None,
    *,
    defaults: ExitPolicyConfig | None = None,
) -> ExitPolicyConfig:
    """Parse an exits block; missing keys fall back to ``defaults`` or class defaults."""
    base = defaults or ExitPolicyConfig()
    raw = exits_raw or {}
    scale = float(raw.get("scale_fraction", base.scale_fraction))
    scale = min(max(scale, 0.0), 1.0)
    target_mode = str(raw.get("target_mode", base.target_mode)).lower().strip()
    if target_mode not in ("r_multiple", "or_fraction"):
        target_mode = base.target_mode
    max_stop = raw.get("max_stop_points", base.max_stop_points)
    arm = float(raw.get("profit_lock_arm", base.profit_lock_arm))
    lock = float(raw.get("profit_lock_fraction", base.profit_lock_fraction))
    arm = min(max(arm, 0.0), 1.0)
    lock = min(max(lock, 0.0), 1.0)
    tiers_raw = raw.get("profit_lock_tiers")
    tiers: list[tuple[float, float]] = []
    if isinstance(tiers_raw, list) and tiers_raw:
        for item in tiers_raw:
            if isinstance(item, dict):
                ta = item.get("arm", item.get("profit_lock_arm"))
                tl = item.get("lock", item.get("fraction", item.get("profit_lock_fraction")))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                ta, tl = item[0], item[1]
            else:
                continue
            if ta is None or tl is None:
                continue
            ta_f = min(max(float(ta), 0.0), 1.0)
            tl_f = min(max(float(tl), 0.0), 1.0)
            if ta_f > 0 and tl_f > 0:
                tiers.append((ta_f, tl_f))
    if not tiers:
        # Seed from legacy single arm/lock, else keep dataclass defaults.
        if "profit_lock_arm" in raw or "profit_lock_fraction" in raw:
            if arm > 0 and lock > 0:
                tiers = [(arm, lock)]
        else:
            tiers = list(base.profit_lock_tiers)
    tiers.sort(key=lambda t: t[0])
    # Keep first tier as the legacy single-pair fields for callers/UI.
    if tiers:
        arm, lock = tiers[0]
    return ExitPolicyConfig(
        stop_mode=str(raw.get("stop_mode", base.stop_mode)),
        stop_buffer_ticks=int(raw.get("stop_buffer_ticks", base.stop_buffer_ticks)),
        target_r=float(raw.get("target_r", base.target_r)),
        target_mode=target_mode,
        target_or_fraction=float(raw.get("target_or_fraction", base.target_or_fraction)),
        max_stop_points=(
            float(max_stop) if max_stop not in (None, "") else None
        ),
        scale_fraction=scale,
        use_hod_lod_target=bool(raw.get("use_hod_lod_target", base.use_hod_lod_target)),
        move_stop_to_be=bool(raw.get("move_stop_to_be", base.move_stop_to_be)),
        runner_trail=bool(raw.get("runner_trail", base.runner_trail)),
        profit_lock_arm=arm,
        profit_lock_fraction=lock,
        profit_lock_tiers=tuple(tiers),
    )


def resolve_session_exits(cfg: Mes5OrbConfig, session: MesSession) -> ExitPolicyConfig:
    """Per-session exits if set, else global config exits."""
    return session.exits if session.exits is not None else cfg.exits


def resolve_session_max_entries(cfg: Mes5OrbConfig, session: MesSession) -> int:
    """Per-session max entries if set, else global risk cap."""
    if session.max_entries is not None:
        return max(int(session.max_entries), 1)
    return max(int(cfg.risk.max_entries_per_session), 1)


def _session_from_raw(name: str, raw: dict[str, Any], *, defaults: OpeningRangeFilter) -> MesSession:
    or_raw = raw.get("opening_range") or {}
    rt_raw = raw.get("retest") or {}
    en_raw = raw.get("entry") or {}
    max_pct = or_raw.get("max_range_pct")
    weekdays_raw = en_raw.get("allowed_weekdays")
    weekdays: tuple[int, ...] | None = None
    if weekdays_raw is not None:
        weekdays = tuple(int(d) for d in weekdays_raw)
    mode = str(en_raw.get("mode", "retest")).lower().strip()
    if mode not in ("retest", "close_break", "fib_macd"):
        mode = "retest"
    dirs = str(en_raw.get("allowed_directions", "both")).lower().strip()
    if dirs not in ("both", "long", "short"):
        dirs = "both"
    sess_exits = None
    if raw.get("exits"):
        sess_exits = _exit_policy_from_raw(raw.get("exits") or {})
    max_entries_raw = raw.get("max_entries")
    max_entries = (
        max(int(max_entries_raw), 1) if max_entries_raw not in (None, "") else None
    )
    return MesSession(
        name=name,
        or_start=_parse_hhmm(str(raw.get("or_start", "09:30"))),
        or_end=_parse_hhmm(str(raw.get("or_end", "09:35"))),
        search_end=_parse_hhmm(str(raw.get("search_end", "15:00"))),
        force_flat=_parse_hhmm(str(raw.get("force_flat", "15:55"))),
        opening_range=OpeningRangeFilter(
            min_range_points=float(or_raw.get("min_range_points", defaults.min_range_points)),
            max_range_points=float(or_raw.get("max_range_points", defaults.max_range_points)),
            max_range_pct=(float(max_pct) if max_pct not in (None, "") else None),
        ),
        retest=RetestConfig(
            tolerance_ticks=int(rt_raw.get("tolerance_ticks", 2)),
            timeout_bars=int(rt_raw.get("timeout_bars", 12)),
            require_rejection_candle=bool(rt_raw.get("require_rejection_candle", True)),
        ),
        entry=EntryConfig(
            mode=mode,
            allowed_directions=dirs,
            skip_if_opposite_first=bool(en_raw.get("skip_if_opposite_first", False)),
            allowed_weekdays=weekdays,
        ),
        enabled=bool(raw.get("enabled", True)),
        exits=sess_exits,
        max_entries=max_entries,
    )


def _default_sessions(market) -> list[MesSession]:
    london_or = OpeningRangeFilter(market.london_min_range, market.london_max_range)
    ny_or = OpeningRangeFilter(market.ny_min_range, market.ny_max_range)
    return [
        _session_from_raw(
            "london",
            {
                "or_start": "03:00",
                "or_end": "03:05",
                "search_end": "08:00",
                "force_flat": "08:00",
            },
            defaults=london_or,
        ),
        _session_from_raw(
            "new_york",
            {
                "or_start": "09:30",
                "or_end": "09:35",
                "search_end": "15:00",
                "force_flat": "15:55",
            },
            defaults=ny_or,
        ),
    ]


def _load_raw(symbol: str) -> dict[str, Any]:
    path = _config_path_for(symbol)
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    # Back-compat: MES used configs/mes_5orb.json exclusively.
    if symbol == "MES" and _LEGACY_CONFIG_PATH.exists():
        with open(_LEGACY_CONFIG_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


@lru_cache
def load_mes_5orb_config(symbol: str | None = None) -> Mes5OrbConfig:
    """Load 5ORB config for a supported futures symbol (default MES)."""
    sym = coerce_futures_symbol(symbol)
    market = get_futures_market(sym)
    raw = _load_raw(sym)
    # If JSON declares a different symbol, prefer the requested one when supported.
    json_sym = normalize_futures_symbol(str(raw.get("symbol") or sym))
    if is_supported_futures(json_sym) and symbol is None and json_sym != sym:
        sym = json_sym
        market = get_futures_market(sym)

    sess_raw = raw.get("sessions") or {}
    london_defaults = OpeningRangeFilter(market.london_min_range, market.london_max_range)
    ny_defaults = OpeningRangeFilter(market.ny_min_range, market.ny_max_range)
    sessions: list[MesSession] = []
    if sess_raw:
        # Load every session block (london, london_mid, new_york, ny_mid, …).
        for name in sorted(sess_raw.keys(), key=lambda n: str(sess_raw[n].get("or_start", "99:99"))):
            defaults = ny_defaults if ("new_york" in name or name.startswith("ny")) else london_defaults
            sessions.append(_session_from_raw(name, sess_raw[name], defaults=defaults))
    if not sessions:
        sessions = _default_sessions(market)

    trail = raw.get("trailing_stop") or {}
    exits_raw = raw.get("exits") or {}
    risk = raw.get("risk") or {}
    asia = asia_config_from_raw(raw.get("asia_range") or {})
    regime = regime_config_from_raw(raw.get("regime") or {})
    global_exits = _exit_policy_from_raw(
        {
            **exits_raw,
            "stop_buffer_ticks": exits_raw.get(
                "stop_buffer_ticks", trail.get("buffer_ticks", 1)
            ),
        }
    )
    return Mes5OrbConfig(
        symbol=sym,
        point_value=float(raw.get("point_value", market.point_value)),
        tick_size=float(raw.get("tick_size", market.tick_size)),
        exchange=str(raw.get("exchange", market.exchange)).upper(),
        timezone=str(raw.get("timezone", "America/New_York")),
        sessions=tuple(sessions),
        trailing_stop=TrailingStopConfig(
            method=str(trail.get("method", "swing_low")),
            pivot_lag_bars=int(trail.get("pivot_lag_bars", 2)),
            buffer_ticks=int(trail.get("buffer_ticks", 1)),
        ),
        exits=global_exits,
        risk=MesRiskConfig(
            contracts=max(int(risk.get("contracts", 1)), 1),
            max_concurrent=max(int(risk.get("max_concurrent", 2)), 1),
            risk_pct=(
                float(risk["risk_pct"])
                if risk.get("risk_pct") not in (None, "")
                else None
            ),
            allow_reentry=bool(risk.get("allow_reentry", True)),
            max_entries_per_session=max(int(risk.get("max_entries_per_session", 2)), 1),
        ),
        asia_range=asia,
        regime=regime,
    )


def clear_mes_5orb_config_cache() -> None:
    load_mes_5orb_config.cache_clear()


def _apply_exit_knobs(
    base: ExitPolicyConfig, params: dict[str, Any], *, prefix: str = ""
) -> ExitPolicyConfig:
    """Overlay exit knobs; ``prefix`` e.g. ``london_`` / ``ny_`` for session keys."""
    from dataclasses import replace

    def _get(key: str) -> Any:
        if prefix:
            scoped = params.get(f"{prefix}{key}")
            if scoped is not None:
                return scoped
        return params.get(key)

    kw: dict[str, Any] = {}
    if _get("target_r") is not None:
        kw["target_r"] = float(_get("target_r"))
    if _get("scale_fraction") is not None:
        kw["scale_fraction"] = min(max(float(_get("scale_fraction")), 0.0), 1.0)
    if _get("stop_buffer_ticks") is not None:
        kw["stop_buffer_ticks"] = int(_get("stop_buffer_ticks"))
    if _get("use_hod_lod_target") is not None:
        kw["use_hod_lod_target"] = bool(_get("use_hod_lod_target"))
    if _get("move_stop_to_be") is not None:
        kw["move_stop_to_be"] = bool(_get("move_stop_to_be"))
    if _get("runner_trail") is not None:
        kw["runner_trail"] = bool(_get("runner_trail"))
    return replace(base, **kw) if kw else base


def _session_opt_prefix(session_name: str) -> str:
    if session_name == "london":
        return "london_"
    if session_name == "new_york":
        return "ny_"
    return f"{session_name}_"


def apply_mes_opt_params(cfg: Mes5OrbConfig, params: dict[str, Any] | None) -> Mes5OrbConfig:
    """Overlay optimiser knobs (exits / retest / risk / asia / per-session) onto a config.

    Global keys (``target_r``, ``tolerance_ticks``, …) update ``cfg.exits`` and all
    sessions' retest. Session-scoped keys (``london_target_r``, ``ny_tolerance_ticks``,
    …) write onto that session's ``exits`` / ``retest`` / ``entry`` / ``max_entries``
    without clobbering the other open.
    """
    from dataclasses import replace

    if not params:
        return cfg
    exits = _apply_exit_knobs(cfg.exits, params)

    risk_kw: dict[str, Any] = {}
    if params.get("allow_reentry") is not None:
        risk_kw["allow_reentry"] = bool(params["allow_reentry"])
    if params.get("max_entries_per_session") is not None:
        risk_kw["max_entries_per_session"] = max(int(params["max_entries_per_session"]), 1)
    risk = replace(cfg.risk, **risk_kw) if risk_kw else cfg.risk

    asia = cfg.asia_range
    asia_kw: dict[str, Any] = {}
    if params.get("asia_target_r") is not None:
        asia_kw["target_r"] = float(params["asia_target_r"])
    if params.get("asia_scale_fraction") is not None:
        asia_kw["scale_fraction"] = min(
            max(float(params["asia_scale_fraction"]), 0.0), 1.0
        )
    if asia_kw:
        asia = replace(asia, **asia_kw)

    regime = cfg.regime
    regime_kw: dict[str, Any] = {}
    if params.get("regime_enabled") is not None:
        regime_kw["enabled"] = bool(params["regime_enabled"])
    if params.get("regime_allowed_structures") is not None:
        regime_kw["allowed_structures"] = tuple(
            str(s).lower() for s in params["regime_allowed_structures"]
        )
    if params.get("regime_allowed_vol") is not None:
        regime_kw["allowed_vol"] = tuple(
            str(v).lower() for v in params["regime_allowed_vol"]
        )
    if params.get("regime_allowed_biases") is not None:
        regime_kw["allowed_biases"] = tuple(
            str(b).lower() for b in params["regime_allowed_biases"]
        )
    if params.get("regime_require_trend_align") is not None:
        regime_kw["require_trend_align"] = bool(params["regime_require_trend_align"])
    if params.get("regime_skip_flat") is not None:
        regime_kw["skip_flat"] = bool(params["regime_skip_flat"])
    if regime_kw:
        regime = replace(regime, **regime_kw)

    tol = params.get("tolerance_ticks")
    rej = params.get("require_rejection_candle")
    timeout = params.get("timeout_bars")
    updated: list[MesSession] = []
    for s in cfg.sessions:
        prefix = _session_opt_prefix(s.name)
        sess = s
        rt_kw: dict[str, Any] = {}
        if tol is not None:
            rt_kw["tolerance_ticks"] = int(tol)
        if rej is not None:
            rt_kw["require_rejection_candle"] = bool(rej)
        if timeout is not None:
            rt_kw["timeout_bars"] = max(int(timeout), 1)
        if params.get(f"{prefix}tolerance_ticks") is not None:
            rt_kw["tolerance_ticks"] = int(params[f"{prefix}tolerance_ticks"])
        if params.get(f"{prefix}require_rejection_candle") is not None:
            rt_kw["require_rejection_candle"] = bool(
                params[f"{prefix}require_rejection_candle"]
            )
        if params.get(f"{prefix}timeout_bars") is not None:
            rt_kw["timeout_bars"] = max(int(params[f"{prefix}timeout_bars"]), 1)
        if rt_kw:
            sess = replace(sess, retest=replace(sess.retest, **rt_kw))

        weekdays = params.get(f"{prefix}allowed_weekdays")
        if weekdays is not None:
            sess = replace(
                sess,
                entry=replace(
                    sess.entry,
                    allowed_weekdays=tuple(int(d) for d in weekdays),
                ),
            )

        if params.get(f"{prefix}max_entries") is not None:
            sess = replace(sess, max_entries=max(int(params[f"{prefix}max_entries"]), 1))

        if any(
            params.get(f"{prefix}{k}") is not None
            for k in (
                "target_r",
                "scale_fraction",
                "stop_buffer_ticks",
                "use_hod_lod_target",
                "move_stop_to_be",
                "runner_trail",
            )
        ):
            base_ex = sess.exits if sess.exits is not None else exits
            sess = replace(sess, exits=_apply_exit_knobs(base_ex, params, prefix=prefix))

        dirs = params.get(f"{prefix}allowed_directions")
        if dirs is not None:
            d = str(dirs).lower().strip()
            if d in ("both", "long", "short"):
                sess = replace(sess, entry=replace(sess.entry, allowed_directions=d))
        updated.append(sess)

    return replace(
        cfg,
        exits=exits,
        risk=risk,
        asia_range=asia,
        regime=regime,
        sessions=tuple(updated),
    )
