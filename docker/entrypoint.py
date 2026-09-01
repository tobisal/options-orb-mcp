"""Container entrypoint for the Options ORB image.

Default command is ``dashboard``. Named services:

  dashboard  web GUI on DASHBOARD_PORT (default 8787)
  discord    Discord remote-control bot
  all        dashboard + Discord in one container
  demo       offline end-to-end demo (no IBKR)
  check      IBKR connectivity probe
  watchdog  host-side style heal when run with docker + compose dir

Anything else is executed as a raw command, e.g.
``docker run ... python -m servers.market_data_mcp.server``.
"""

from __future__ import annotations

import os
import subprocess
import sys

_SERVICES = {
    "dashboard": ["-m", "dashboard.app"],
    "discord": ["-m", "scripts.discord_bot"],
    "demo": ["-m", "scripts.demo"],
    "check": ["-m", "scripts.check_ibkr"],
    "watchdog": ["-m", "scripts.ibkr_watchdog"],
}


def _exec(module_args: list[str]) -> None:
    os.execv(sys.executable, [sys.executable, *module_args])


def main() -> None:
    args = sys.argv[1:]
    if not args:
        args = [os.environ.get("ORB_SERVICE", "dashboard")]

    head = args[0]
    if head == "all":
        dash = subprocess.Popen([sys.executable, "-m", "dashboard.app"])
        try:
            raise SystemExit(subprocess.call([sys.executable, "-m", "scripts.discord_bot"]))
        finally:
            dash.terminate()
            try:
                dash.wait(timeout=10)
            except subprocess.TimeoutExpired:
                dash.kill()
        return

    if head in _SERVICES:
        _exec(_SERVICES[head])

    os.execvp(head, args)


if __name__ == "__main__":
    main()
