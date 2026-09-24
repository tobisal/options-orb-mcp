"""Typed data models shared across the core library and MCP servers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from core.timeutils import utcnow


class SessionWindow(str, Enum):
    """Trading windows. For US options these map onto the underlying's
    behaviour during each global session rather than distinct venues."""

    ASIA = "asia"  # overnight globex range
    LONDON = "london"  # EU / US pre-market
    NEW_YORK = "new_york"  # US regular-hours open (the classic ORB)


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class Regime(str, Enum):
    TREND = "trend"
    RANGE = "range"
    UNCERTAIN = "uncertain"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class SpreadType(str, Enum):
    BULL_CALL = "bull_call_debit"
    BEAR_PUT = "bear_put_debit"
    BULL_PUT = "bull_put_credit"
    BEAR_CALL = "bear_call_credit"


class Bar(BaseModel):
    """A single OHLCV bar for the underlying."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class ORBSignal(BaseModel):
    """Output of the ORB analysis for a given symbol and window."""

    symbol: str
    window: SessionWindow
    as_of: datetime
    range_high: float
    range_low: float
    last_price: float
    direction: Direction
    breakout: bool
    # Normalised breakout strength: distance beyond the range in units of the
    # opening-range width. 0 = at the edge, 1 = one full range width beyond.
    strength: float = 0.0
    regime: Regime = Regime.UNCERTAIN
    atr: float | None = None
    notes: str = ""


class OptionLeg(BaseModel):
    right: OptionRight
    strike: float
    expiry: str  # YYYYMMDD
    action: str  # BUY | SELL
    quantity: int = 1
    # Optional per-leg quote context.
    bid: float | None = None
    ask: float | None = None
    iv: float | None = None
    delta: float | None = None


class SpreadPlan(BaseModel):
    """A fully specified, defined-risk vertical spread ready to preview/place."""

    symbol: str
    spread_type: SpreadType
    direction: Direction
    expiry: str
    long_leg: OptionLeg
    short_leg: OptionLeg
    contracts: int = Field(ge=0)
    # Per-1-contract economics (in underlying currency, before multiplier).
    net_debit: float = 0.0  # positive = you pay (debit); negative = you receive (credit)
    max_loss: float = 0.0  # total across all contracts, always >= 0
    max_profit: float = 0.0  # total across all contracts
    contract_multiplier: int = 100
    target_r: float = 1.5
    take_profit_price: float | None = None  # spread price at TP
    stop_loss_price: float | None = None  # spread price at SL
    use_trailing_stop: bool = False
    trail_activate_r: float = 0.5
    trail_distance_r: float = 0.3
    original_stop_loss_price: float | None = None
    rationale: str = ""

    @property
    def reward_risk(self) -> float:
        return self.max_profit / self.max_loss if self.max_loss > 0 else 0.0


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class TradeRecord(BaseModel):
    """A persisted trade for the research journal."""

    id: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    environment: str = "PAPER"
    symbol: str
    window: SessionWindow
    regime: Regime
    spread_type: SpreadType
    direction: Direction
    contracts: int
    entry_price: float  # net debit/credit at entry
    max_loss: float
    max_profit: float
    target_r: float
    status: TradeStatus = TradeStatus.PENDING
    exit_price: float | None = None
    pnl: float | None = None
    closed_at: datetime | None = None
    signal_strength: float = 0.0
    order_ref: str | None = None
    notes: str = ""
    plan_json: str = "{}"
