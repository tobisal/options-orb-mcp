"""Async Interactive Brokers client wrapper built on ``ib_async``.

Only this module knows about IBKR specifics. It is imported lazily so that the
pricing engine, backtester and unit tests run with **no** IBKR dependency and
**no** Gateway/TWS running. Anything that actually touches the wire raises a
clear error if ``ib_async`` is missing or the connection is down.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.config import get_settings
from core.models import Bar, OptionLeg, OptionRight
from core.timeutils import as_naive_utc, utcnow

# ``ib_async`` is optional at import time; resolved on first real use.
try:  # pragma: no cover - exercised only when the dependency is present
    import ib_async as iba

    _IB_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover
    iba = None  # type: ignore[assignment]
    _IB_IMPORT_ERROR = exc

# Market-data type name -> IBKR code (reqMarketDataType).
_MARKET_DATA_TYPES = {"live": 1, "frozen": 2, "delayed": 3, "delayed_frozen": 4}
# IBKR error codes meaning "you are not subscribed to this market data".
_NO_SUBSCRIPTION_CODES = {354, 10089, 10091, 10167, 10168, 10197}

# Circuit breaker: monotonic time of the last failed connect (shared across
# callers) so repeated attempts while Gateway/TWS is down don't hammer the socket.
_last_failure_monotonic: float = 0.0
_logging_configured = False

# One IB socket per process, always on IBKR_CLIENT_ID (default 17).
_shared_ib: Any = None
_shared_client_id: int | None = None
_shared_refs: int = 0
_shared_readonly: bool | None = None
_shared_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _shared_lock
    if _shared_lock is None:
        _shared_lock = asyncio.Lock()
    return _shared_lock


def _client_id_candidates(preferred: int) -> list[int]:
    """Always use ``IBKR_CLIENT_ID`` only (default 17). No pid offset / walk."""
    return [int(preferred)]


def _resolve_ipv4(host: str) -> str:
    """Force IPv4. Docker DNS AAAA records make ib_async connect hang until timeout."""
    raw = (host or "").strip() or "127.0.0.1"
    if raw in {"127.0.0.1", "0.0.0.0"}:
        return raw
    if raw in {"localhost", "::1"}:
        return "127.0.0.1"
    try:
        infos = socket.getaddrinfo(raw, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return raw
    if not infos:
        return raw
    return infos[0][4][0]


def _configure_ib_logging() -> None:
    """Quiet ib_async's own connection logging unless IBKR_VERBOSE is set."""
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    if not get_settings().ibkr_verbose:
        # ib_async logs "API connection failed" / "Make sure API port ..." at
        # ERROR level; we surface status ourselves, so silence the noise.
        logging.getLogger("ib_async").setLevel(logging.CRITICAL)


class IBKRUnavailable(RuntimeError):
    """Raised when IBKR functionality is requested but cannot be provided."""


def _require_ib() -> None:
    if iba is None:
        raise IBKRUnavailable(
            "ib_async is not installed. Run `uv sync` (or `pip install ib-async`). "
            f"Original import error: {_IB_IMPORT_ERROR!r}"
        )


@dataclass
class OptionQuote:
    strike: float
    right: OptionRight
    expiry: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    iv: float | None = None
    delta: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.last


