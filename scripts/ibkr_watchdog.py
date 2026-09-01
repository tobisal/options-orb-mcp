"""IBKR API watchdog: probe, kick socat if needed, restart dashboard.

Run on the EC2 host via cron so a dead API (dashboard still up) recovers
without a full ``compose down``.

Examples::

    python -m scripts.ibkr_watchdog
    python -m scripts.ibkr_watchdog --compose-dir /home/ubuntu/options-orb-mcp
    python -m scripts.ibkr_watchdog --loop --every 300

Cron (every 5 minutes)::

    */5 * * * * cd /home/ubuntu/options-orb-mcp && /usr/bin/docker compose run --rm --no-deps \\
      -e IBKR_HOST=127.0.0.1 -e IBKR_PORT=4002 watchdog >>/home/ubuntu/orb-watchdog.log 2>&1

Prefer the host shell helper ``scripts/ibkr_watchdog.sh`` (uses docker exec).
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from pathlib import Path


def _probe() -> tuple[bool, str]:
    """Readonly connect using the app client (same network as caller)."""
    try:
        from core.ibkr_client import IBKRClient, IBKRUnavailable
    except Exception as exc:  # pragma: no cover
        return False, f"import failed: {exc}"

    async def _run() -> None:
        async with IBKRClient(readonly=True) as ib:
            if not ib.is_connected():
                raise IBKRUnavailable("connected flag false")

    try:
        asyncio.run(_run())
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _docker(*args: str, cwd: Path | None = None) -> int:
    cmd = ["docker", *args]
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


def _compose(*args: str, cwd: Path) -> int:
    cmd = ["docker", "compose", *args]
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(cwd))


def heal(compose_dir: Path, *, restart_dashboard: bool) -> int:
    ok, msg = _probe()
    if ok:
        print(f"IBKR API healthy ({msg})", flush=True)
        return 0

    print(f"IBKR API down: {msg}", flush=True)
    print("Kicking socat inside orb-ib-gateway...", flush=True)
    _docker("exec", "orb-ib-gateway", "pkill", "-x", "socat")
    time.sleep(8)

    ok2, msg2 = _probe()
    if ok2:
        print(f"API recovered after socat kick ({msg2})", flush=True)
        if restart_dashboard:
            print("Restarting dashboard to drop stale sockets...", flush=True)
            _compose("restart", "dashboard", cwd=compose_dir)
        return 0

    print(f"Still down after socat kick: {msg2}", flush=True)
    if restart_dashboard:
        print("Restarting dashboard anyway (clears client cooldown)...", flush=True)
        _compose("restart", "dashboard", cwd=compose_dir)
    return 1


def main() -> None:
    p = argparse.ArgumentParser(description="Heal a dead IBKR API while dashboard stays up.")
    p.add_argument(
        "--compose-dir",
        type=Path,
        default=Path.cwd(),
        help="Repo root with docker-compose.yml (default: cwd).",
    )
    p.add_argument(
        "--no-restart-dashboard",
        action="store_true",
        help="Only kick socat; do not docker compose restart dashboard.",
    )
    p.add_argument("--loop", action="store_true", help="Run forever.")
    p.add_argument("--every", type=int, default=300, help="Seconds between loops (default 300).")
    args = p.parse_args()
    compose_dir = args.compose_dir.resolve()
    restart = not args.no_restart_dashboard

    if args.loop:
        while True:
            code = heal(compose_dir, restart_dashboard=restart)
            print(f"sleep {args.every}s (last exit={code})", flush=True)
            time.sleep(max(args.every, 60))
    else:
        raise SystemExit(heal(compose_dir, restart_dashboard=restart))


if __name__ == "__main__":
    main()
