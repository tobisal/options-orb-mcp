"""Risk management: position sizing, per-trade caps, daily loss kill switch,
and the paper/live safety gate.

Every order the executor places must pass ``RiskManager.pre_trade_checks``.
Sizing assumes **defined-risk** structures only (verticals), so the maximum
loss of a position is known before entry.

Risk budgets are fractions of **current equity** (starting capital + lifetime
realised P&L), so position size grows as the account grows.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from core.config import Settings, get_settings
from core.db import Database
from core.models import SessionWindow, TradeRecord, TradeStatus
from core.timeutils import utcnow
from core.trail import open_risk_per_contract_usd

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
    open_risk_acct: float = 0.0  # current stop-based open risk across book

    def as_dict(self) -> dict:
        return {
            "approved": self.approved,
            "contracts": self.contracts,
            "risk_budget": round(self.risk_budget, 2),
            "projected_risk": round(self.projected_risk, 2),
            "open_risk_acct": round(self.open_risk_acct, 2),
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

    # --- equity ------------------------------------------------------------
    def current_equity(self) -> float:
        """Starting capital + lifetime realised P&L (account currency).

        Open mark-to-market is omitted here so sizing does not depend on a
        live spot feed; closed P&L still compounds size as the book grows.
        Floor at a small positive amount so a drawdown cannot invert sizing.
        """
        assert self.db is not None
        start = float(self.settings.starting_capital)
        realised = float(self.db.realised_pnl(environment=self.environment()))
        return max(start + realised, start * 0.05, 1.0)

    # --- budgets -----------------------------------------------------------
    def risk_budget_per_trade(self) -> float:
        """Account-currency amount permitted at risk on a single trade."""
        from core.weekly_hunter import load_weekly_hunter_config, week_status

        equity = self.current_equity()
        settings = self.settings
        cfg = load_weekly_hunter_config()
        if settings.weekly_hunter_enabled and cfg.enabled:
            status = week_status(self.db, cfg)
            return equity * status.risk_fraction
        return equity * settings.max_risk_per_trade

    def daily_loss_limit(self) -> float:
        from core.weekly_hunter import load_weekly_hunter_config

        equity = self.current_equity()
        cfg = load_weekly_hunter_config()
        if self.settings.weekly_hunter_enabled and cfg.enabled:
            return equity * cfg.max_daily_loss
        return equity * self.settings.max_daily_loss

    # --- sizing ------------------------------------------------------------
    def _contracts_cap(self) -> int | None:
        """Optional hunter contract ceiling that scales with equity.

        At starting capital the cap equals ``contracts_scale`` (e.g. 2).
        As equity doubles, the cap doubles, so size can grow with the account.
        """
        try:
            from core.weekly_hunter import load_weekly_hunter_config

            cfg = load_weekly_hunter_config()
            if not (self.settings.weekly_hunter_enabled and cfg.enabled):
                return None
            start = max(float(self.settings.starting_capital), 1.0)
            equity = self.current_equity()
            return max(1, int(math.floor(cfg.contracts_scale * equity / start)))
        except Exception:
            return None

    def size_position(self, per_contract_max_loss_usd: float) -> int:
        """Number of contracts whose combined max loss fits the risk budget.

        ``per_contract_max_loss_usd`` is the USD max loss of one spread
        contract (already inclusive of the x100 multiplier).
        When weekly hunter is on, contracts are capped at a ceiling that
        scales with current equity (``contracts_scale`` at start capital).
        """
        if per_contract_max_loss_usd <= 0:
            return 0
        per_contract_acct = per_contract_max_loss_usd * self.acct_ccy_per_usd
        budget = self.risk_budget_per_trade()
        n = max(int(math.floor(budget / per_contract_acct)), 0)
        cap = self._contracts_cap()
        if cap is not None:
            n = min(n, cap)
        return n

    def open_risk_acct(self, trade: TradeRecord) -> float:
        """Account-currency risk still on an open trade (current stop vs entry)."""
        try:
            plan = json.loads(trade.plan_json or "{}")
        except json.JSONDecodeError:
            plan = {}
        if not isinstance(plan, dict):
            plan = {}
        per = open_risk_per_contract_usd(plan, float(trade.entry_price))
        return round(per * max(trade.contracts, 0) * self.acct_ccy_per_usd, 2)

    def total_open_risk_acct(self, environment: str | None = None) -> float:
        assert self.db is not None
        env = environment or self.environment()
        opens = self.db.query_trades(status=TradeStatus.OPEN, environment=env, limit=1000)
        return round(sum(self.open_risk_acct(t) for t in opens), 2)

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

        equity = self.current_equity()
        if per_contract_max_loss_usd <= 0:
            reasons.append("Per-contract max loss is non-positive; not a defined-risk spread.")
        if contracts <= 0 and per_contract_max_loss_usd > 0:
            single = per_contract_max_loss_usd * self.acct_ccy_per_usd
            pct = single / equity * 100 if equity else 0
            reasons.append(
                f"One contract would risk {single:.2f} {self.settings.account_currency} "
                f"({pct:.1f}% of equity {equity:.2f}), exceeding the per-trade cap of "
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
        open_risk = self.total_open_risk_acct(env)
        approved = len(reasons) == 0 and contracts > 0
        return RiskDecision(
            approved=approved,
            contracts=contracts,
            risk_budget=self.risk_budget_per_trade(),
            projected_risk=projected_risk,
            reasons=reasons,
            open_risk_acct=open_risk,
        )
