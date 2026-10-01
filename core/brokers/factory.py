"""Resolve execution backend (IBKR vs Tradovate prop multi-account)."""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

from core.brokers.base import BrokerUnavailable, FuturesBroker
from core.brokers.ibkr_adapter import IbkrBroker
from core.brokers.multi_account import PropAccount
from core.config import get_settings

_log = logging.getLogger("orb.broker.factory")


def resolve_execution_backend() -> str:
    settings = get_settings()
    raw = (
        getattr(settings, "execution_backend", None)
        or os.environ.get("EXECUTION_BACKEND")
        or "ibkr"
    )
    return str(raw).strip().lower()


def _looks_like_aws() -> bool:
    """Heuristic: AWS compute / known metadata hostname patterns."""
    if os.environ.get("AWS_EXECUTION_ENV") or os.environ.get("AWS_REGION"):
        return True
    if os.environ.get("ECS_CONTAINER_METADATA_URI"):
        return True
    host = socket.gethostname().lower()
    if "ip-" in host and ".ec2" in host:
        return True
    # Explicit override for local testing of the guard.
    if os.environ.get("ORB_FORCE_AWS_HOST") == "1":
        return True
    return False


def _parse_prop_accounts(raw: str | None) -> list[PropAccount]:
    if not raw or not str(raw).strip():
        return []
    text = str(raw).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Comma-separated account ids
        return [
            PropAccount(id=part.strip(), name=part.strip())
            for part in text.split(",")
            if part.strip()
        ]
    out: list[PropAccount] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                aid = str(item.get("id") or item.get("account_id") or "").strip()
                if not aid:
                    continue
                out.append(
                    PropAccount(
                        id=aid,
                        name=str(item.get("name") or aid),
                        enabled=bool(item.get("enabled", True)),
                        size_scale=float(item.get("size_scale") or 1.0),
                    )
                )
            elif item is not None:
                out.append(PropAccount(id=str(item), name=str(item)))
    return out


def get_broker(*, readonly: bool = False) -> FuturesBroker:
    """Factory: IBKR by default; Tradovate multi-account when configured."""
    backend = resolve_execution_backend()
    if backend in {"topstep", "topstepx", "projectx"}:
        if _looks_like_aws():
            raise BrokerUnavailable(
                "TopstepX/ProjectX order origin from AWS/VPS is prohibited by firm ToS. "
                "Use EXECUTION_BACKEND=tradovate_prop on cloud hosts, or run Topstep "
                "orders from a personal device."
            )
        raise BrokerUnavailable(
            "TopstepX backend is not implemented on this branch; use tradovate_prop."
        )

    if backend in {"tradovate", "tradovate_prop", "prop"}:
        # Lazy import so the default IBKR dashboard path does not require httpx
        # at process start (Docker images without optional deps still boot).
        from core.brokers.multi_account import MultiAccountBroker
        from core.brokers.tradovate_client import TradovateClient

        settings = get_settings()
        accounts = _parse_prop_accounts(
            getattr(settings, "prop_accounts_json", None)
            or os.environ.get("PROP_ACCOUNTS_JSON")
        )
        if not accounts:
            raise BrokerUnavailable(
                "EXECUTION_BACKEND=tradovate_prop requires PROP_ACCOUNTS_JSON."
            )
        client = TradovateClient(
            username=getattr(settings, "tradovate_user", "")
            or os.environ.get("TRADOVATE_USER", ""),
            password=getattr(settings, "tradovate_password", "")
            or os.environ.get("TRADOVATE_PASSWORD", ""),
            app_id=getattr(settings, "tradovate_app_id", None)
            or os.environ.get("TRADOVATE_APP_ID", "options-orb-mcp"),
            app_version=getattr(settings, "tradovate_app_version", None)
            or os.environ.get("TRADOVATE_APP_VERSION", "1.0"),
            device_id=getattr(settings, "tradovate_device_id", None)
            or os.environ.get("TRADOVATE_DEVICE_ID", "orb-aws"),
            cid=getattr(settings, "tradovate_cid", None)
            or os.environ.get("TRADOVATE_CID", ""),
            sec=getattr(settings, "tradovate_sec", None)
            or os.environ.get("TRADOVATE_SEC", ""),
            env=getattr(settings, "tradovate_env", None)
            or os.environ.get("TRADOVATE_ENV", "demo"),
        )
        _log.info(
            "Tradovate prop broker: env=%s accounts=%s",
            client.env,
            [a.id for a in accounts],
        )
        return MultiAccountBroker(client, accounts)

    return IbkrBroker(readonly=readonly)
