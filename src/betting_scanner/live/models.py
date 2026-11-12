"""Typed live scan plans, observations and durable archive metadata."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from betting_scanner.backtesting.models import Money
from betting_scanner.core.models import DomainModel, Identifier, UTCTimestamp
from betting_scanner.providers.odds_api.client import BudgetState
from betting_scanner.providers.odds_api.normalise import NormalisedPage
from betting_scanner.providers.odds_api.schema import ApiSport, validate_sport_key
from betting_scanner.strategies.arbitrage.results import ArbitrageResult, EngineConfig

SportKey = Annotated[str, AfterValidator(validate_sport_key)]


class LiveScanPlan(DomainModel):
    sport_keys: Annotated[tuple[SportKey, ...], Field(min_length=1, max_length=10)]
    stake: Money = Decimal("100.00")
    engine: EngineConfig = Field(default_factory=EngineConfig)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        object.__setattr__(self, "sport_keys", tuple(sorted(set(self.sport_keys))))
        return self

    def estimate(self, max_retries: int = 1) -> int:
        return len(self.sport_keys) * (max_retries + 1)


class LiveObservation(DomainModel):
    id: Identifier
    sport_key: SportKey
    observed_at: UTCTimestamp
    normalised: NormalisedPage
    analyses: tuple[ArbitrageResult, ...]
    excluded_started: tuple[Identifier, ...]
    plan: LiveScanPlan


class LiveRequest(DomainModel):
    id: Identifier
    sport_key: SportKey
    plan: LiveScanPlan
    status: Literal[
        "in_flight", "complete", "empty", "duplicate", "error", "uncertain"
    ] = "in_flight"
    observation_id: Identifier | None = None
    reason: str | None = None


class ObservationIndex(DomainModel):
    observation_id: Identifier
    body_digest: str
    observed_at: UTCTimestamp


class LiveManifest(DomainModel):
    format_version: Literal[1] = 1
    mode: Literal["live"] = "live"
    provider: Literal["the-odds-api"] = "the-odds-api"
    region: Literal["uk"] = "uk"
    market: Literal["h2h"] = "h2h"
    created_at: UTCTimestamp
    updated_at: UTCTimestamp
    budget: BudgetState
    requests: tuple[LiveRequest, ...] = ()
    observations: dict[str, ObservationIndex] = Field(default_factory=dict)
    conflicted_observations: tuple[str, ...] = ()
    number_of_events: int = 0
    number_of_quotes: int = 0
    replayable_event_observations: int = 0


class LiveDiscovery(DomainModel):
    checked_at: UTCTimestamp
    sports: tuple[ApiSport, ...]


class LiveScanReport(DomainModel):
    plan: LiveScanPlan
    observations: tuple[LiveObservation, ...]
    errors: tuple[str, ...]
    run_budget: BudgetState
    archive_budget: BudgetState
    events_analysed: int
    unique_bookmakers: int
    quotes_processed: int
    mathematical_candidates: int
    eligible_opportunities: int
    unverified_candidates: int
    rejected_markets: int
    stop_code: str | None = None
