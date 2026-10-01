"""Fan-out one MES signal across multiple prop accounts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from core.brokers.base import BrokerUnavailable, FuturesBroker
from core.timeutils import utcnow

_log = logging.getLogger("orb.prop.copy")


@dataclass(frozen=True)
class PropAccount:
    id: str
    name: str = ""
    enabled: bool = True
    size_scale: float = 1.0


class MultiAccountBroker:
    """Place/modify/close on a lead + followers via an underlying FuturesBroker.

    Lead failure aborts the whole entry. Follower failures are retried once and
    recorded; callers must surface divergent exposure via ``prop_copies``.
    """

    def __init__(
        self,
        underlying: FuturesBroker,
        accounts: list[PropAccount],
        *,
        signal_id: str | None = None,
    ) -> None:
        self._u = underlying
        enabled = [a for a in accounts if a.enabled and a.id]
        if not enabled:
            raise BrokerUnavailable("PROP_ACCOUNTS is empty — no accounts to trade.")
        self.accounts = enabled
        self.signal_id = signal_id or f"SIG-{utcnow():%Y%m%d%H%M%S}"

    @property
    def lead(self) -> PropAccount:
        return self.accounts[0]

    async def connect(self) -> None:
        await self._u.connect()

    async def disconnect(self) -> None:
        await self._u.disconnect()

    async def __aenter__(self) -> "MultiAccountBroker":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    async def list_accounts(self) -> list[dict[str, Any]]:
        return [
            {
                "id": a.id,
                "name": a.name or a.id,
                "enabled": a.enabled,
                "size_scale": a.size_scale,
            }
            for a in self.accounts
        ]

    def _qty(self, contracts: int, scale: float) -> int:
        return max(int(round(float(contracts) * float(scale))), 1)

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
        """Fan-out entry. ``account_id`` ignored — uses configured account list."""
        _ = account_id
        copies: list[dict[str, Any]] = []
        lead_res: dict[str, Any] | None = None

        for i, acct in enumerate(self.accounts):
            qty = self._qty(contracts, acct.size_scale)
            # Lead: single attempt then abort. Followers: retry once on failure.
            max_attempts = 1 if i == 0 else 2
            attempt = 0
            last_err = None
            res: dict[str, Any] = {"ok": False}
            while attempt < max_attempts:
                attempt += 1
                try:
                    res = await self._u.place_future_bracket(
                        symbol,
                        side=side,
                        contracts=qty,
                        stop_price=stop_price,
                        entry_limit=entry_limit,
                        take_profit_price=take_profit_price,
                        fill_timeout=fill_timeout,
                        account_id=acct.id,
                    )
                except Exception as exc:  # noqa: BLE001 — surface per-account
                    last_err = str(exc)
                    res = {"ok": False, "error": last_err}
                if res.get("ok"):
                    break
                last_err = str(res.get("error") or "place failed")
                if attempt < max_attempts:
                    await asyncio.sleep(0.35)

            row = {
                "account_id": acct.id,
                "account_name": acct.name or acct.id,
                "order_ref": res.get("order_ref"),
                "contracts": qty,
                "ok": bool(res.get("ok")),
                "status": res.get("status"),
                "avg_fill_price": res.get("avg_fill_price"),
                "error": None if res.get("ok") else last_err or res.get("error"),
                "role": "lead" if i == 0 else "follower",
            }
            copies.append(row)

            if i == 0:
                if not res.get("ok"):
                    return {
                        "ok": False,
                        "error": f"Lead account {acct.id} place failed: {row['error']}",
                        "prop_copies": copies,
                        "signal_id": self.signal_id,
                    }
                lead_res = res

        assert lead_res is not None
        failed = [c for c in copies if not c["ok"]]
        if failed:
            _log.error(
                "Prop copy divergent after lead fill: %s",
                ", ".join(f"{c['account_id']}:{c['error']}" for c in failed),
            )

        return {
            "ok": True,
            "order_ref": lead_res.get("order_ref"),
            "avg_fill_price": lead_res.get("avg_fill_price"),
            "filled_qty": lead_res.get("filled_qty"),
            "status": lead_res.get("status"),
            "prop_copies": copies,
            "signal_id": self.signal_id,
            "backend": "tradovate_prop",
            "copy_failures": len(failed),
        }

    async def modify_future_stop(
        self,
        symbol: str,
        order_ref: str,
        *,
        stop_price: float,
        contracts: int,
        side: str,
        account_id: str | None = None,
        prop_copies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        results = []
        targets = prop_copies or [
            {"account_id": a.id, "order_ref": order_ref, "contracts": contracts, "ok": True}
            for a in self.accounts
        ]
        for row in targets:
            if not row.get("ok") and not row.get("order_ref"):
                continue
            acct = str(row.get("account_id") or account_id or "")
            ref = str(row.get("order_ref") or order_ref)
            qty = int(row.get("contracts") or contracts)
            try:
                res = await self._u.modify_future_stop(
                    symbol,
                    ref,
                    stop_price=stop_price,
                    contracts=qty,
                    side=side,
                    account_id=acct,
                )
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)}
            results.append({"account_id": acct, "order_ref": ref, **res})
        ok_any = any(r.get("ok") for r in results)
        return {"ok": ok_any, "results": results, "backend": "tradovate_prop"}

    async def close_future_position(
        self,
        symbol: str,
        *,
        contracts: int,
        side: str,
        order_ref: str,
        account_id: str | None = None,
        prop_copies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        results = []
        targets = prop_copies or [
            {"account_id": a.id, "order_ref": order_ref, "contracts": contracts, "ok": True}
            for a in self.accounts
        ]
        for row in targets:
            acct = str(row.get("account_id") or account_id or "")
            ref = str(row.get("order_ref") or order_ref)
            qty = int(row.get("contracts") or contracts)
            if not acct or not ref:
                continue
            try:
                res = await self._u.close_future_position(
                    symbol,
                    contracts=qty,
                    side=side,
                    order_ref=ref,
                    account_id=acct,
                )
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)}
            results.append({"account_id": acct, "order_ref": ref, **res})
        ok_any = any(r.get("ok") for r in results)
        return {"ok": ok_any, "results": results, "backend": "tradovate_prop"}
