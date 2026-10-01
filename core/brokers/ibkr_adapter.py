"""IBKR adapter wrapping the existing IBKRClient to the FuturesBroker protocol."""

from __future__ import annotations

from typing import Any

from core.ibkr_client import IBKRClient, IBKRUnavailable
from core.brokers.base import BrokerUnavailable


class IbkrBroker:
    """Thin adapter so engine code can depend on FuturesBroker uniformly."""

    def __init__(self, *, readonly: bool = False) -> None:
        self._client = IBKRClient(readonly=readonly)
        self._readonly = readonly

    async def connect(self) -> None:
        try:
            await self._client.connect()
        except IBKRUnavailable as exc:
            raise BrokerUnavailable(str(exc)) from exc

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def __aenter__(self) -> "IbkrBroker":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    async def list_accounts(self) -> list[dict[str, Any]]:
        # IBKR path is single-session; account selection is Gateway-side.
        return [{"id": "ibkr", "name": "IBKR", "enabled": True}]

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
    ) -> dict[str, Any]:
        _ = account_id
        return await self._client.place_future_bracket(
            symbol,
            side=side,
            contracts=contracts,
            stop_price=stop_price,
            entry_limit=entry_limit,
            take_profit_price=take_profit_price,
            fill_timeout=fill_timeout,
        )

    async def modify_future_stop(
        self,
        symbol: str,
        order_ref: str,
        *,
        stop_price: float,
        contracts: int,
        side: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        _ = account_id
        return await self._client.modify_future_stop(
            symbol,
            order_ref,
            stop_price=stop_price,
            contracts=contracts,
            side=side,
        )

    async def close_future_position(
        self,
        symbol: str,
        *,
        contracts: int,
        side: str,
        order_ref: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        _ = account_id
        return await self._client.close_future_position(
            symbol,
            contracts=contracts,
            side=side,
            order_ref=order_ref,
        )
