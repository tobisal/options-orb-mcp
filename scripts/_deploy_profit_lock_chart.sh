#!/bin/bash
set -euo pipefail
cd ~/options-orb-mcp
docker compose cp dashboard/app.py dashboard:/app/dashboard/app.py
docker compose cp dashboard/static/index.html dashboard:/app/dashboard/static/index.html
docker compose cp core/strategy/mes_5orb/exits.py dashboard:/app/core/strategy/mes_5orb/exits.py
docker compose cp core/strategy/mes_5orb/sessions.py dashboard:/app/core/strategy/mes_5orb/sessions.py
docker compose cp core/engine.py dashboard:/app/core/engine.py
docker compose cp core/backtest_mes.py dashboard:/app/core/backtest_mes.py
docker compose cp core/journal.py dashboard:/app/core/journal.py
docker compose cp configs/mes_5orb.json dashboard:/app/configs/mes_5orb.json
docker compose restart dashboard
sleep 8
curl -sS 'http://127.0.0.1:8787/api/ticker?symbol=MES&hours=6' | python3 - <<'PY'
import json,sys
d=json.load(sys.stdin)
lv=d.get("levels") or []
print("levels", len(lv))
for x in lv:
    kind=str(x.get("kind") or "")
    i=str(x.get("id") or "")
    if kind in ("arm","lock") or "profit" in i:
        print(i, x.get("label"), x.get("price"))
PY
