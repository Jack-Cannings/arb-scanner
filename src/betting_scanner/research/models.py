"""Research records preserve evidence without claiming executable quotes."""

from datetime import date
from typing import Literal

from pydantic import Field

from betting_scanner.core.models import (
    DecimalOdds,
    DomainModel,
    Identifier,
    Text,
    UTCTimestamp,
)
from betting_scanner.strategies.arbitrage.results import PriceCalculation

Tour = Literal["atp", "wta"]


class SourceRow(DomainModel):
    source_file: str
    line: int
    tour: Tour
    raw: dict[str, str]


class ResearchMatch(SourceRow):
    match_id: Identifier
    tournament_id: Text
    tournament_name: Text
    tournament_date: date
    match_start: UTCTimestamp | None
    winner_id: Identifier
    winner_name: Text
    loser_id: Identifier
    loser_name: Text
    winner_odds: DecimalOdds
    loser_odds: DecimalOdds
    score: str
    bookmaker_count: int | None
    bookmaker_id: None = None
    odds_observed_at: None = None
    warnings: tuple[str, ...] = ()


class SkippedRow(SourceRow):
    reasons: tuple[str, ...]


class InputEvidence(DomainModel):
    source_file: str
    tour: Tour
    sha256: str
    columns: tuple[str, ...]
    rows: int
    provenance: str = "Unknown; user-supplied download"
    licence: str = "Unknown; establish source and redistribution rights before sharing"


class ImportResult(DomainModel):
    inputs: tuple[InputEvidence, ...]
    matches: tuple[ResearchMatch, ...]
    skipped: tuple[SkippedRow, ...]
    quality: dict[str, int] = Field(default_factory=dict)
    quality_examples: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class Candidate(DomainModel):
    match: ResearchMatch
    calculation: PriceCalculation
    verification: Literal["unverified"] = "unverified"
    simultaneous_prices_verified: Literal[False] = False
    settlement_rules_verified: Literal[False] = False
    executed: Literal[False] = False
    warnings: tuple[str, ...] = ()
