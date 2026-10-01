"""Broker protocols for futures execution (IBKR, Tradovate prop, multi-account)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FuturesBroker(Protocol):
    """Minimal async futures execution surface used by engine settle/place."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def list_accounts(self) -> list[dict[str, Any]]: ...

    async def place_future_bracket(
        self,
        symbol: str,
        *,
        side: str,
        contracts: int,
        stop_price: float,
        entry_limit: float | None = None,
        take_profit_price: float | None = None,
        fill_timeout: float | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def modify_future_stop(
        self,
        symbol: str,
        order_ref: str,
        *,
        stop_price: float,
        contracts: int,
        side: str,
        account_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def close_future_position(
        self,
        symbol: str,
        *,
        contracts: int,
        side: str,
        order_ref: str,
        account_id: str | None = None,
    ) -> dict[str, Any]: ...


class BrokerUnavailable(RuntimeError):
    """Raised when a broker cannot be reached or authenticated."""
