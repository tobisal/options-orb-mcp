"""Tradovate REST client for MES brackets (prop / cloud execution).

Orders always set ``isAutomated=True`` (CME algo flag). Demo vs live host is
selected via ``TRADOVATE_ENV=demo|live``.

This is intentionally a thin HTTP client: auth, contract lookup, place,
cancel/liquidate. Multi-account fan-out lives in ``multi_account.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from core.brokers.base import BrokerUnavailable
from core.timeutils import utcnow

_log = logging.getLogger("orb.tradovate")

DEMO_HOST = "https://demo.tradovateapi.com"
LIVE_HOST = "https://live.tradovateapi.com"
MD_DEMO = "https://md-demo.tradovateapi.com"
MD_LIVE = "https://md.tradovateapi.com"


class TradovateClient:
    """Async Tradovate futures broker implementing FuturesBroker methods."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        app_id: str = "options-orb-mcp",
        app_version: str = "1.0",
        device_id: str = "orb-aws",
        cid: str = "",
        sec: str = "",
        env: str = "demo",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.app_id = app_id
        self.app_version = app_version
        self.device_id = device_id
        self.cid = cid
        self.sec = sec
        self.env = (env or "demo").lower().strip()
        self._host = DEMO_HOST if self.env != "live" else LIVE_HOST
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._contract_cache: dict[str, int] = {}
        self._owned_http = http is None
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._connected = False

    async def connect(self) -> None:
        if not self.username or not self.password:
            raise BrokerUnavailable(
                "Tradovate credentials missing (TRADOVATE_USER / TRADOVATE_PASSWORD)."
            )
        await self._ensure_token()
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._token = None
        if self._owned_http:
            await self._http.aclose()

    async def __aenter__(self) -> "TradovateClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.disconnect()

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        body: dict[str, Any] = {
            "name": self.username,
            "password": self.password,
            "appId": self.app_id,
            "appVersion": self.app_version,
            "deviceId": self.device_id,
        }
        if self.cid and self.sec:
            body["cid"] = self.cid
            body["sec"] = self.sec
        url = f"{self._host}/v1/auth/accesstokenrequest"
        try:
            resp = await self._http.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise BrokerUnavailable(f"Tradovate auth failed: {exc}") from exc
        token = data.get("accessToken") or data.get("token")
        if not token:
            raise BrokerUnavailable(f"Tradovate auth missing token: {data}")
        # Tradovate tokens typically last ~90 minutes; refresh early.
        self._token = str(token)
        self._token_exp = time.time() + float(data.get("expirationTime", 80 * 60) or 4800)
        return self._token

    async def _headers(self) -> dict[str, str]:
        tok = await self._ensure_token()
        return {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._host}/v1/{path.lstrip('/')}"
        headers = await self._headers()
        resp = await self._http.post(url, json=payload, headers=headers)
        if resp.status_code == 401:
            self._token = None
            headers = await self._headers()
            resp = await self._http.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise BrokerUnavailable(
                f"Tradovate {path} HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        return data if isinstance(data, dict) else {"result": data}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._host}/v1/{path.lstrip('/')}"
        headers = await self._headers()
        resp = await self._http.get(url, params=params or {}, headers=headers)
        if resp.status_code == 401:
            self._token = None
            headers = await self._headers()
            resp = await self._http.get(url, params=params or {}, headers=headers)
        if resp.status_code >= 400:
            raise BrokerUnavailable(
                f"Tradovate {path} HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    async def list_accounts(self) -> list[dict[str, Any]]:
        data = await self._get("account/list")
        rows = data if isinstance(data, list) else data.get("accounts") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "id": str(row.get("id") or row.get("accountId") or ""),
                    "name": str(row.get("name") or row.get("accountName") or ""),
                    "enabled": True,
                }
            )
        return [a for a in out if a["id"]]

    async def resolve_contract_id(self, symbol: str = "MES") -> int:
        key = symbol.upper()
        if key in self._contract_cache:
            return self._contract_cache[key]
        # Prefer continuous/front month via suggest.
        data = await self._get("contract/suggest", {"t": key, "l": 5})
        rows = data if isinstance(data, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if key in name.upper() or str(row.get("contractMaturityId") or ""):
                cid = int(row.get("id") or 0)
                if cid:
                    self._contract_cache[key] = cid
                    return cid
        if rows and isinstance(rows[0], dict) and rows[0].get("id"):
            cid = int(rows[0]["id"])
            self._contract_cache[key] = cid
            return cid
        raise BrokerUnavailable(f"No Tradovate contract found for {symbol}")

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
        if not account_id:
            return {"ok": False, "error": "account_id required for Tradovate place"}
        qty = max(int(contracts), 0)
        if qty <= 0:
            return {"ok": False, "error": "No contracts to place."}
        action = side.upper()
        if action not in {"BUY", "SELL"}:
            return {"ok": False, "error": f"Invalid side {side}"}
        try:
            contract_id = await self.resolve_contract_id(symbol)
        except BrokerUnavailable as exc:
            return {"ok": False, "error": str(exc)}

        order_ref = f"TV-{symbol}-{account_id}-{utcnow():%Y%m%d%H%M%S}"
        entry_payload: dict[str, Any] = {
            "accountSpec": None,
            "accountId": int(account_id) if str(account_id).isdigit() else account_id,
            "action": action,
            "symbol": symbol.upper(),
            "orderQty": qty,
            "orderType": "Limit" if entry_limit is not None else "Market",
            "isAutomated": True,
            "timeInForce": "Day",
            "text": order_ref,
            "contractId": contract_id,
        }
        if entry_limit is not None:
            entry_payload["price"] = round(float(entry_limit), 2)

        try:
            entry_res = await self._post("order/placeorder", entry_payload)
        except BrokerUnavailable as exc:
            return {"ok": False, "error": str(exc), "order_ref": order_ref}

        # Best-effort fill wait: Tradovate fill confirmation varies; treat
        # accepted order as filled at entry_limit/market for journaling.
        timeout = float(fill_timeout if fill_timeout is not None else 15.0)
        await asyncio.sleep(min(max(timeout * 0.05, 0.2), 1.0))

        exit_action = "Sell" if action == "BUY" else "Buy"
        stop_payload: dict[str, Any] = {
            "accountId": entry_payload["accountId"],
            "action": exit_action,
            "symbol": symbol.upper(),
            "orderQty": qty,
            "orderType": "Stop",
            "stopPrice": round(float(stop_price), 2),
            "isAutomated": True,
            "timeInForce": "GTC",
            "text": f"{order_ref}-STOP",
            "contractId": contract_id,
        }
        try:
            await self._post("order/placeorder", stop_payload)
        except BrokerUnavailable as exc:
            _log.warning("Tradovate stop place failed for %s: %s", order_ref, exc)

        if take_profit_price is not None:
            tp_payload = {
                "accountId": entry_payload["accountId"],
                "action": exit_action,
                "symbol": symbol.upper(),
                "orderQty": qty,
                "orderType": "Limit",
                "price": round(float(take_profit_price), 2),
                "isAutomated": True,
                "timeInForce": "GTC",
                "text": f"{order_ref}-TP",
                "contractId": contract_id,
            }
            try:
                await self._post("order/placeorder", tp_payload)
            except BrokerUnavailable as exc:
                _log.warning("Tradovate TP place failed for %s: %s", order_ref, exc)

        avg = float(entry_limit) if entry_limit is not None else None
        # Prefer fill price from response when present.
        for key in ("avgFillPrice", "price", "fillPrice"):
            if entry_res.get(key) is not None:
                try:
                    avg = float(entry_res[key])
                    break
                except (TypeError, ValueError):
                    pass

        return {
            "ok": True,
            "order_ref": order_ref,
            "account_id": str(account_id),
            "avg_fill_price": avg,
            "filled_qty": float(qty),
            "status": str(entry_res.get("orderStatus") or entry_res.get("status") or "Submitted"),
            "raw": entry_res,
            "backend": "tradovate",
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
    ) -> dict[str, Any]:
        """Cancel prior stop text and place a new stop (best-effort)."""
        if not account_id:
            return {"ok": False, "error": "account_id required"}
        # Cancel working orders tagged with this stop text when possible.
        try:
            await self._cancel_by_text(f"{order_ref}-STOP", account_id=account_id)
        except BrokerUnavailable:
            pass
        action = "Sell" if side.upper() in {"BUY", "LONG"} else "Buy"
        try:
            contract_id = await self.resolve_contract_id(symbol)
            payload = {
                "accountId": int(account_id) if str(account_id).isdigit() else account_id,
                "action": action,
                "symbol": symbol.upper(),
                "orderQty": max(int(contracts), 1),
                "orderType": "Stop",
                "stopPrice": round(float(stop_price), 2),
                "isAutomated": True,
                "timeInForce": "GTC",
                "text": f"{order_ref}-STOP",
                "contractId": contract_id,
            }
            res = await self._post("order/placeorder", payload)
            return {
                "ok": True,
                "order_ref": f"{order_ref}-STOP",
                "stop_price": float(stop_price),
                "status": res.get("orderStatus") or "Submitted",
                "backend": "tradovate",
            }
        except BrokerUnavailable as exc:
            return {"ok": False, "error": str(exc)}

    async def close_future_position(
        self,
        symbol: str,
        *,
        contracts: int,
        side: str,
        order_ref: str,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        if not account_id:
            return {"ok": False, "error": "account_id required"}
        try:
            await self._cancel_by_text(order_ref, account_id=account_id)
            await self._cancel_by_text(f"{order_ref}-STOP", account_id=account_id)
            await self._cancel_by_text(f"{order_ref}-TP", account_id=account_id)
        except BrokerUnavailable:
            pass
        action = "Sell" if side.upper() in {"BUY", "LONG"} else "Buy"
        # If side is the exit side already (engine passes flatten side), use it.
        if side.upper() in {"BUY", "SELL"}:
            # Engine close_future_position passes the closing side.
            action = "Buy" if side.upper() == "BUY" else "Sell"
        try:
            contract_id = await self.resolve_contract_id(symbol)
            payload = {
                "accountId": int(account_id) if str(account_id).isdigit() else account_id,
                "action": action,
                "symbol": symbol.upper(),
                "orderQty": max(int(contracts), 1),
                "orderType": "Market",
                "isAutomated": True,
                "timeInForce": "Day",
                "text": f"{order_ref}-FLAT",
                "contractId": contract_id,
            }
            res = await self._post("order/placeorder", payload)
            return {
                "ok": True,
                "order_ref": f"{order_ref}-FLAT",
                "status": res.get("orderStatus") or "Submitted",
                "backend": "tradovate",
            }
        except BrokerUnavailable as exc:
            # Fallback liquidateposition endpoint
            try:
                res = await self._post(
                    "order/liquidateposition",
                    {
                        "accountId": int(account_id)
                        if str(account_id).isdigit()
                        else account_id,
                        "contractId": await self.resolve_contract_id(symbol),
                        "admin": False,
                    },
                )
                return {"ok": True, "status": "Liquidated", "raw": res, "backend": "tradovate"}
            except BrokerUnavailable as exc2:
                return {"ok": False, "error": f"{exc}; liquidate: {exc2}"}

    async def _cancel_by_text(self, text: str, *, account_id: str) -> None:
        """Best-effort cancel of working orders whose text matches."""
        try:
            orders = await self._get("order/list")
        except BrokerUnavailable:
            return
        rows = orders if isinstance(orders, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("text") or "") != text:
                continue
            oid = row.get("id")
            if oid is None:
                continue
            try:
                await self._post("order/cancelorder", {"orderId": oid})
            except BrokerUnavailable:
                continue
