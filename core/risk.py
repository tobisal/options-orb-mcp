"""Risk management: position sizing, per-trade caps, daily loss kill switch,
and the paper/live safety gate.

Every order the executor places must pass ``RiskManager.pre_trade_checks``.
Sizing assumes **defined-risk** structures only (verticals), so the maximum
loss of a position is known before entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from core.config import Settings, get_settings
from core.db import Database
from core.models import SessionWindow
from core.timeutils import utcnow

# Rough FX to translate USD option risk into the account currency (GBP) for
# sizing. This is a conservative default; for live use, source a live rate.
DEFAULT_ACCT_CCY_PER_USD = 0.79  # ~GBP per USD


@dataclass
class RiskDecision:
    approved: bool
    contracts: int
    risk_budget: float  # account-currency amount allowed at risk this trade
    projected_risk: float  # account-currency max loss of the sized position
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "contracts": self.contracts,
            "risk_budget": round(self.risk_budget, 2),
            "projected_risk": round(self.projected_risk, 2),
            "reasons": self.reasons,
        }


@dataclass
class RiskManager:
    settings: Settings = field(default_factory=get_settings)
    db: Database | None = None
    acct_ccy_per_usd: float = DEFAULT_ACCT_CCY_PER_USD

    def __post_init__(self) -> None:
        if self.db is None:
            self.db = Database()

    # --- budgets -----------------------------------------------------------
    def risk_budget_per_trade(self) -> float:
        """Account-currency amount permitted at risk on a single trade."""
        return self.settings.starting_capital * self.settings.max_risk_per_trade

    def daily_loss_limit(self) -> float:
        return self.settings.starting_capital * self.settings.max_daily_loss

    # --- sizing ------------------------------------------------------------
    def size_futures(
        self,
        stop_points: float,
        *,
        point_value: float = 5.0,
        requested_contracts: int | None = None,
    ) -> int:
        """Size MES (or similar) futures by dollar stop risk.

        Risk per contract (USD) = stop_points × point_value.
        """
        if stop_points <= 0 or point_value <= 0:
            return 0
        per_contract_usd = stop_points * point_value
        per_contract_acct = per_contract_usd * self.acct_ccy_per_usd
        budget = self.risk_budget_per_trade()
        n = max(int(math.floor(budget / per_contract_acct)), 0)
        if requested_contracts is not None:
            n = min(n, max(int(requested_contracts), 0))
        return n

    def pre_trade_checks_futures(
        self,
        stop_points: float,
        *,
        point_value: float = 5.0,
        requested_contracts: int | None = None,
        max_concurrent: int = 1,
        window: SessionWindow | None = None,
    ) -> RiskDecision:
        """Risk gates for MES futures (stop-distance sizing)."""
        assert self.db is not None
        reasons: list[str] = []
        reasons.extend(self._live_gate_reasons())

        contracts = self.size_futures(
            stop_points,
            point_value=point_value,
            requested_contracts=requested_contracts,
        )
        # Prefer configured fixed size when budget allows at least that many.
        if requested_contracts and contracts >= requested_contracts:
            contracts = requested_contracts
        elif requested_contracts and contracts < requested_contracts and contracts > 0:
            pass  # use what budget allows
        elif requested_contracts and contracts == 0:
            reasons.append(
                f"Stop risk {stop_points:.2f} pts × ${point_value:.0f} exceeds "
                f"per-trade budget {self.risk_budget_per_trade():.2f} "
                f"{self.settings.account_currency}."
            )

        env = self.environment()
        open_count = self.db.open_position_count(environment=env)
        if open_count >= max_concurrent:
            reasons.append(
                f"MES max concurrent positions reached ({open_count}/{max_concurrent})."
            )
        if open_count >= self.settings.max_open_positions:
            reasons.append(
                f"Max open positions reached ({open_count}/{self.settings.max_open_positions})."
            )

        tripped, realised = self.daily_loss_tripped()
        if tripped:
            reasons.append(
                f"Daily loss kill switch active (realised {realised:.2f} "
                f"{self.settings.account_currency} <= -{self.daily_loss_limit():.2f})."
            )

        projected = contracts * stop_points * point_value * self.acct_ccy_per_usd
        approved = len(reasons) == 0 and contracts > 0
        return RiskDecision(
            approved=approved,
            contracts=contracts,
            risk_budget=self.risk_budget_per_trade(),
            projected_risk=projected,
            reasons=reasons,
        )

    # --- gates -------------------------------------------------------------
    def environment(self) -> str:
        return self.settings.trading_environment()

    def daily_loss_tripped(self) -> tuple[bool, float]:
        """Whether today's realised losses breach the daily kill switch."""
        assert self.db is not None
        start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        realised = self.db.realised_pnl_since(start_of_day, environment=self.environment())
        tripped = realised <= -self.daily_loss_limit()
        return tripped, realised

    def _live_gate_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.settings.live_requested_but_unconfirmed:
            reasons.append(
                "ACCOUNT_MODE=live but LIVE_TRADING_CONFIRM interlock is not set to "
                "'I_UNDERSTAND_THE_RISK'; refusing to trade live."
            )
        return reasons

    def pre_trade_checks(
        self,
        per_contract_max_loss_usd: float,
        *,
        requested_contracts: int | None = None,
        window: SessionWindow | None = None,
    ) -> RiskDecision:
        """Run all gates and size the trade. Returns an approve/deny decision."""
        assert self.db is not None
        reasons: list[str] = []

        # 1. Safety gate integrity.
        reasons.extend(self._live_gate_reasons())

        # 2. Size the position from the risk budget.
        max_contracts = self.size_position(per_contract_max_loss_usd)
        contracts = max_contracts
        if requested_contracts is not None:
            contracts = min(requested_contracts, max_contracts)

        if per_contract_max_loss_usd <= 0:
            reasons.append("Per-contract max loss is non-positive; not a defined-risk spread.")
        if contracts <= 0 and per_contract_max_loss_usd > 0:
            single = per_contract_max_loss_usd * self.acct_ccy_per_usd
            pct = single / self.settings.starting_capital * 100 if self.settings.starting_capital else 0
            reasons.append(
                f"One contract would risk {single:.2f} {self.settings.account_currency} "
                f"({pct:.1f}% of capital), exceeding the per-trade cap of "
                f"{self.risk_budget_per_trade():.2f} "
                f"({self.settings.max_risk_per_trade * 100:.1f}%). "
                "Increase MAX_RISK_PER_TRADE, use a narrower spread, or add capital."
            )

        env = self.environment()
        per_window = self.settings.max_open_positions_per_window
        daily_cap = self.settings.max_open_positions
        start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # 3. Concurrent + daily caps: 3 active per window, 9 per day across
        # Asia / London / New York.
        open_count = self.db.open_position_count(environment=env)
        if open_count >= daily_cap:
            reasons.append(f"Max open positions reached ({open_count}/{daily_cap}).")

        today_count = self.db.entries_since(start_of_day, environment=env)
        if today_count >= daily_cap:
            reasons.append(
                f"Daily trade cap reached ({today_count}/{daily_cap} entries today)."
            )

        if window is not None:
            open_in_window = self.db.open_position_count(environment=env, window=window)
            if open_in_window >= per_window:
                reasons.append(
                    f"Max open positions for {window.value} reached "
                    f"({open_in_window}/{per_window})."
                )
            today_in_window = self.db.entries_since(
                start_of_day, environment=env, window=window
            )
            if today_in_window >= per_window:
                reasons.append(
                    f"Daily cap for {window.value} reached "
                    f"({today_in_window}/{per_window} entries today)."
                )

        # 4. Daily loss kill switch.
        tripped, realised = self.daily_loss_tripped()
        if tripped:
            reasons.append(
                f"Daily loss kill switch active (realised {realised:.2f} "
                f"{self.settings.account_currency} <= -{self.daily_loss_limit():.2f})."
            )

        projected_risk = contracts * per_contract_max_loss_usd * self.acct_ccy_per_usd
        approved = len(reasons) == 0 and contracts > 0
        return RiskDecision(
            approved=approved,
            contracts=contracts,
            risk_budget=self.risk_budget_per_trade(),
            projected_risk=projected_risk,
            reasons=reasons,
        )