@dataclass
class IBKRClient:
    """Thin async facade over ``ib_async.IB``.

    Use as an async context manager::

        async with IBKRClient() as ib:
            bars = await ib.historical_bars("SPY")
    """

    host: str | None = None
    port: int | None = None
    client_id: int | None = None
    readonly: bool = True  # avoid Gateway "write access" dialog for reads/status
    _ib: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        s = get_settings()
        self.host = self.host or s.ibkr_host
        self.port = self.port or s.ibkr_port
        self.client_id = self.client_id if self.client_id is not None else s.ibkr_client_id

    # --- connection lifecycle ---------------------------------------------
    async def connect(self, timeout: float = 12.0) -> None:
        global _last_failure_monotonic, _shared_ib, _shared_client_id, _shared_refs
        global _shared_readonly
        _require_ib()
        _configure_ib_logging()
        if self._ib is not None and self._ib.isConnected():
            return

        async with _lock():
            if (
                _shared_ib is not None
                and _shared_ib.isConnected()
                and _shared_readonly is self.readonly
            ):
                self._ib = _shared_ib
                self.client_id = _shared_client_id
                _shared_refs += 1
                return
            if _shared_ib is not None:
                try:
                    _shared_ib.disconnect()
                except Exception:
                    pass
                _shared_ib = None
                _shared_client_id = None
                _shared_refs = 0
                _shared_readonly = None

            cooldown = get_settings().ibkr_retry_cooldown
            since = time.monotonic() - _last_failure_monotonic
            if _last_failure_monotonic and since < cooldown:
                raise IBKRUnavailable(
                    f"IBKR connect skipped: in cooldown after a recent failed connection "
                    f"({since:.0f}s of {cooldown:.0f}s). Start TWS/IB Gateway and it will "
                    f"reconnect automatically."
                )

            preferred = int(self.client_id or get_settings().ibkr_client_id)
            last_exc: Exception | None = None
            gateway_down = False
            host = _resolve_ipv4(str(self.host))
            for cid in _client_id_candidates(preferred):
                ib = iba.IB()
                try:
                    await asyncio.wait_for(
                        ib.connectAsync(
                            host,
                            self.port,
                            clientId=cid,
                            readonly=self.readonly,
                        ),
                        timeout=timeout,
                    )
                except ConnectionRefusedError as exc:
                    last_exc = exc
                    gateway_down = True
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    break
                except TimeoutError as exc:
                    last_exc = exc
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    break
                except OSError as exc:
                    last_exc = exc
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    break

                self._ib = ib
                self.client_id = cid
                _shared_ib = ib
                _shared_client_id = cid
                _shared_refs = 1
                _shared_readonly = self.readonly
                _last_failure_monotonic = 0.0
                self._apply_market_data_type()
                return

            _last_failure_monotonic = time.monotonic()
            hint = (
                "Is TWS/IB Gateway running with the API enabled?"
                if gateway_down
                else (
                    "API handshake timed out. Often the Gateway dialog "
                    "'API client needs write access' — ensure READ_ONLY_API=no "
                    "and IBC logged 'Read-Only API checkbox is now set to: false'. "
                    "Retry with readonly connect, or: docker exec orb-ib-gateway pkill -x socat"
                    if isinstance(last_exc, TimeoutError)
                    else "clientId already in use or connection failed; retry in a moment."
                )
            )
            raise IBKRUnavailable(
                f"Could not connect to IBKR at {host}:{self.port} "
                f"(configured host={self.host}; clientId={preferred}). {hint} "
                f"Original error: {last_exc!r}"
            )

    async def disconnect(self) -> None:
        """Release a reference to the shared process connection.

        The socket is kept alive for reuse (dashboard polls / auto-trader /
        MCP tools in the same process). It is only torn down if it is already
        dead, or if this instance was not the shared socket.
        """
        global _shared_ib, _shared_client_id, _shared_refs, _shared_readonly
        async with _lock():
            if self._ib is _shared_ib:
                _shared_refs = max(_shared_refs - 1, 0)
                self._ib = None
                return
            if self._ib is not None and self._ib.isConnected():
                self._ib.disconnect()
            self._ib = None

    def _apply_market_data_type(self) -> None:
        """Select the market-data type, with live->delayed auto-fallback.

        For ``auto`` we request real-time (1) and register an error handler that
        switches to delayed (3) if IBKR reports no subscription - so a bare paper
        account still gets free delayed data and upgrades to live automatically
        once subscriptions exist.
        """
        mode = get_settings().ibkr_market_data_type.strip().lower()
        if mode in _MARKET_DATA_TYPES:
            self._ib.reqMarketDataType(_MARKET_DATA_TYPES[mode])
            return

        # auto (or anything unrecognised): prefer live, fall back to delayed.
        self._ib.reqMarketDataType(1)
        self._delayed_fallback_armed = True

        def _on_error(reqId: int, errorCode: int, errorString: str, contract: Any) -> None:  # noqa: ARG001
            if errorCode in _NO_SUBSCRIPTION_CODES and getattr(self, "_delayed_fallback_armed", False):
                self._delayed_fallback_armed = False  # only switch once
                try:
                    self._ib.reqMarketDataType(3)
                except Exception:  # pragma: no cover - best effort
                    pass

        try:
            self._ib.errorEvent += _on_error
        except Exception:  # pragma: no cover - event API differences
            pass

    async def __aenter__(self) -> IBKRClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()

    @property
    def ib(self) -> Any:
        if self._ib is None or not self._ib.isConnected():
            raise IBKRUnavailable("Not connected. Call connect() first.")
        return self._ib

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # --- contracts ---------------------------------------------------------
    def _stock(self, symbol: str) -> Any:
        return iba.Stock(symbol, "SMART", "USD")

    async def _qualify(self, contract: Any) -> Any:
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise IBKRUnavailable(f"Could not qualify contract: {contract}")
        return qualified[0]

    # --- market data -------------------------------------------------------
    async def historical_bars(
        self,
        symbol: str,
        *,
        duration: str = "2 D",
        bar_size: str = "5 mins",
        use_rth: bool = False,
        what_to_show: str = "TRADES",
        end_datetime: str = "",
    ) -> list[Bar]:
        """Fetch OHLCV bars for the underlying.

        ``end_datetime`` is IBKR format ``yyyyMMdd HH:mm:ss`` (UTC); empty means now.
        A trailing `` UTC`` is added when a timestamp is given without a zone.
        """
        contract = await self._qualify(self._stock(symbol))
        end = end_datetime.strip()
        if end and " " in end and not end.upper().endswith("UTC") and " GMT" not in end.upper():
            end = f"{end} UTC"
        raw = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=2,
        )
        bars: list[Bar] = []
        for b in raw:
            ts = b.date if isinstance(b.date, datetime) else datetime.fromisoformat(str(b.date))
            bars.append(
                Bar(
                    ts=as_naive_utc(ts),
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume or 0),
                )
            )
        return bars

    async def quote(self, symbol: str) -> dict[str, Any]:
        """Snapshot bid/ask/last for the underlying."""
        contract = await self._qualify(self._stock(symbol))
        tickers = await self.ib.reqTickersAsync(contract)
        t = tickers[0]
        bid, ask = _clean(t.bid), _clean(t.ask)
        mid = round((bid + ask) / 2, 4) if bid and ask else None
        last = _clean(t.last) or _clean(t.close) or mid
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": last,
            "close": _clean(t.close),
            "time": utcnow().isoformat(),
        }

    async def option_chain(
        self,
        symbol: str,
        *,
        max_expiries: int = 4,
        strikes_around: int = 12,
        spot_hint: float | None = None,
    ) -> dict[str, Any]:
        """Return available expiries and strikes near the money for a symbol.

        IBKR returns several option classes for the same underlying (e.g. SPY
        vs 2SPY). We pick the standard 100-multiplier class whose strikes sit
        near ``spot``, not the first SMART row.
        """
        stock = await self._qualify(self._stock(symbol))
        params = await self.ib.reqSecDefOptParamsAsync(
            stock.symbol, "", stock.secType, stock.conId
        )
        quoted = await self.quote(symbol)
        spot = quoted.get("last") or (spot_hint or 0.0)
        chain = select_option_chain(params, symbol, spot)
        if chain is None:
            raise IBKRUnavailable(f"No option chain returned for {symbol}")

        expiries = sorted(chain.expirations)[:max_expiries]
        strikes = sorted(float(s) for s in chain.strikes)
        near = _nearest_strikes(strikes, spot, strikes_around)
        if not chain_is_usable(spot, near):
            near = atm_strike_grid(spot)
        return {
            "symbol": symbol,
            "spot": spot,
            "trading_class": chain.tradingClass,
            "multiplier": chain.multiplier,
            "expiries": expiries,
            "strikes": near,
            "all_strikes_count": len(strikes),
        }

    async def option_quotes(
        self, symbol: str, expiry: str, strikes: list[float], right: OptionRight
    ) -> list[OptionQuote]:
        """Fetch quotes + model greeks/IV for a set of option strikes."""
        contracts = [
            iba.Option(symbol, expiry, strike, right.value, "SMART", multiplier="100")
            for strike in strikes
        ]
        contracts = await self.ib.qualifyContractsAsync(*contracts)
        # genericTickList "106" requests option implied volatility / model greeks.
        tickers = await self.ib.reqTickersAsync(*contracts)
        quotes: list[OptionQuote] = []
        for c, t in zip(contracts, tickers):
            greeks = getattr(t, "modelGreeks", None)
            quotes.append(
                OptionQuote(
                    strike=float(c.strike),
                    right=right,
                    expiry=expiry,
                    bid=_clean(t.bid),
                    ask=_clean(t.ask),
                    last=_clean(t.last) or _clean(t.close),
                    iv=_clean(getattr(greeks, "impliedVol", None)) if greeks else None,
                    delta=_clean(getattr(greeks, "delta", None)) if greeks else None,
                )
            )
        return quotes

    async def atm_iv(self, symbol: str, expiry: str) -> float | None:
        """Approximate at-the-money implied volatility for a symbol/expiry."""
        chain = await self.option_chain(symbol, max_expiries=8)
        spot = chain["spot"]
        strikes = chain["strikes"]
        if not strikes or not spot:
            return None
        atm = min(strikes, key=lambda s: abs(s - spot))
        quotes = await self.option_quotes(symbol, expiry, [atm], OptionRight.CALL)
        return quotes[0].iv if quotes else None

    # --- order placement (combos + bracket) --------------------------------
    def _build_combo(self, symbol: str, long_leg: OptionLeg, short_leg: OptionLeg) -> Any:
        """Construct a BAG (combo) contract for a two-legged vertical spread."""
        # Leg contracts must be qualified to obtain conIds before combo assembly;
        # the caller (execution server) qualifies via place_spread_order.
        bag = iba.Contract()
        bag.symbol = symbol
        bag.secType = "BAG"
        bag.currency = "USD"
        bag.exchange = "SMART"
        return bag

    async def _qualified_vertical_bag(
        self, symbol: str, long_leg: OptionLeg, short_leg: OptionLeg
    ) -> Any:
        long_c, short_c = await self.ib.qualifyContractsAsync(
            iba.Option(
                symbol, long_leg.expiry, long_leg.strike, long_leg.right.value, "SMART",
                multiplier="100",
            ),
            iba.Option(
                symbol, short_leg.expiry, short_leg.strike, short_leg.right.value, "SMART",
                multiplier="100",
            ),
        )
        bag = self._build_combo(symbol, long_leg, short_leg)
        bag.comboLegs = [
            iba.ComboLeg(conId=long_c.conId, ratio=1, action="BUY", exchange="SMART"),
            iba.ComboLeg(conId=short_c.conId, ratio=1, action="SELL", exchange="SMART"),
        ]
        return bag, long_c, short_c

    def _cancel_working_for_ref(self, order_ref: str) -> int:
        """Cancel working entry/TP/SL orders tied to this placement."""
        cancelled = 0
        oca = f"{order_ref}-EXIT"
        try:
            self.ib.reqAllOpenOrders()
        except Exception:
            pass
        for trade in list(self.ib.openTrades()):
            order = trade.order
            ref = str(getattr(order, "orderRef", "") or "")
            group = str(getattr(order, "ocaGroup", "") or "")
            if ref == f"{order_ref}-FLAT":
                continue
            if ref == order_ref or ref.startswith(f"{order_ref}-") or group == oca:
                try:
                    self.ib.cancelOrder(order)
                    cancelled += 1
                except Exception:
                    pass
        return cancelled

    async def close_spread_order(
        self,
        symbol: str,
        long_leg: OptionLeg,
        short_leg: OptionLeg,
        contracts: int,
        order_ref: str,
    ) -> dict[str, Any]:
        """Flatten a long vertical at the broker: cancel TP/SL, market-sell the bag.

        Used at session-window close so live (and paper IBKR) positions do not
        ride into the next window on GTC exits alone.
        """
        qty = max(int(contracts), 0)
        if qty <= 0:
            return {"ok": False, "error": "No contracts to close."}
        bag, long_c, short_c = await self._qualified_vertical_bag(symbol, long_leg, short_leg)
        pos_by_id = {
            int(p.contract.conId): float(p.position)
            for p in self.ib.positions()
            if getattr(p.contract, "conId", 0)
        }
        long_qty = pos_by_id.get(int(long_c.conId), 0.0)
        short_qty = pos_by_id.get(int(short_c.conId), 0.0)
        already_flat = abs(long_qty) < 0.01 and abs(short_qty) < 0.01
        cancelled = self._cancel_working_for_ref(order_ref)
        await asyncio.sleep(0.25)
        if already_flat:
            return {
                "ok": True,
                "already_flat": True,
                "cancelled": cancelled,
                "status": "Inactive",
            }
        flatten_qty = max(qty, int(round(abs(long_qty))) or qty)
        close = iba.MarketOrder("SELL", flatten_qty, tif="DAY")
        close.orderRef = f"{order_ref}-FLAT"
        try:
            trade = self.ib.placeOrder(bag, close)
        except Exception as exc:
            return {"ok": False, "error": f"Flatten order rejected: {exc}", "cancelled": cancelled}
        await asyncio.sleep(0.35)
        status = getattr(getattr(trade, "orderStatus", None), "status", "Submitted")
        err = str(getattr(trade, "log", "") or "")
        if status in {"Cancelled", "Inactive", "ApiCancelled"}:
            return {
                "ok": False,
                "error": f"Flatten not accepted ({status}). Option market may be closed.",
                "cancelled": cancelled,
                "status": status,
            }
        return {
            "ok": True,
            "already_flat": False,
            "cancelled": cancelled,
            "status": status,
            "order_ref": close.orderRef,
            "log": err,
        }

    async def place_spread_order(
        self,
        symbol: str,
        long_leg: OptionLeg,
        short_leg: OptionLeg,
        contracts: int,
        limit_price: float,
        *,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
    ) -> dict[str, Any]:
        """Place a defined-risk vertical as a combo limit order.

        A debit spread is BUY of the combo (net debit paid). Take-profit and
        stop-loss are attached as an OCA group of two closing (SELL) orders so
        exactly one fires. Works against a paper account; real fills differ.
        """
        bag, _long_c, _short_c = await self._qualified_vertical_bag(symbol, long_leg, short_leg)

        entry = iba.LimitOrder("BUY", contracts, round(limit_price, 2), tif="DAY")
        entry.orderRef = f"ORB-{symbol}-{utcnow():%Y%m%d%H%M%S}"
        entry_trade = self.ib.placeOrder(bag, entry)

        oca_group = f"{entry.orderRef}-EXIT"
        exit_trades = []
        if take_profit_price is not None:
            tp = iba.LimitOrder("SELL", contracts, round(take_profit_price, 2), tif="GTC")
            tp.ocaGroup = oca_group
            tp.ocaType = 1
            exit_trades.append(self.ib.placeOrder(bag, tp))
        if stop_loss_price is not None:
            sl = iba.StopOrder("SELL", contracts, round(stop_loss_price, 2), tif="GTC")
            sl.ocaGroup = oca_group
            sl.ocaType = 1
            exit_trades.append(self.ib.placeOrder(bag, sl))

        await asyncio.sleep(0.2)  # let the order status propagate
        return {
            "order_ref": entry.orderRef,
            "entry_status": entry_trade.orderStatus.status,
            "exit_orders": len(exit_trades),
            "oca_group": oca_group,
        }

    async def account_summary(self) -> dict[str, Any]:
        rows = await self.ib.accountSummaryAsync()
        out: dict[str, Any] = {}
        for r in rows:
            if r.tag in {"NetLiquidation", "AvailableFunds", "BuyingPower", "TotalCashValue"}:
                out[r.tag] = float(r.value)
        return out

    async def positions(self) -> list[dict[str, Any]]:
        poss = self.ib.positions()
        return [
            {
                "symbol": p.contract.symbol,
                "secType": p.contract.secType,
                "right": getattr(p.contract, "right", ""),
                "strike": getattr(p.contract, "strike", 0.0),
                "expiry": getattr(p.contract, "lastTradeDateOrContractMonth", ""),
                "position": p.position,
                "avgCost": p.avgCost,
            }
            for p in poss
        ]


