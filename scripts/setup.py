"""Register the four MCP servers with an LLM client.

Writes ``.cursor/mcp.json`` (Cursor) and prints an equivalent Claude Desktop
config block. Uses the current Python interpreter so the servers run inside the
same environment this script was launched from.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SERVERS = {
    "market-data-mcp": "servers.market_data_mcp.server",
    "research-mcp": "servers.research_mcp.server",
    "optimiser-mcp": "servers.optimiser_mcp.server",
    "execution-mcp": "servers.execution_mcp.server",
}


def _server_entry(module: str) -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", module],
        "env": {"PYTHONPATH": str(REPO_ROOT)},
    }


def build_config() -> dict:
    return {"mcpServers": {name: _server_entry(mod) for name, mod in SERVERS.items()}}


def main() -> None:
    config = build_config()

    cursor_dir = REPO_ROOT / ".cursor"
    cursor_dir.mkdir(exist_ok=True)
    cursor_path = cursor_dir / "mcp.json"

    # Merge with any existing mcp.json rather than clobbering unrelated servers.
    if cursor_path.exists():
        try:
            existing = json.loads(cursor_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        existing.setdefault("mcpServers", {}).update(config["mcpServers"])
        config = existing

    cursor_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    print(f"Wrote {cursor_path}")
    print(f"Python interpreter: {sys.executable}")
    print("\nRegistered servers:")
    for name in SERVERS:
        print(f"  - {name}")
    print(
        "\nRestart Cursor (or reload MCP servers) to pick up the changes.\n"
        "\nOn a new PC, still needed:\n"
        "  1. copy .env.example .env  (IBKR_PORT=4002, ACCOUNT_MODE=paper)\n"
        "  2. IB Gateway paper logged in, API on 4002  (docs/IBKR_SETUP.md)\n"
        "  3. python -m scripts.check_ibkr\n"
        "  4. python -m scripts.fetch_history --symbol SPY --days 365\n"
        "     (or copy data/history/SPY_5mins.csv from the other machine)\n"
        "  5. python -m dashboard.app\n"
        "  6. python -m scripts.nightly_optimise --install-task\n"
        "  7. (optional) fill DISCORD_* in .env, then python -m scripts.discord_bot\n"
        "\nFor Claude Desktop, add this to claude_desktop_config.json:\n"
    )
    print(json.dumps(build_config(), indent=2))


if __name__ == "__main__":
    main()
