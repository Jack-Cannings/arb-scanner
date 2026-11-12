"""Typed configuration and hypothetical arbitrage results."""

from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field

from betting_scanner.core.models import (
    DomainModel,
    Identifier,
    OddsQuote,
    Text,
    UTCTimestamp,
)


class EngineConfig(DomainModel):
    max_quote_age: Annotated[timedelta, Field(gt=timedelta(0))] = timedelta(minutes=5)
    clock_skew_tolerance: Annotated[timedelta, Field(ge=timedelta(0))] = timedelta(
        seconds=5
    )


class ReasonCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNKNOWN_MARKET = "unknown_market"
    UNKNOWN_OUTCOME_SPACE = "unknown_outcome_space"
    INCOMPLETE_OUTCOME_DEFINITION = "incomplete_outcome_definition"
    UNVERIFIED_OUTCOME_SPACE = "unverified_outcome_space"
    UNKNOWN_MARKET_SCOPE = "unknown_market_scope"
    UNSUPPORTED_MARKET = "unsupported_market"
    EVENT_NOT_PREMATCH = "event_not_prematch"
    STALE_QUOTE = "stale_quote"
    FUTURE_QUOTE = "future_quote"
    MISSING_UPDATE_TIME = "missing_update_time"
    UNVERIFIED_SETTLEMENT = "unverified_settlement"
    INCOMPATIBLE_SETTLEMENT = "incompatible_settlement"
    MISSING_OUTCOME_QUOTES = "missing_outcome_quotes"
    NO_VERIFIED_COVERAGE = "no_verified_coverage"
    NO_ARBITRAGE = "no_arbitrage"
    NO_ELIGIBLE_ARBITRAGE = "no_eligible_arbitrage"
    ROUNDING_REMOVES_PROFIT = "rounding_removes_profit"
    ZERO_STAKE = "zero_stake"


class DetectionIssue(DomainModel):
    code: ReasonCode
    message: Text
    bookmaker_id: Identifier | None = None
    outcome_id: Identifier | None = None
    provider_id: Identifier | None = None


class StakeLeg(DomainModel):
    quote: OddsQuote
    ideal_stake: Decimal
    stake: Decimal
    # Raw decimal multiplication and conservative penny payout are both retained.
    raw_payout: Decimal
    payout: Decimal


class ArbitrageCalculation(DomainModel):
    reciprocal_sum: Decimal
    mathematical_arbitrage: bool
    total_stake: Decimal
    theoretical_payout: Decimal
    theoretical_profit: Decimal
    theoretical_roi: Decimal
    legs: tuple[StakeLeg, ...]
    worst_case_payout: Decimal
    worst_case_profit: Decimal
    worst_case_roi: Decimal

    @property
    def profitable_rounded_stakes(self) -> bool:
        return self.worst_case_profit > 0 and all(leg.stake > 0 for leg in self.legs)


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


class ActionabilityStatus(StrEnum):
    ACTIONABLE = "actionable"
    MATHEMATICAL_CANDIDATE = "mathematical_candidate"
    NO_ARBITRAGE = "no_arbitrage"
    REJECTED = "rejected"


class ArbitrageResult(DomainModel):
    event_id: Identifier | None
    market_id: Identifier | None
    strategy_id: Identifier = "cross_bookmaker_arbitrage"
    evaluated_at: UTCTimestamp
    total_stake: Decimal
    # The exploratory best prices can include unverified rules/update times.
    candidate: ArbitrageCalculation | None = None
    # Only verified, fresh, mutually compatible quotes enter this calculation.
    eligible: ArbitrageCalculation | None = None
    market_eligible: bool = False
    verification_status: VerificationStatus = VerificationStatus.REJECTED
    actionability_status: ActionabilityStatus = ActionabilityStatus.REJECTED
    reasons: tuple[DetectionIssue, ...] = ()