def _clean(x: Any) -> float | None:
    """IBKR uses NaN / -1 to mean 'no data'; normalise to None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v < 0:  # NaN or sentinel
        return None
    return v


def _nearest_strikes(strikes: list[float], spot: float, count: int) -> list[float]:
    if not strikes or spot <= 0:
        return []
    ordered = sorted(strikes, key=lambda s: abs(s - spot))
    return sorted(ordered[: max(count, 1)])


def chain_is_usable(spot: float, strikes: list[float], *, max_pct: float = 0.05) -> bool:
    """True when the chain has at least two distinct strikes near ``spot``."""
    uniq = sorted({float(s) for s in strikes})
    if spot <= 0 or len(uniq) < 2:
        return False
    nearest = min(uniq, key=lambda s: abs(s - spot))
    return abs(nearest - spot) / spot <= max_pct


def atm_strike_grid(spot: float, *, increment: float = 1.0, count: int = 8) -> list[float]:
    """Conventional $1 (or ``increment``) strike ladder centred on ``spot``."""
    if spot <= 0:
        return []
    atm = round(spot / increment) * increment
    return [
        atm + increment * k
        for k in range(-count, count + 1)
        if atm + increment * k > 0
    ]


def select_option_chain(params: list[Any], symbol: str, spot: float) -> Any | None:
    """Pick the standard option class for ``symbol``, not a mini / odd listing.

    ``reqSecDefOptParams`` returns one row per trading class. Taking the first
    SMART row can yield ``2SPY`` with a handful of far-OTM strikes, which then
    makes vertical construction collapse to a single strike.
    """
    if not params:
        return None
    want = symbol.upper()
    ranked: list[tuple[float, int, Any]] = []
    for chain in params:
        strikes = [float(s) for s in (getattr(chain, "strikes", None) or [])]
        expiries = list(getattr(chain, "expirations", None) or [])
        if not strikes or not expiries:
            continue
        trading = str(getattr(chain, "tradingClass", "") or "").upper()
        multiplier = str(getattr(chain, "multiplier", "") or "")
        exchange = str(getattr(chain, "exchange", "") or "").upper()
        score = 0.0
        if trading == want:
            score += 1000
        elif want in trading:
            score += 50
        if multiplier in {"100", "100.0"}:
            score += 80
        if exchange == "SMART":
            score += 40
        score += min(len(strikes), 400) / 10.0
        if spot > 0:
            near = sum(1 for s in strikes if abs(s - spot) / spot <= 0.08)
            score += min(near, 50)
            nearest = min(strikes, key=lambda s: abs(s - spot))
            if abs(nearest - spot) / spot > 0.15:
                score -= 600
        ranked.append((score, len(strikes), chain))
    if not ranked:
        return params[0]
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return ranked[0][2]
