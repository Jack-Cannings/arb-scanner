"""Finite monitoring configuration, lifecycle records and session checkpoints."""

import math
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from betting_scanner.core.models import DomainModel, Identifier, UTCTimestamp
from betting_scanner.live.models import LiveScanPlan, LiveScanReport
from betting_scanner.providers.odds_api.client import BudgetState
from betting_scanner.strategies.arbitrage.results import (
    ArbitrageResult,
    VerificationStatus,
)

PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class MonitorPlan(DomainModel):
    scan: LiveScanPlan
    interval: PositiveSeconds = 300.0
    max_credits: Annotated[int, Field(ge=0, strict=True)] = 10
    max_scans: Annotated[int, Field(gt=0, strict=True)] | None = 10
    duration: PositiveSeconds | None = None

    @model_validator(mode="after")
    def finite_limits(self) -> Self:
        if self.max_scans is None and self.duration is None:
            raise ValueError("Monitoring needs a maximum scan count or duration")
        return self

    @property
    def scheduled_scans(self) -> int:
        duration_scans = (
            math.ceil(self.duration / self.interval) if self.duration else None
        )
        if self.max_scans is None:
            assert duration_scans is not None
            return duration_scans
        return (
            min(self.max_scans, duration_scans)
            if duration_scans is not None
            else self.max_scans
        )


class MonitorEstimate(DomainModel):
    scheduled_scans: int
    credits_per_scan_bound: int
    maximum_credits: int
    archive_cap: int
    archive_accounted: int
    archive_headroom: int
    provider_remaining: int | None
    session_cap: int
    affordable_scans: int
    schedule_offsets: tuple[float, ...]


class StopReason(StrEnum):
    MAX_SCANS = "maximum_scans_reached"
    DURATION = "duration_reached"
    SESSION_CREDITS = "session_credit_budget"
    ARCHIVE_CREDITS = "archive_credit_budget"
    API_QUOTA = "api_quota_exhausted"
    INTERRUPTION = "user_interruption"
    AUTHENTICATION = "authentication_failure"
    PROVIDER_ERROR = "unrecoverable_provider_error"


class CandidateTrack(DomainModel):
    id: Identifier
    sport_key: str
    event_id: Identifier
    market_id: Identifier
    combination: tuple[tuple[Identifier, Identifier, Identifier], ...]
    provider_event_reference: str
    first_observed_at: UTCTimestamp
    most_recent_observed_at: UTCTimestamp
    last_checked_at: UTCTimestamp
    status: Literal["observed", "no_longer_observed", "not_checked"]
    theoretical_roi: Decimal
    rounded_worst_case_profit: Decimal
    verification_status: VerificationStatus
    latest_result: ArbitrageResult


class CandidateArchive(DomainModel):
    format_version: Literal[1] = 1
    candidates: tuple[CandidateTrack, ...] = ()


class MonitoringScan(DomainModel):
    number: int
    started_at: UTCTimestamp
    finished_at: UTCTimestamp | None = None
    report: LiveScanReport | None = None
    new_candidates: tuple[Identifier, ...] = ()
    continuing_candidates: tuple[Identifier, ...] = ()
    no_longer_observed: tuple[Identifier, ...] = ()
    not_checked: tuple[Identifier, ...] = ()


class MonitoringReport(DomainModel):
    format_version: Literal[1] = 1
    id: Identifier
    plan: MonitorPlan
    started_at: UTCTimestamp
    finished_at: UTCTimestamp | None = None
    stop_reason: StopReason | None = None
    error: str | None = None
    scans: tuple[MonitoringScan, ...] = ()
    skipped_slots: int = 0
    event_observations: int = 0
    unique_events: tuple[Identifier, ...] = ()
    observed_candidates: tuple[Identifier, ...] = ()
    candidates: tuple[CandidateTrack, ...] = ()
    session_budget: BudgetState
    archive_budget: BudgetState
