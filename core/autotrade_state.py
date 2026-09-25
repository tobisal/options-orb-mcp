"""Persist desired auto-trade settings so the loop resumes after a restart.

When the dashboard (or host) restarts — e.g. after IB Gateway's nightly soft
restart and the watchdog bouncing the dashboard — in-memory auto-trade would
otherwise stay stopped until someone clicks Start again. This module stores
the last Start/Stop intent next to the journal DB.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.config import get_settings

_log = logging.getLogger(__name__)

_DEFAULT_NAME = "autotrade.json"


def state_path(db_path: Path | None = None) -> Path:
    settings = get_settings()
    base = (db_path or settings.resolved_db_path).parent
    return base / _DEFAULT_NAME


def default_state() -> dict[str, Any]:
    settings = get_settings()
    return {
        "enabled": False,
        "symbol": settings.default_symbol,
        "window": "auto",
        "demo": False,
        "interval": 60.0,
        "target_r": None,
        "risk_pct": round(settings.max_risk_per_trade * 100),
    }


def load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or state_path()
    base = default_state()
    if not p.is_file():
        return base
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("autotrade state unreadable (%s): %s", p, exc)
        return base
    if not isinstance(raw, dict):
        return base
    out = {**base, **{k: raw[k] for k in base if k in raw}}
    # Also pick up risk_pct if present in file but missing from older base merge
    if "risk_pct" in raw and "risk_pct" not in out:
        out["risk_pct"] = raw["risk_pct"]
    out["enabled"] = bool(out.get("enabled"))
    out["demo"] = bool(out.get("demo"))
    try:
        out["interval"] = float(out.get("interval") or 60.0)
    except (TypeError, ValueError):
        out["interval"] = 60.0
    tr = out.get("target_r")
    if tr in (None, ""):
        out["target_r"] = None
    else:
        try:
            out["target_r"] = float(tr)
        except (TypeError, ValueError):
            out["target_r"] = None
    rp = out.get("risk_pct")
    if rp in (None, ""):
        out["risk_pct"] = base["risk_pct"]
    else:
        try:
            out["risk_pct"] = float(rp)
        except (TypeError, ValueError):
            out["risk_pct"] = base["risk_pct"]
    out["symbol"] = str(out.get("symbol") or base["symbol"]).upper()
    out["window"] = str(out.get("window") or "auto")
    return out


def save_state(state: dict[str, Any], path: Path | None = None) -> Path:
    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(state.get("enabled")),
        "symbol": str(state.get("symbol") or "MES").upper(),
        "window": str(state.get("window") or "auto"),
        "demo": bool(state.get("demo")),
        "interval": float(state.get("interval") or 60.0),
        "target_r": state.get("target_r"),
        "risk_pct": state.get("risk_pct"),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return p


def should_autostart(state: dict[str, Any] | None = None) -> bool:
    """True when persisted intent is on, or AUTO_TRADE_AUTOSTART forces it."""
    settings = get_settings()
    if settings.auto_trade_autostart:
        return True
    st = state if state is not None else load_state()
    return bool(st.get("enabled"))


def start_kwargs(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Args for ``AutoTrader.start`` from file and optional env overrides."""
    settings = get_settings()
    st = {**(state if state is not None else load_state())}
    if settings.auto_trade_symbol:
        st["symbol"] = settings.auto_trade_symbol
    if settings.auto_trade_window:
        st["window"] = settings.auto_trade_window
    if settings.auto_trade_interval is not None:
        st["interval"] = settings.auto_trade_interval
    if settings.auto_trade_demo is not None:
        st["demo"] = settings.auto_trade_demo
    return {
        "symbol": st.get("symbol") or settings.default_symbol,
        "window": st.get("window") or "auto",
        "demo": bool(st.get("demo")),
        "interval": float(st.get("interval") or 60.0),
        "target_r": st.get("target_r"),
        "risk_pct": st.get("risk_pct"),
    }
