"""SQLite persistence for the trade journal and backtest results.

Deliberately dependency-light (stdlib ``sqlite3``) so the system is trivially
replicable on any device: the database is a single file under ``data/``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import get_settings
from core.models import (
    Direction,
    Regime,
    SessionWindow,
    SpreadType,
    TradeRecord,
    TradeStatus,
)
from core.timeutils import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    environment     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    window          TEXT NOT NULL,
    regime          TEXT NOT NULL,
    spread_type     TEXT NOT NULL,
    direction       TEXT NOT NULL,
    contracts       INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    max_loss        REAL NOT NULL,
    max_profit      REAL NOT NULL,
    target_r        REAL NOT NULL,
    status          TEXT NOT NULL,
    exit_price      REAL,
    pnl             REAL,
    closed_at       TEXT,
    signal_strength REAL NOT NULL DEFAULT 0,
    order_ref       TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    plan_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS backtests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    label         TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    window        TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    metrics_json  TEXT NOT NULL,
    is_out_of_sample INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS active_strategy (
    symbol      TEXT NOT NULL,
    window      TEXT NOT NULL,
    backtest_id INTEGER,
    label       TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    PRIMARY KEY (symbol, window)
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _path_looks_cloud_synced(path: Path) -> bool:
    """SQLite WAL on OneDrive/Documents is a common source of disk I/O errors."""
    try:
        resolved = str(path.resolve()).lower()
    except OSError:
        resolved = str(path).lower()
    markers = ("\\onedrive", "\\dropbox", "\\google drive", "\\icloud")
    if any(m in resolved for m in markers):
        return True
    onedrive = (
        os.environ.get("OneDrive")
        or os.environ.get("OneDriveConsumer")
        or os.environ.get("OneDriveCommercial")
        or ""
    )
    if onedrive:
        try:
            root = str(Path(onedrive).resolve()).lower()
        except OSError:
            root = onedrive.lower()
        if root and resolved.startswith(root):
            return True
        # Known Folder Move keeps C:\Users\...\Documents while files live in OneDrive.
        if "\\documents\\" in resolved or resolved.endswith("\\documents"):
            return True
    return resolved.startswith("\\\\")


def _is_disk_io(exc: BaseException) -> bool:
    return "disk i/o" in str(exc).lower()


def _io_error_hint(path: Path) -> str:
    return (
        f"SQLite disk I/O error at {path}. Usually the disk is full, OneDrive is "
        "syncing this folder, or trades.db is damaged. Close other dashboard "
        "processes, right-click the data folder → Always keep on this device, "
        "or set DB_PATH to a local file such as "
        r"%LOCALAPPDATA%\options-orb\trades.db"
    )


def _raise_io(path: Path, exc: sqlite3.OperationalError) -> None:
    if "SQLite disk I/O error at" in str(exc):
        raise exc
    raise sqlite3.OperationalError(_io_error_hint(path)) from exc


class Database:
    """Thin wrapper over a SQLite file with helpers for trades & backtests."""

    def __init__(self, path: str | Path | None = None) -> None:
        settings = get_settings()
        self.path = Path(path) if path is not None else settings.resolved_db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cloud_fs = _path_looks_cloud_synced(self.path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        last: sqlite3.OperationalError | None = None
        for attempt in range(3):
            try:
                conn = sqlite3.connect(str(self.path), timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA temp_store=MEMORY")
                mode = "DELETE" if self._cloud_fs else "WAL"
                try:
                    conn.execute(f"PRAGMA journal_mode={mode}")
                except sqlite3.OperationalError:
                    conn.execute("PRAGMA journal_mode=DELETE")
                return conn
            except sqlite3.OperationalError as exc:
                last = exc
                if not _is_disk_io(exc) or attempt == 2:
                    break
                time.sleep(0.25 * (attempt + 1))
        assert last is not None
        if _is_disk_io(last):
            _raise_io(self.path, last)
        raise last

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            if _is_disk_io(exc):
                _raise_io(self.path, exc)
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
            if "plan_json" not in cols:
                conn.execute(
                    "ALTER TABLE trades ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}'"
                )

    # --- trades ------------------------------------------------------------
    def insert_trade(self, trade: TradeRecord) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    created_at, environment, symbol, window, regime, spread_type,
                    direction, contracts, entry_price, max_loss, max_profit,
                    target_r, status, exit_price, pnl, closed_at, signal_strength,
                    order_ref, notes, plan_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _iso(trade.created_at),
                    trade.environment,
                    trade.symbol,
                    trade.window.value,
                    trade.regime.value,
                    trade.spread_type.value,
                    trade.direction.value,
                    trade.contracts,
                    trade.entry_price,
                    trade.max_loss,
                    trade.max_profit,
                    trade.target_r,
                    trade.status.value,
                    trade.exit_price,
                    trade.pnl,
                    _iso(trade.closed_at),
                    trade.signal_strength,
                    trade.order_ref,
                    trade.notes,
                    trade.plan_json or "{}",
                ),
            )
            return int(cur.lastrowid)

    def close_trade(
        self, trade_id: int, exit_price: float, pnl: float, closed_at: datetime | None = None
    ) -> bool:
        closed_at = closed_at or utcnow()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE trades
                   SET status = ?, exit_price = ?, pnl = ?, closed_at = ?
                 WHERE id = ?
                """,
                (TradeStatus.CLOSED.value, exit_price, pnl, _iso(closed_at), trade_id),
            )
            return cur.rowcount > 0

    def update_status(self, trade_id: int, status: TradeStatus) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE trades SET status = ? WHERE id = ?", (status.value, trade_id)
            )
            return cur.rowcount > 0

    def update_plan_json(self, trade_id: int, plan: dict[str, Any] | str) -> bool:
        """Persist trail / stop updates on an open trade's plan blob."""
        payload = plan if isinstance(plan, str) else json.dumps(plan)
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE trades SET plan_json = ? WHERE id = ?", (payload, trade_id)
            )
            return cur.rowcount > 0

    def get_trade(self, trade_id: int) -> TradeRecord | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return _row_to_trade(row) if row else None

    def query_trades(
        self,
        *,
        window: SessionWindow | None = None,
        regime: Regime | None = None,
        status: TradeStatus | None = None,
        environment: str | None = None,
        limit: int = 500,
    ) -> list[TradeRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if window is not None:
            clauses.append("window = ?")
            params.append(window.value)
        if regime is not None:
            clauses.append("regime = ?")
            params.append(regime.value)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if environment is not None:
            clauses.append("environment LIKE ?")
            params.append(f"{environment}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM trades {where} ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_trade(r) for r in rows]

    def open_position_count(
        self,
        environment: str | None = None,
        window: SessionWindow | None = None,
    ) -> int:
        # Match by prefix so simulated trades (e.g. "PAPER(sim)") count toward
        # the same environment's ("PAPER") position cap.
        clauses = ["status = ?"]
        params: list[Any] = [TradeStatus.OPEN.value]
        if environment:
            clauses.append("environment LIKE ?")
            params.append(f"{environment}%")
        if window is not None:
            clauses.append("window = ?")
            params.append(window.value)
        sql = f"SELECT COUNT(*) AS c FROM trades WHERE {' AND '.join(clauses)}"
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"])

    def entries_since(
        self,
        since: datetime,
        *,
        environment: str | None = None,
        window: SessionWindow | None = None,
    ) -> int:
        """Count journal entries created since ``since`` (any status)."""
        clauses = ["created_at >= ?"]
        params: list[Any] = [_iso(since)]
        if environment:
            clauses.append("environment LIKE ?")
            params.append(f"{environment}%")
        if window is not None:
            clauses.append("window = ?")
            params.append(window.value)
        sql = f"SELECT COUNT(*) AS c FROM trades WHERE {' AND '.join(clauses)}"
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["c"])

    def realised_pnl_since(self, since: datetime, environment: str | None = None) -> float:
        with self._conn() as conn:
            if environment:
                row = conn.execute(
                    """SELECT COALESCE(SUM(pnl), 0) AS s FROM trades
                        WHERE status = ? AND environment LIKE ? AND closed_at >= ?""",
                    (TradeStatus.CLOSED.value, f"{environment}%", _iso(since)),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COALESCE(SUM(pnl), 0) AS s FROM trades
                        WHERE status = ? AND closed_at >= ?""",
                    (TradeStatus.CLOSED.value, _iso(since)),
                ).fetchone()
        return float(row["s"])

    def realised_pnl(self, environment: str | None = None) -> float:
        """Sum of closed-trade P&L (all time)."""
        with self._conn() as conn:
            if environment:
                row = conn.execute(
                    """SELECT COALESCE(SUM(pnl), 0) AS s FROM trades
                        WHERE status = ? AND environment LIKE ?""",
                    (TradeStatus.CLOSED.value, f"{environment}%"),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COALESCE(SUM(pnl), 0) AS s FROM trades
                        WHERE status = ?""",
                    (TradeStatus.CLOSED.value,),
                ).fetchone()
        return float(row["s"])

    # --- backtests ---------------------------------------------------------
    def insert_backtest(
        self,
        *,
        label: str,
        symbol: str,
        window: SessionWindow,
        params: dict[str, Any],
        metrics: dict[str, Any],
        is_out_of_sample: bool = False,
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO backtests
                    (created_at, label, symbol, window, params_json, metrics_json, is_out_of_sample)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    _iso(utcnow()),
                    label,
                    symbol,
                    window.value,
                    json.dumps(params),
                    json.dumps(metrics),
                    1 if is_out_of_sample else 0,
                ),
            )
            return int(cur.lastrowid)

    def list_backtests(self, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM backtests WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM backtests ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "label": r["label"],
                    "symbol": r["symbol"],
                    "window": r["window"],
                    "params": json.loads(r["params_json"]),
                    "metrics": json.loads(r["metrics_json"]),
                    "is_out_of_sample": bool(r["is_out_of_sample"]),
                }
            )
        return out

    def get_backtest(self, backtest_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM backtests WHERE id = ?", (backtest_id,)).fetchone()
        if r is None:
            return None
        return {
            "id": r["id"],
            "created_at": r["created_at"],
            "label": r["label"],
            "symbol": r["symbol"],
            "window": r["window"],
            "params": json.loads(r["params_json"]),
            "metrics": json.loads(r["metrics_json"]),
            "is_out_of_sample": bool(r["is_out_of_sample"]),
        }

    def get_active_strategy(self, symbol: str, window: SessionWindow) -> dict[str, Any] | None:
        """Operator-chosen ORB params for this symbol/window, if any."""
        with self._conn() as conn:
            r = conn.execute(
                """
                SELECT backtest_id, label, params_json, selected_at
                  FROM active_strategy
                 WHERE UPPER(symbol) = UPPER(?)
                   AND window = ?
                """,
                (symbol, window.value),
            ).fetchone()
        if r is None:
            return None
        params = json.loads(r["params_json"] or "{}")
        if not is_tradable_orb_params(params):
            return None
        return {
            "id": r["backtest_id"],
            "created_at": r["selected_at"],
            "label": r["label"],
            "params": params,
            "metrics": {},
        }

    def set_active_strategy(
        self,
        symbol: str,
        window: SessionWindow,
        *,
        params: dict[str, Any],
        backtest_id: int | None = None,
        label: str = "",
    ) -> dict[str, Any]:
        if not is_tradable_orb_params(params):
            raise ValueError("Not a tradable ORB parameter set.")
        selected_at = _iso(utcnow()) or ""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO active_strategy
                    (symbol, window, backtest_id, label, params_json, selected_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(symbol, window) DO UPDATE SET
                    backtest_id = excluded.backtest_id,
                    label = excluded.label,
                    params_json = excluded.params_json,
                    selected_at = excluded.selected_at
                """,
                (
                    symbol.upper(),
                    window.value,
                    backtest_id,
                    label,
                    json.dumps(params),
                    selected_at,
                ),
            )
        found = self.get_active_strategy(symbol, window)
        assert found is not None
        return found

    def clear_active_strategy(self, symbol: str, window: SessionWindow | None = None) -> int:
        """Clear the chosen set (one window, or every window for the symbol)."""
        with self._conn() as conn:
            if window is None:
                cur = conn.execute(
                    "DELETE FROM active_strategy WHERE UPPER(symbol) = UPPER(?)",
                    (symbol,),
                )
            else:
                cur = conn.execute(
                    """
                    DELETE FROM active_strategy
                     WHERE UPPER(symbol) = UPPER(?) AND window = ?
                    """,
                    (symbol, window.value),
                )
            return int(cur.rowcount)

    def active_backtest_ids(self, symbol: str | None = None) -> set[int]:
        with self._conn() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT backtest_id FROM active_strategy WHERE UPPER(symbol) = UPPER(?)",
                    (symbol,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT backtest_id FROM active_strategy").fetchall()
        return {int(r["backtest_id"]) for r in rows if r["backtest_id"] is not None}

    def latest_optimised_params(
        self, symbol: str, window: SessionWindow
    ) -> dict[str, Any] | None:
        """Most recent optimiser (else persisted backtest) ORB params for this pair.

        Walk-forward rows store a grid, not a tradable set, and are skipped.
        Prefer a row labelled as an optimiser run when both exist.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, created_at, label, params_json, metrics_json
                  FROM backtests
                 WHERE UPPER(symbol) = UPPER(?)
                   AND window = ?
                 ORDER BY id DESC
                 LIMIT 50
                """,
                (symbol, window.value),
            ).fetchall()
        fallback: dict[str, Any] | None = None
        for r in rows:
            params = json.loads(r["params_json"] or "{}")
            if not is_tradable_orb_params(params):
                continue
            payload = {
                "id": r["id"],
                "created_at": r["created_at"],
                "label": r["label"],
                "params": params,
                "metrics": json.loads(r["metrics_json"] or "{}"),
            }
            if "optimise" in (r["label"] or "").lower():
                return payload
            if fallback is None:
                fallback = payload
        return fallback


def is_tradable_orb_params(params: Any) -> bool:
    return (
        isinstance(params, dict)
        and "opening_range_minutes" in params
        and "grid" not in params
    )


def _row_to_trade(row: sqlite3.Row) -> TradeRecord:
    return TradeRecord(
        id=row["id"],
        created_at=_parse_dt(row["created_at"]),
        environment=row["environment"],
        symbol=row["symbol"],
        window=SessionWindow(row["window"]),
        regime=Regime(row["regime"]),
        spread_type=SpreadType(row["spread_type"]),
        direction=Direction(row["direction"]),
        contracts=row["contracts"],
        entry_price=row["entry_price"],
        max_loss=row["max_loss"],
        max_profit=row["max_profit"],
        target_r=row["target_r"],
        status=TradeStatus(row["status"]),
        exit_price=row["exit_price"],
        pnl=row["pnl"],
        closed_at=_parse_dt(row["closed_at"]),
        signal_strength=row["signal_strength"],
        order_ref=row["order_ref"],
        notes=row["notes"],
        plan_json=row["plan_json"] if "plan_json" in row.keys() else "{}",
    )
