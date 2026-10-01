"""Plain-text Discord replies for dashboard JSON payloads."""

from __future__ import annotations

import json
from typing import Any


def clip(text: str, limit: int = 1900) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n…(truncated)"


def _sign(n: float | None) -> str:
    if n is None:
        return "-"
    return f"{n:+.2f}"


def format_status(summary: dict[str, Any], auto: dict[str, Any] | None = None) -> str:
    lines: list[str] = []
    if summary.get("environment") or summary.get("paper_equity") is not None:
        env = summary.get("environment", "?")
        equity = summary.get("paper_equity")
        if equity is None:
            equity = summary.get("starting_capital")
        daily = summary.get("daily_pnl")
        if daily is None:
            daily = summary.get("daily_realised_pnl")
        ibkr = "connected" if summary.get("ibkr_connected") else "offline"
        lines.extend(
            [
                f"**Paper account** ({env})",
                f"Equity {equity} {summary.get('account_currency', '')}  ·  "
                f"daily {_sign(daily)}  ·  open mark {_sign(summary.get('open_unrealized_pnl'))}",
                f"Open {summary.get('open_positions')}/{summary.get('max_open_positions')}  ·  "
                f"kill switch {'TRIPPED' if summary.get('daily_kill_switch_tripped') else 'OK'}  ·  IBKR {ibkr}",
            ]
        )
        strat = summary.get("strategy_by_window") or {}
        if strat:
            bits = []
            for window, st in strat.items():
                p = (st or {}).get("params") or {}
                src = (st or {}).get("source") or "?"
                bits.append(
                    f"{window.replace('_', ' ')}: {src} OR {p.get('opening_range_minutes')}m "
                    f"buf {p.get('breakout_buffer_atr')} str {p.get('min_strength')}"
                )
            lines.append("Params: " + " · ".join(bits))
    if auto:
        state = "RUNNING" if auto.get("running") else "stopped"
        lines.append(
            f"Auto-trade **{state}**  {auto.get('symbol', '')}/{auto.get('window', '')}  "
            f"cycles {auto.get('cycles', 0)}  placed {auto.get('trades_placed', 0)}"
        )
        log_rows = auto.get("log") or []
        if log_rows:
            last = log_rows[0] if isinstance(log_rows[0], dict) else {}
            lines.append(f"Last: {last.get('msg', '')}")
    return clip("\n".join(lines) if lines else "No status.")


