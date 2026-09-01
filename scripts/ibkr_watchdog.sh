#!/usr/bin/env bash
# Host-side IBKR API healer for EC2. Install with cron:
#   */5 * * * * /home/ubuntu/options-orb-mcp/scripts/ibkr_watchdog.sh >>/home/ubuntu/orb-watchdog.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

probe() {
  docker run --rm --network container:orb-ib-gateway \
    -e IBKR_HOST=127.0.0.1 -e IBKR_PORT=4002 -e ACCOUNT_MODE=paper \
    -e IBKR_CLIENT_ID=17 \
    options-orb-mcp:latest python -c "
import asyncio
from ib_async import IB
async def main():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 4002, clientId=17, readonly=True, timeout=12)
    print('API OK')
    ib.disconnect()
asyncio.run(main())
"
}

if probe; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) healthy"
  exit 0
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) API down — kicking socat"
docker exec orb-ib-gateway pkill -x socat || true
sleep 8

if probe; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) recovered — restarting dashboard"
  docker compose restart dashboard
  exit 0
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) still down — restarting dashboard (clears cooldown)"
docker compose restart dashboard
exit 1
