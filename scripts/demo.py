"""End-to-end offline demo of the ORB -> research -> execute loop.

Runs entirely on deterministic synthetic data (no IBKR needed), exercising every
agent: market analysis -> risk-sized spread -> simulated placement -> journal ->
backtest/optimisation. Run with:  python -m scripts.demo
"""

from __future__ import annotations

import asyncio
import json

import servers.execution_mcp.server as execution
import servers.optimiser_mcp.server as optimiser
import servers.research_mcp.server as research


def _hdr(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


async def _find_breakout_seed(window: str = "new_york", max_seed: int = 80) -> int | None:
    for seed in range(1, max_seed):
        r = await execution._build_plan("SPY", window, True, 1.5, seed)
        if r.get("ok") and r.get("tradeable"):
            return seed
    return None


async def run() -> None:
    window = "new_york"
    _hdr("1. MARKET ANALYSIS + RISK SIZING (execution.preview_spread)")
    seed = (await _find_breakout_seed(window)) or 1
    print(f"Using synthetic seed {seed} (chosen for a qualifying breakout).")
    preview = await execution.preview_spread("SPY", window, 1.5, True, seed)
    sig = preview["signal"]
    print(f"Signal: {sig['direction']} breakout, strength {sig['strength']}, regime {sig['regime']}")
    plan = preview["plan"]
    print(
        f"Proposed {plan['spread_type']} {plan['long_leg']['strike']}/{plan['short_leg']['strike']} "
        f"x{plan['contracts']} | max loss {plan['max_loss']} | max profit {plan['max_profit']} "
        f"| TP {plan['take_profit_price']} SL {plan['stop_loss_price']}"
    )
    print(f"Risk decision: {preview['risk']}")

    _hdr("2. EXECUTION (execution.place_spread - simulated paper fill)")
    placed = await execution.place_spread("SPY", window, 1.5, True, True, seed)
    trade_id = placed.get("trade_id")
    print(f"Placed: {json.dumps({k: placed.get(k) for k in ('ok', 'trade_id', 'environment', 'placement')}, default=str)}")

    _hdr("3. MANAGE / CLOSE (execution.close_position at take-profit)")
    if trade_id is not None:
        closed = await execution.close_position(trade_id, plan["take_profit_price"])
        print(f"Closed at TP: {closed}")

    _hdr("4. RESEARCH (research.performance_report)")
    report = await research.performance_report()
    ov = report["overall"]
    print(
        f"closed_trades={report['closed_trades']} | win% {ov['win_rate']:.0%} | "
        f"expectancy {ov['expectancy']:.2f} | total P&L {ov['total_pnl']:.2f}"
    )
    verdict = await research.learn_from_history(window)
    print(f"learn_from_history({window}): verdict={verdict['verdict']} - {verdict['recommendation']}")

    _hdr("5. OPTIMISATION (optimiser.optimise - grid search, offline)")
    opt = await optimiser.optimise("SPY", window, True, 60, 3, 3)
    print(f"Tested {opt['combinations_tested']} parameter combinations. Top 3:")
    for row in opt["top"]:
        m = row["metrics"]
        print(
            f"  score {row['score']:>7} | trades {m['trades']:>3} | win% {m['win_rate']:.0%} "
            f"| expectancy {m['expectancy']:.2f} | PF {m['profit_factor']:.2f} | params {row['params']}"
        )

    _hdr("6. WALK-FORWARD VALIDATION (optimiser.walk_forward_test - out-of-sample)")
    wf = await optimiser.walk_forward_test("SPY", window, 3, True, 90, 3)
    if "error" in wf:
        print(wf["error"])
    else:
        agg = wf["aggregate_out_of_sample"]
        print(f"Folds: {wf['folds']}")
        print(
            f"Aggregate OUT-OF-SAMPLE: trades {agg['trades']}, win% {agg['win_rate']:.0%}, "
            f"expectancy {agg['expectancy']:.2f}, profit factor {agg['profit_factor']:.2f}"
        )
        print(wf["walk_forward_note"])

    print("\nDemo complete. This ran fully offline on synthetic data.")
    print("With TWS/IB Gateway running and use_synthetic omitted, the same tools")
    print("operate on live market data and route paper (or gated live) orders.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
