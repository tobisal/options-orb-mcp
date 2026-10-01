"""Tests for Tradovate prop multi-account fan-out."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.brokers.base import BrokerUnavailable
from core.brokers.factory import _looks_like_aws, get_broker, resolve_execution_backend
from core.brokers.multi_account import MultiAccountBroker, PropAccount
from core.brokers.tradovate_client import TradovateClient
from core.journal import is_prop_backed
from core.models import (
    Direction,
    Regime,
    SessionWindow,
    SpreadType,
    TradeRecord,
    TradeStatus,
)


class _FakeUnderlying:
    def __init__(self, *, fail_accounts: set[str] | None = None, fail_once: set[str] | None = None):
        self.fail_accounts = fail_accounts or set()
        self.fail_once = fail_once or set()
        self._failed_once: set[str] = set()
        self.places: list[dict[str, Any]] = []
        self.modifies: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def list_accounts(self) -> list[dict[str, Any]]:
        return []

    async def place_future_bracket(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
        acct = str(kwargs.get("account_id") or "")
        self.places.append({"symbol": symbol, **kwargs})
        if acct in self.fail_accounts:
            return {"ok": False, "error": f"hard fail {acct}"}
        if acct in self.fail_once and acct not in self._failed_once:
            self._failed_once.add(acct)
            return {"ok": False, "error": f"transient {acct}"}
        return {
            "ok": True,
            "order_ref": f"TV-MES-{acct}-TEST",
            "avg_fill_price": 5000.0,
            "filled_qty": float(kwargs.get("contracts") or 1),
            "status": "Filled",
            "account_id": acct,
        }

    async def modify_future_stop(self, symbol: str, order_ref: str, **kwargs: Any) -> dict[str, Any]:
        self.modifies.append({"symbol": symbol, "order_ref": order_ref, **kwargs})
        return {"ok": True, "stop_price": kwargs.get("stop_price")}

    async def close_future_position(self, symbol: str, **kwargs: Any) -> dict[str, Any]:
        self.closes.append({"symbol": symbol, **kwargs})
        return {"ok": True, "status": "Closed"}


@pytest.mark.asyncio
async def test_fanout_places_all_accounts():
    fake = _FakeUnderlying()
    accounts = [
        PropAccount(id="1", name="lead"),
        PropAccount(id="2", name="f2", size_scale=1.0),
        PropAccount(id="3", name="f3", size_scale=2.0),
    ]
    broker = MultiAccountBroker(fake, accounts, signal_id="SIG-TEST")
    res = await broker.place_future_bracket(
        "MES",
        side="SELL",
        contracts=6,
        stop_price=5100.0,
    )
    assert res["ok"] is True
    assert res["order_ref"] == "TV-MES-1-TEST"
    assert len(res["prop_copies"]) == 3
    assert all(c["ok"] for c in res["prop_copies"])
    assert fake.places[0]["contracts"] == 6
    assert fake.places[2]["contracts"] == 12  # size_scale 2
    assert res["copy_failures"] == 0


@pytest.mark.asyncio
async def test_lead_failure_aborts():
    fake = _FakeUnderlying(fail_accounts={"1"})
    broker = MultiAccountBroker(
        fake,
        [PropAccount(id="1"), PropAccount(id="2")],
    )
    res = await broker.place_future_bracket(
        "MES", side="BUY", contracts=1, stop_price=1.0
    )
    assert res["ok"] is False
    assert "Lead account" in str(res["error"])
    assert len(fake.places) == 1  # did not continue to follower


@pytest.mark.asyncio
async def test_follower_retry_then_ok():
    fake = _FakeUnderlying(fail_once={"2"})
    broker = MultiAccountBroker(
        fake,
        [PropAccount(id="1"), PropAccount(id="2")],
    )
    res = await broker.place_future_bracket(
        "MES", side="BUY", contracts=1, stop_price=1.0
    )
    assert res["ok"] is True
    assert res["copy_failures"] == 0
    # lead once + follower fail + follower retry = 3
    assert len(fake.places) == 3


@pytest.mark.asyncio
async def test_modify_and_close_fanout():
    fake = _FakeUnderlying()
    broker = MultiAccountBroker(
        fake,
        [PropAccount(id="1"), PropAccount(id="2")],
    )
    placed = await broker.place_future_bracket(
        "MES", side="SELL", contracts=2, stop_price=100.0
    )
    copies = placed["prop_copies"]
    mod = await broker.modify_future_stop(
        "MES",
        placed["order_ref"],
        stop_price=99.0,
        contracts=2,
        side="SELL",
        prop_copies=copies,
    )
    assert mod["ok"] is True
    assert len(fake.modifies) == 2
    clo = await broker.close_future_position(
        "MES",
        contracts=2,
        side="BUY",
        order_ref=placed["order_ref"],
        prop_copies=copies,
    )
    assert clo["ok"] is True
    assert len(fake.closes) == 2


def test_is_prop_backed():
    rec = TradeRecord(
        environment="PAPER",
        symbol="MES",
        window=SessionWindow.ASIA,
        regime=Regime.TREND,
        spread_type=SpreadType.BEAR_PUT,
        direction=Direction.SHORT,
        contracts=6,
        entry_price=5000.0,
        max_loss=100.0,
        max_profit=100.0,
        target_r=1.5,
        status=TradeStatus.OPEN,
        order_ref="TV-MES-1-20260101120000",
        notes="",
        plan_json=json.dumps({"execution_backend": "tradovate_prop"}),
    )
    assert is_prop_backed(rec) is True


def test_factory_refuses_topstep_on_aws(monkeypatch):
    monkeypatch.setenv("EXECUTION_BACKEND", "topstepx")
    monkeypatch.setenv("ORB_FORCE_AWS_HOST", "1")
    # Clear settings cache if used
    from core.config import get_settings

    get_settings.cache_clear()
    assert _looks_like_aws() is True
    with pytest.raises(BrokerUnavailable, match="prohibited"):
        get_broker()
    monkeypatch.delenv("EXECUTION_BACKEND", raising=False)
    monkeypatch.delenv("ORB_FORCE_AWS_HOST", raising=False)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_tradovate_place_sets_is_automated():
    """Ensure place payload includes isAutomated via a captured HTTP client."""

    class _Cap:
        def __init__(self):
            self.posts: list[tuple[str, dict]] = []

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json or {}))

            class _R:
                status_code = 200
                text = "{}"

                def raise_for_status(self):
                    return None

                def json(self):
                    if "accesstokenrequest" in url:
                        return {"accessToken": "tok", "expirationTime": 999999}
                    return {"orderId": 1, "orderStatus": "Working"}

            return _R()

        async def get(self, url, params=None, headers=None):
            class _R:
                status_code = 200
                text = "[]"

                def raise_for_status(self):
                    return None

                def json(self):
                    if "suggest" in url:
                        return [{"id": 42, "name": "MESU5"}]
                    return []

            return _R()

        async def aclose(self):
            return None

    cap = _Cap()
    client = TradovateClient(
        username="u",
        password="p",
        env="demo",
        http=cap,  # type: ignore[arg-type]
    )
    await client.connect()
    res = await client.place_future_bracket(
        "MES",
        side="SELL",
        contracts=6,
        stop_price=5100.0,
        account_id="99",
    )
    assert res["ok"] is True
    bodies = [b for u, b in cap.posts if "placeorder" in u]
    assert bodies
    assert all(b.get("isAutomated") is True for b in bodies)
