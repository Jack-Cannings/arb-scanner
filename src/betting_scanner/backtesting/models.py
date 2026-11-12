"""Historical inputs, explicit simulation assumptions and report records."""

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, model_validator

from betting_scanner.core.models import (
    DecimalOdds,
    DomainModel,
    Identifier,
    OddsQuote,
    OddsSnapshot,
    SettlementRule,
    Text,
    UTCTimestamp,
)
from betting_scanner.strategies.arbitrage.calculation import validate_budget
from betting_scanner.strategies.arbitrage.results import ArbitrageResult, EngineConfig


def _decimal_input(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Use Decimal, numeric strings or integers, not floats")
    return value


ExactDecimal = Annotated[
    Decimal, BeforeValidator(_decimal_input), Field(allow_inf_nan=False)
]
Money = Annotated[ExactDecimal, AfterValidator(validate_budget)]
Nonnegative = Annotated[ExactDecimal, Field(ge=0)]


class StakeLimits(DomainModel):
    minimum: Money | None = None
    maximum: Money | None = None

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("Minimum stake cannot exceed maximum stake")
        return self


class ReplayConfig(DomainModel):
    starting_bankroll: Money = Decimal("1000.00")
    stake_budget: Money = Decimal("100.00")
    minimum_roi: Nonnegative = Decimal(0)
    minimum_profit: Nonnegative = Decimal(0)
    max_event_exposure: Money = Decimal("100.00")
    max_aggregate_exposure: Money = Decimal("500.00")
    execution_delay: Annotated[timedelta, Field(ge=timedelta(0))] = timedelta(0)
    # Proportional haircut to decimal prices, deliberately adverse by default.
    slippage: Annotated[ExactDecimal, Field(ge=0, lt=1)] = Decimal("0.005")
    execution_mode: Literal["accept_all", "reject_last"] = "accept_all"
    bookmaker_limits: dict[Identifier, StakeLimits] = Field(default_factory=dict)
    engine: EngineConfig = Field(default_factory=EngineConfig)


class HistoricalSnapshot(DomainModel):
    observed_at: UTCTimestamp
    snapshot: OddsSnapshot

    @model_validator(mode="after")
    def unique_current_offers(self) -> Self:
        keys = [
            (q.provider_id, q.bookmaker_id, q.market_id, q.outcome_id)
            for q in self.snapshot.quotes
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "A full historical snapshot must contain one current offer "
                "per provider/book/outcome"
            )
        return self


class ExecutionInstruction(DomainModel):
    """Synthetic fill assumptions, revealed only at the simulated execution time."""

    execution_at: UTCTimestamp
    event_id: Identifier
    market_id: Identifier
    bookmaker_id: Identifier
    outcome_id: Identifier
    accepted: bool = Field(default=True, strict=True)
    odds_override: DecimalOdds | None = None


class BetDisposition(StrEnum):
    NOT_PLACED = "not_placed"
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"


class SettlementRecord(DomainModel):
    observed_at: UTCTimestamp
    event_id: Identifier
    market_id: Identifier
    bookmaker_id: Identifier
    outcome_id: Identifier
    disposition: Literal[BetDisposition.WON, BetDisposition.LOST, BetDisposition.VOID]
    settlement_rule: SettlementRule
    source: Literal["synthetic", "historical"]


class HistoricalDataset(DomainModel):
    name: Text
    description: Text
    snapshots: Annotated[tuple[HistoricalSnapshot, ...], Field(min_length=1)]
    settlements: tuple[SettlementRecord, ...] = ()
    execution_instructions: tuple[ExecutionInstruction, ...] = ()

    @model_validator(mode="after")
    def unambiguous_records(self) -> Self:
        keys = [(r.observed_at, r.snapshot.event.id) for r in self.snapshots]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "Only one full event snapshot per observation time is allowed"
            )
        settlement_keys = [
            (r.event_id, r.market_id, r.bookmaker_id, r.outcome_id)
            for r in self.settlements
        ]
        if len(settlement_keys) != len(set(settlement_keys)):
            raise ValueError("Duplicate/corrected settlement records are not supported")
        instruction_keys = [
            (r.execution_at, r.event_id, r.market_id, r.bookmaker_id, r.outcome_id)
            for r in self.execution_instructions
        ]
        if len(instruction_keys) != len(set(instruction_keys)):
            raise ValueError("Conflicting execution instructions")
        return self


class PaperBet(DomainModel):
    planned_quote: OddsQuote
    execution_quote: OddsQuote | None = None
    stake: Decimal
    accepted: bool = False
    rejection_reason: str | None = None
    disposition: BetDisposition = BetDisposition.OPEN
    payout: Decimal | None = None
    settlement_source: Literal["synthetic", "historical"] | None = None


class ExecutionStatus(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    FAILED = "failed"


class PaperTrade(DomainModel):
    id: Identifier
    event_id: Identifier
    market_id: Identifier
    decided_at: UTCTimestamp
    executed_at: UTCTimestamp
    event_start: UTCTimestamp
    theoretical_roi: Decimal
    execution_status: ExecutionStatus
    bets: tuple[PaperBet, ...]
    accepted_stake: Decimal
    expected_worst_case_payout: Decimal
    reasons: tuple[str, ...] = ()
    settled_at: UTCTimestamp | None = None
    realised_profit: Decimal | None = None


class ReplayObservation(DomainModel):
    observed_at: UTCTimestamp
    detection: ArbitrageResult
    new_candidate: bool
    new_eligible_opportunity: bool
    notes: tuple[str, ...] = ()


class BankrollPoint(DomainModel):
    timestamp: UTCTimestamp
    reason: Text
    cash: Decimal
    open_stake: Decimal
    equity_at_cost: Decimal


class BacktestMetrics(DomainModel):
    total_snapshots: int
    unique_events: int
    mathematical_candidates: int
    eligible_opportunities: int
    execution_attempts: int
    fully_executed: int
    partially_executed: int
    failed_executions: int
    settled_positions: int
    unsettled_positions: int
    average_theoretical_roi: Decimal | None
    average_realised_roi: Decimal | None
    total_stakes: Decimal
    realised_pnl: Decimal
    unsettled_exposure: Decimal
    ending_cash: Decimal
    ending_equity_at_cost: Decimal
    maximum_drawdown: Decimal
    largest_simulated_loss: Decimal


class BacktestReport(DomainModel):
    name: Text
    config: ReplayConfig
    metrics: BacktestMetrics
    observations: tuple[ReplayObservation, ...]
    trades: tuple[PaperTrade, ...]
    bankroll_history: tuple[BankrollPoint, ...]
    valuation_assumption: str = (
        "Open accepted stakes are carried at cost, with zero unrealised P&L. "
        "No payout or realised profit is booked until all accepted legs have "
        "compatible observed settlement evidence. "
        "Cash is not reserved for pending orders."
    )
