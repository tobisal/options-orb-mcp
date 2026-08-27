"""Nightly ORB optimiser: rank and apply tomorrow's set per session window.

Default clock is 23:30 GMT (11:30pm) — after the New York cash close and
before Asia. Override with ``--at 11:30`` if you meant 11:30 UTC.

Examples:
    python -m scripts.nightly_optimise
    python -m scripts.nightly_optimise --dry-run
    python -m scripts.nightly_optimise --install-task
    python -m scripts.nightly_optimise --loop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.config import REPO_ROOT, get_settings
from core.nightly import run_nightly_optimise
from core.timeutils import utcnow

TASK_NAME = "OptionsORB-NightlyOptimise"
_REPORT_DIR = REPO_ROOT / "data" / "nightly"


def _parse_hhmm(raw: str) -> tuple[int, int]:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("Time must be HH:MM (GMT), e.g. 23:30")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError("Time must be HH:MM in 24h GMT")
    return hour, minute


def _save_report(report: dict) -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    day = (report.get("as_of") or utcnow().isoformat())[:10]
    path = _REPORT_DIR / f"{day}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    latest = _REPORT_DIR / "latest.json"
    latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _print_report(report: dict) -> None:
    if not report.get("ok"):
        print(f"FAILED: {report.get('error')}")
        return
    print(
        f"{report['symbol']}  {report.get('bars')} bars  "
        f"applied {report.get('applied_windows')}/3 windows"
    )
    if report.get("warning"):
        print(f"warning: {report['warning']}")
    for row in report.get("windows") or []:
        best = row.get("best") or {}
        params = best.get("params") or {}
        flag = "APPLIED" if row.get("applied") else "SKIPPED"
        print(
            f"  {flag:8} {row['window']:10}  "
            f"OR {params.get('opening_range_minutes')}m  "
            f"buf {params.get('breakout_buffer_atr')}  "
            f"str {params.get('min_strength')}  "
            f"score {best.get('score')}  "
            f"{row.get('reason')}"
        )


async def _run_once(args: argparse.Namespace) -> int:
    def log(msg: str) -> None:
        print(msg, flush=True)

    report = await run_nightly_optimise(
        symbol=args.symbol,
        lookback_days=args.lookback_days,
        apply=not args.dry_run,
        min_trades=args.min_trades,
        refresh_days=args.refresh_days,
        progress=log,
    )
    path = _save_report(report)
    _print_report(report)
    print(f"Report: {path}")
    return 0 if report.get("ok") else 1


def _seconds_until(hour: int, minute: int) -> float:
    now = datetime.now(UTC)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 0.0)


def _install_windows_task(hour: int, minute: int, extra_args: list[str]) -> int:
    if sys.platform != "win32":
        print("Scheduled-task install is Windows-only. Use cron: 30 23 * * *")
        return 1
    python = sys.executable
    repo = str(REPO_ROOT)
    args = " ".join(["-m", "scripts.nightly_optimise", *extra_args])
    utc = datetime.now(UTC).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    local = utc.astimezone()
    at = local.strftime("%H:%M")
    tz_name = datetime.now().astimezone().tzname()
    ps = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{python}' -Argument '{args}' -WorkingDirectory '{repo}'
$trigger = New-ScheduledTaskTrigger -Daily -At '{at}'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output 'Installed {TASK_NAME} daily at {at} local ({tz_name}) = {hour:02d}:{minute:02d} GMT'
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip() or "Failed to register scheduled task.")
        return result.returncode
    print(
        "Windows Task Scheduler uses local time. After a DST change, re-run "
        "--install-task (or use --loop, which waits on GMT)."
    )
    return 0


def _uninstall_windows_task() -> int:
    if sys.platform != "win32":
        return 1
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )
    print((result.stdout or result.stderr or "").strip() or f"Removed {TASK_NAME}")
    return 0 if result.returncode == 0 else result.returncode


def main() -> None:
    settings = get_settings()
    p = argparse.ArgumentParser(
        description=(
            "Rank ORB parameters for Asia, London and New York and apply the "
            "#1 set for the next sessions (same as dashboard Use for trading)."
        )
    )
    p.add_argument("--symbol", default=settings.default_symbol)
    p.add_argument("--lookback-days", type=int, default=60)
    p.add_argument("--refresh-days", type=int, default=5)
    p.add_argument("--min-trades", type=int, default=5)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Rank and save the backtest, but do not change live/paper params.",
    )
    p.add_argument(
        "--at",
        default="23:30",
        type=str,
        help="GMT clock for --loop / --install-task (default 23:30 = 11:30pm GMT).",
    )
    p.add_argument(
        "--install-task",
        action="store_true",
        help="Register a daily Windows scheduled task at --at GMT.",
    )
    p.add_argument("--uninstall-task", action="store_true")
    p.add_argument(
        "--loop",
        action="store_true",
        help="Sleep until --at GMT each day and run (timezone-correct).",
    )
    args = p.parse_args()
    hour, minute = _parse_hhmm(args.at)

    if args.uninstall_task:
        raise SystemExit(_uninstall_windows_task())
    if args.install_task:
        extra = [
            "--symbol",
            args.symbol,
            "--lookback-days",
            str(args.lookback_days),
            "--refresh-days",
            str(args.refresh_days),
            "--min-trades",
            str(args.min_trades),
        ]
        if args.dry_run:
            extra.append("--dry-run")
        raise SystemExit(_install_windows_task(hour, minute, extra))

    if args.loop:
        print(
            f"Nightly loop: {args.symbol} at {hour:02d}:{minute:02d} GMT "
            f"(lookback {args.lookback_days}d). Ctrl+C to stop.",
            flush=True,
        )
        while True:
            wait = _seconds_until(hour, minute)
            nxt = datetime.now(UTC) + timedelta(seconds=wait)
            print(f"Sleeping {wait / 3600:.2f}h until {nxt:%Y-%m-%d %H:%M} GMT", flush=True)
            time.sleep(wait)
            code = asyncio.run(_run_once(args))
            if code != 0:
                print("Run failed; will retry at the next scheduled clock.", flush=True)
            time.sleep(60)
        return

    raise SystemExit(asyncio.run(_run_once(args)))


if __name__ == "__main__":
    main()