def format_signals(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return clip(f"Signals failed: {payload['error']}")
    lines = [
        f"**Signals** {payload.get('symbol', '')}  source {payload.get('data_source', '?')}"
    ]
    if payload.get("warning"):
        lines.append(str(payload["warning"]))
    for s in payload.get("signals") or []:
        brk = "BREAKOUT " + str(s.get("direction", "")).upper() if s.get("breakout") else "no breakout"
        lines.append(
            f"• **{str(s.get('window', '')).replace('_', ' ')}**  {brk}  "
            f"regime {s.get('regime')}  last {s.get('last_price')}  "
            f"OR {s.get('range_low')}–{s.get('range_high')}  "
            f"str {s.get('strength')}"
        )
    return clip("\n".join(lines))


def format_preview(plan: dict[str, Any]) -> str:
    if not plan.get("ok"):
        return clip(plan.get("reason") or plan.get("error") or "No trade.")
    sig = plan.get("signal") or {}
    p = plan.get("plan") or {}
    risk = plan.get("risk") or {}
    lines = [
        f"**Preview** {p.get('symbol', sig.get('symbol', ''))}  "
        f"{sig.get('window')}  {sig.get('direction')}  "
        f"{'tradeable' if plan.get('tradeable') else 'blocked'}",
        f"{p.get('spread_type')}  x{p.get('contracts')}  "
        f"debit {p.get('net_debit')}  max loss {p.get('max_loss')}  "
        f"max profit {p.get('max_profit')}",
        f"Long {((p.get('long_leg') or {}).get('strike'))} "
        f"{((p.get('long_leg') or {}).get('right'))} / "
        f"short {((p.get('short_leg') or {}).get('strike'))}  "
        f"expiry {p.get('expiry')}",
    ]
    reasons = risk.get("reasons") or []
    if reasons:
        lines.append("Risk: " + "; ".join(reasons[:4]))
    return clip("\n".join(lines))


def format_positions(payload: dict[str, Any]) -> str:
    opens = payload.get("open_trades") or []
    if not opens:
        return "No open journal positions."
    lines = [
        f"**Open positions**  mark {_sign(payload.get('open_unrealized_pnl'))}  "
        f"equity {payload.get('paper_equity', '-')}"
    ]
    for t in opens[:12]:
        pnl = t.get("unrealized_pnl")
        lines.append(
            f"• #{t.get('id')} {t.get('symbol')} {t.get('window')} "
            f"{t.get('spread_type')} x{t.get('contracts')}  "
            f"entry {t.get('entry_price')} mark {t.get('mark', '-')}  "
            f"live {_sign(pnl)}"
        )
    return clip("\n".join(lines))


def format_trades(payload: dict[str, Any]) -> str:
    trades = payload.get("trades") or []
    if not trades:
        return "No trades recorded yet."
    lines = [f"**Trades** ({payload.get('count', len(trades))})"]
    for t in trades[:12]:
        pnl = t.get("unrealized_pnl") if t.get("status") == "open" else t.get("pnl")
        lines.append(
            f"• #{t.get('id')} {t.get('created_at', '')[:16]}  "
            f"{t.get('symbol')} {t.get('window')} {t.get('spread_type')}  "
            f"{t.get('status')}  {_sign(pnl)}"
        )
    return clip("\n".join(lines))


def format_optimise(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return clip(f"Optimise failed: {payload['error']}")
    top = payload.get("top") or []
    lines = [
        f"**Optimise** {payload.get('symbol')} {payload.get('window')}  "
        f"{payload.get('combinations_tested') or len(top)} combos  "
        f"{payload.get('method') or 'grid'}"
    ]
    for i, row in enumerate(top[:5], start=1):
        p = row.get("params") or {}
        m = row.get("metrics") or {}
        if p.get("target_r") is not None:
            lines.append(
                f"{i}. {p.get('target_r')}R scale {p.get('scale_fraction')} "
                f"buf {p.get('stop_buffer_ticks')} tol {p.get('tolerance_ticks')}  "
                f"score {row.get('score')}  n={m.get('trades')} exp {m.get('expectancy')}"
            )
        else:
            lines.append(
                f"{i}. OR {p.get('opening_range_minutes')}m buf {p.get('breakout_buffer_atr')} "
                f"str {p.get('min_strength')}  score {row.get('score')}  "
                f"n={m.get('trades')} exp {m.get('expectancy')}"
            )
    if payload.get("persisted_id"):
        lines.append(f"Saved as backtest #{payload['persisted_id']} (not applied until you select it).")
    return clip("\n".join(lines))


def format_nightly(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return clip(f"Nightly failed: {payload.get('error')}")
    lines = [
        f"**Nightly rank** {payload.get('symbol')}  "
        f"applied {payload.get('applied_windows')}/3  "
        f"{payload.get('bars')} bars"
    ]
    if payload.get("warning"):
        lines.append(str(payload["warning"]))
    for row in payload.get("windows") or []:
        best = row.get("best") or {}
        p = best.get("params") or {}
        flag = "APPLIED" if row.get("applied") else "SKIP"
        lines.append(
            f"• {flag} {row.get('window')}  OR {p.get('opening_range_minutes')}m "
            f"buf {p.get('breakout_buffer_atr')} str {p.get('min_strength')}  "
            f"{row.get('reason')}"
        )
    return clip("\n".join(lines))


def format_log_line(entry: dict[str, Any]) -> str:
    level = str(entry.get("level") or "info")
    msg = str(entry.get("msg") or "")
    t = str(entry.get("t") or "")[-8:]
    if level == "trade":
        upper = msg.upper()
        if upper.startswith("PLACED") or " PLACED " in f" {upper}":
            return clip(f"🟢 **TRADE PLACED** `{t}`\n{msg}")
        if upper.startswith("CLOSED") or "CLOSED JOURNAL" in upper:
            return clip(f"🔴 **TRADE CLOSED** `{t}`\n{msg}")
        return clip(f"⚡ **TRADE** `{t}`\n{msg}")
    return f"`{t}` **{level}** {msg}"


def format_trade_alert(trade: dict[str, Any], *, event: str) -> str:
    """Discord alert for a journal row (open = entry fill, closed = exit fill)."""
    tid = trade.get("id") or trade.get("trade_id") or "?"
    symbol = trade.get("symbol") or "?"
    window = str(trade.get("window") or "").replace("_", " ")
    direction = str(trade.get("direction") or "").upper()
    contracts = trade.get("contracts") or 1
    entry = trade.get("entry_price")
    exit_px = trade.get("exit_price")
    pnl = trade.get("pnl")
    status = str(trade.get("status") or "")
    notes = str(trade.get("notes") or "")
    plan: dict[str, Any] = {}
    raw_plan = trade.get("plan_json")
    if isinstance(raw_plan, dict):
        plan = raw_plan
    elif isinstance(raw_plan, str) and raw_plan.strip():
        try:
            loaded = json.loads(raw_plan)
            if isinstance(loaded, dict):
                plan = loaded
        except (TypeError, ValueError, json.JSONDecodeError):
            plan = {}
    session = plan.get("session_name") or window
    stop = plan.get("stop_loss_price")
    if stop is None:
        stop = plan.get("stop_price")
    target = plan.get("target_price")
    target_label = plan.get("target_label") or "TP"
    env = trade.get("environment") or ""

    if event == "placed" or status == "open":
        lines = [
            f"🟢 **FILLED / PLACED** #{tid}  {symbol}  {direction}  x{contracts}",
            f"Session **{session}**  ·  {env}",
            f"Entry `{entry}`  ·  Stop `{stop}`  ·  {target_label} `{target}`",
        ]
        if notes:
            lines.append(notes.split("|")[0].strip()[:160])
        return clip("\n".join(lines))

    lines = [
        f"🔴 **CLOSED** #{tid}  {symbol}  {direction}  x{contracts}",
        f"Session **{session}**  ·  pnl {_sign(pnl if isinstance(pnl, (int, float)) else None)}",
        f"Entry `{entry}` → exit `{exit_px}`",
    ]
    if notes:
        # Prefer exit= reason fragment when present.
        exit_bit = ""
        for part in notes.split("|"):
            part = part.strip()
            if part.startswith("exit="):
                exit_bit = part
                break
        lines.append(exit_bit or notes.split("|")[0].strip()[:160])
    return clip("\n".join(lines))


def should_relay_log(entry: dict[str, Any], *, verbose: bool) -> bool:
    if verbose:
        return True
    level = str(entry.get("level") or "")
    msg = str(entry.get("msg") or "").upper()
    # Place / fill alerts are owned by the journal trade pump (richer format).
    if level == "trade" and (msg.startswith("PLACED") or msg.startswith("CLOSED")):
        return False
    return level in {"trade", "error", "warn", "start", "stop"}
