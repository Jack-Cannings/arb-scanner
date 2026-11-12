"""Provider-independent data representation; no pricing or eligibility logic."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
Text = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)
]


def normalise_utc(value: datetime) -> datetime:
    """Reject naive datetimes and preserve the instant in UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(UTC)


def _timestamp_input(value: object) -> object:
    if not isinstance(value, (str, datetime)):
        raise ValueError("Timestamp must be an aware datetime or ISO 8601 string")
    return value


UTCTimestamp = Annotated[
    AwareDatetime, BeforeValidator(_timestamp_input), AfterValidator(normalise_utc)
]


def _exact_odds_input(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Supply decimal odds as Decimal, a string or an integer")
    return value


DecimalOdds = Annotated[
    Decimal,
    BeforeValidator(_exact_odds_input),
    Field(gt=Decimal(1), allow_inf_nan=False),
]


class DomainModel(BaseModel):
    """Shared validation and Pydantic JSON round-trip methods."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Sport(DomainModel):
    id: Identifier
    name: Text


class Competition(DomainModel):
    id: Identifier
    sport_id: Identifier
    name: Text
    governing_body_or_category: Text | None = None


class Participant(DomainModel):
    id: Identifier
    name: Text


class EventStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    COMPLETED = "completed"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Event(DomainModel):
    id: Identifier
    sport_id: Identifier
    competition_id: Identifier
    participants: Annotated[tuple[Participant, ...], Field(min_length=1)]
    scheduled_start: UTCTimestamp
    status: EventStatus = EventStatus.UNKNOWN
    provider_references: dict[Identifier, Text] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_participants(self) -> Self:
        ids = [participant.id for participant in self.participants]
        if len(ids) != len(set(ids)):
            raise ValueError("Event participant identifiers must be unique")
        return self


class Bookmaker(DomainModel):
    id: Identifier
    name: Text
    region: Text | None = None
    provider_references: dict[Identifier, Text] = Field(default_factory=dict)


class MarketType(StrEnum):
    MATCH_WINNER = "match_winner"
    THREE_WAY_RESULT = "three_way_result"
    HANDICAP = "handicap"
    TOTAL = "total"
    SET_WINNER = "set_winner"
    PLAYER = "player"


class OutcomeType(StrEnum):
    PARTICIPANT = "participant"
    DRAW = "draw"
    OVER = "over"
    UNDER = "under"
    OTHER = "other"


class Outcome(DomainModel):
    id: Identifier
    market_id: Identifier
    name: Text
    outcome_type: OutcomeType
    participant_id: Identifier | None = None

    @model_validator(mode="after")
    def participant_reference(self) -> Self:
        if self.outcome_type == OutcomeType.PARTICIPANT and self.participant_id is None:
            raise ValueError("Participant outcomes require participant_id")
        if self.outcome_type == OutcomeType.DRAW and self.participant_id is not None:
            raise ValueError("Draw outcomes cannot reference a participant")
        return self


class OutcomeDefinitionStatus(StrEnum):
    COMPLETE = "complete"
    MISSING_EXPECTED_OUTCOMES = "missing_expected_outcomes"
    UNKNOWN = "unknown"


class Market(DomainModel):
    id: Identifier
    event_id: Identifier
    market_type: MarketType
    outcome_space_description: Text
    # None explicitly means the expected outcome definition is unknown.
    expected_outcome_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)] | None
    outcomes: tuple[Outcome, ...] = ()
    # Evidence supplied by normalisation/review, never inferred from labels.
    mutually_exclusive: bool = Field(default=False, strict=True)
    collectively_exhaustive: bool = Field(default=False, strict=True)
    period: Text | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_outcome_space(self) -> Self:
        ids = [outcome.id for outcome in self.outcomes]
        if len(ids) != len(set(ids)):
            raise ValueError("Market outcome identifiers must be unique")
        if any(outcome.market_id != self.id for outcome in self.outcomes):
            raise ValueError("Outcome market_id must reference its containing market")
        expected = self.expected_outcome_ids
        if expected is not None:
            if len(expected) != len(set(expected)):
                raise ValueError("Expected outcome identifiers must be unique")
            if not set(ids).issubset(expected):
                raise ValueError("Defined outcomes must belong to expected_outcome_ids")
        return self

    @property
    def definition_status(self) -> OutcomeDefinitionStatus:
        """Definition completeness only; says nothing about quoted coverage."""
        if self.expected_outcome_ids is None:
            return OutcomeDefinitionStatus.UNKNOWN
        if {outcome.id for outcome in self.outcomes} == set(self.expected_outcome_ids):
            return OutcomeDefinitionStatus.COMPLETE
        return OutcomeDefinitionStatus.MISSING_EXPECTED_OUTCOMES


class SettlementCategory(StrEnum):
    MATCH_COMPLETION_REQUIRED = "match_completion_required"
    MINIMUM_ONE_SET_COMPLETED = "minimum_one_set_completed"
    MATCH_STARTED = "match_started"
    OTHER_DOCUMENTED = "other_documented"
    UNKNOWN = "unknown"


class SettlementRule(DomainModel):
    """Evidence about a rule, not a compatibility decision or a full rules engine.

    Matching categories alone do not prove compatibility. Unknown/unverified
    information must never be treated as verified compatible rules.
    """

    category: SettlementCategory = SettlementCategory.UNKNOWN
    verified: bool = Field(default=False, strict=True)
    description: Text | None = None
    documentation: Text | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.verified and self.category == SettlementCategory.UNKNOWN:
            raise ValueError("Unknown settlement rules cannot be verified")
        if (self.verified or self.category == SettlementCategory.OTHER_DOCUMENTED) and (
            self.description is None or self.documentation is None
        ):
            raise ValueError(
                "Documented/verified rules require description and evidence"
            )
        return self


class OddsQuote(DomainModel):
    provider_id: Identifier
    provider_event_reference: Text
    event_id: Identifier
    bookmaker_id: Identifier
    market_id: Identifier
    outcome_id: Identifier
    decimal_odds: DecimalOdds
    provider_updated_at: UTCTimestamp | None = None
    collected_at: UTCTimestamp
    # Historical availability evidence is distinct from downloading an archive now.
    observed_at: UTCTimestamp | None = None
    settlement_rule: SettlementRule = Field(default_factory=SettlementRule)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class OddsSnapshot(DomainModel):
    """One event's normalised records with cross-reference integrity checks.

    Partial quote coverage is valid data. No freshness, settlement compatibility
    or arbitrage eligibility decisions are made here.
    """

    sport: Sport
    competition: Competition
    event: Event
    bookmakers: tuple[Bookmaker, ...]
    markets: tuple[Market, ...]
    quotes: tuple[OddsQuote, ...]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if (
            self.competition.sport_id != self.sport.id
            or self.event.sport_id != self.sport.id
            or self.event.competition_id != self.competition.id
        ):
            raise ValueError("Sport, competition and event references must agree")
        bookmaker_ids = {bookmaker.id for bookmaker in self.bookmakers}
        markets = {market.id: market for market in self.markets}
        if len(bookmaker_ids) != len(self.bookmakers) or len(markets) != len(
            self.markets
        ):
            raise ValueError("Bookmaker and market identifiers must be unique")
        participant_ids = {participant.id for participant in self.event.participants}
        for market in self.markets:
            if market.event_id != self.event.id:
                raise ValueError("Market must reference the snapshot event")
            for outcome in market.outcomes:
                if (
                    outcome.participant_id is not None
                    and outcome.participant_id not in participant_ids
                ):
                    raise ValueError("Outcome references an unknown participant")
        for quote in self.quotes:
            quote_market = markets.get(quote.market_id)
            if (
                quote.event_id != self.event.id
                or quote.bookmaker_id not in bookmaker_ids
            ):
                raise ValueError("Quote references an unknown event or bookmaker")
            if quote_market is None or quote.outcome_id not in {
                outcome.id for outcome in quote_market.outcomes
            }:
                raise ValueError("Quote references an unknown market or outcome")
            event_ref = self.event.provider_references.get(quote.provider_id)
            if event_ref is not None and event_ref != quote.provider_event_reference:
                raise ValueError("Quote provider event reference disagrees with event")
        return self
