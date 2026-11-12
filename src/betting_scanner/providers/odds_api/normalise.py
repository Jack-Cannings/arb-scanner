"""Lossless archive timing and conservative provider-to-domain translation."""

import unicodedata
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from betting_scanner.backtesting.models import HistoricalSnapshot
from betting_scanner.core.models import (
    Bookmaker,
    Competition,
    DomainModel,
    Event,
    EventStatus,
    Market,
    MarketType,
    OddsQuote,
    OddsSnapshot,
    Outcome,
    OutcomeType,
    Participant,
    Sport,
)
from betting_scanner.providers.odds_api.schema import (
    ApiEvent,
    ApiOutcome,
    ResponseRecord,
    parse_page,
    validate_sport_key,
)


class NormalisationIssue(DomainModel):
    provider_event_id: str | None
    reason: str


class NormalisedPage(DomainModel):
    snapshots: tuple[HistoricalSnapshot, ...]
    issues: tuple[NormalisationIssue, ...]


def _id(kind: str, *parts: str) -> str:
    return f"{kind}-{uuid5(NAMESPACE_URL, repr(('the-odds-api', *parts)))}"


def _label(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def normalise(record: ResponseRecord, sport_key: str, region: str) -> NormalisedPage:
    validate_sport_key(sport_key)
    page = parse_page(record)
    snapshots: list[HistoricalSnapshot] = []
    issues: list[NormalisationIssue] = []
    seen: set[str] = set()
    for raw in page.data:
        reference = raw.get("id")
        event_id = reference if isinstance(reference, str) else None
        try:
            event = ApiEvent.model_validate(raw)
            if event.sport_key != sport_key or event.id in seen:
                raise ValueError("Mismatched competition or duplicate event")
            seen.add(event.id)
            snapshots.append(_event(record, event, region))
        except (ValueError, ValidationError):
            issues.append(
                NormalisationIssue(
                    provider_event_id=event_id,
                    reason="Malformed or ambiguous event; "
                    "no quotes from this event were normalised.",
                )
            )
    # Duplicate provider event IDs make the entire page ambiguous, not one winner.
    ids = [raw.get("id") for raw in page.data if isinstance(raw.get("id"), str)]
    if len(ids) != len(set(ids)):
        return NormalisedPage(
            snapshots=(),
            issues=(
                NormalisationIssue(
                    provider_event_id=None,
                    reason="Duplicate provider event IDs in one snapshot.",
                ),
            ),
        )
    return NormalisedPage(snapshots=tuple(snapshots), issues=tuple(issues))


def _event(record: ResponseRecord, event: ApiEvent, region: str) -> HistoricalSnapshot:
    page = parse_page(record)
    internal_id = _id("event", event.sport_key, event.id)
    labels = [event.home_team, event.away_team]
    if _label(labels[0]) == _label(labels[1]):
        raise ValueError("Ambiguous participants")
    participants = tuple(
        Participant(id=_id("participant", internal_id, _label(label)), name=label)
        for label in labels
    )
    lookup = {_label(p.name): p for p in participants}
    market_id = _id("market", internal_id, "h2h", *sorted(p.id for p in participants))
    outcomes = tuple(
        Outcome(
            id=_id("outcome", market_id, p.id),
            market_id=market_id,
            name=p.name,
            outcome_type=OutcomeType.PARTICIPANT,
            participant_id=p.id,
        )
        for p in participants
    )
    outcome_lookup = {outcome.participant_id: outcome.id for outcome in outcomes}
    books: list[Bookmaker] = []
    quotes: list[OddsQuote] = []
    seen_books: set[str] = set()
    for book in event.bookmakers:
        if book.key in seen_books:
            raise ValueError("Duplicate bookmaker")
        seen_books.add(book.key)
        bookmaker_id = _id("bookmaker", book.key)
        books.append(
            Bookmaker(
                id=bookmaker_id,
                name=book.title,
                provider_references={"the-odds-api": book.key},
            )
        )
        markets = [market for market in book.markets if market.key == "h2h"]
        if len(markets) > 1:
            raise ValueError("Ambiguous h2h markets")
        if not markets:
            continue
        market = markets[0]
        seen_outcomes: set[str] = set()
        for raw in market.outcomes:
            outcome = ApiOutcome.model_validate(raw)
            participant = lookup.get(_label(outcome.name))
            if (
                participant is None
                or participant.id in seen_outcomes
                or outcome.point is not None
                or outcome.description is not None
            ):
                raise ValueError("Unrecognised or duplicate h2h outcome")
            seen_outcomes.add(participant.id)
            quotes.append(
                OddsQuote(
                    provider_id="the-odds-api",
                    provider_event_reference=event.id,
                    event_id=internal_id,
                    bookmaker_id=bookmaker_id,
                    market_id=market_id,
                    outcome_id=outcome_lookup[participant.id],
                    decimal_odds=outcome.price,
                    provider_updated_at=market.last_update or book.last_update,
                    collected_at=record.collected_at,
                    observed_at=page.timestamp,
                    metadata={
                        "requested_snapshot": record.requested_at.isoformat()
                        if record.requested_at
                        else None,
                        "actual_snapshot": page.timestamp.isoformat(),
                        "downloaded_at": record.collected_at.isoformat(),
                        "bookmaker_last_update": book.last_update.isoformat()
                        if book.last_update
                        else None,
                        "market_last_update": market.last_update.isoformat()
                        if market.last_update
                        else None,
                        "requested_region": region,
                        "provider_market": "h2h",
                        "provider_bookmaker_key": book.key,
                        "provider_participant_label": outcome.name,
                    },
                )
            )
    competition_id = _id("competition", event.sport_key)
    snapshot = OddsSnapshot(
        sport=Sport(id="tennis", name="Tennis"),
        competition=Competition(
            id=competition_id, sport_id="tennis", name=event.sport_title
        ),
        event=Event(
            id=internal_id,
            sport_id="tennis",
            competition_id=competition_id,
            participants=participants,
            scheduled_start=event.commence_time,
            status=EventStatus.UNKNOWN,
            provider_references={"the-odds-api": event.id},
        ),
        bookmakers=tuple(books),
        markets=(
            Market(
                id=market_id,
                event_id=internal_id,
                market_type=MarketType.MATCH_WINNER,
                outcome_space_description="Provider h2h selections for listed "
                "participants; settlement unverified.",
                expected_outcome_ids=tuple(o.id for o in outcomes),
                outcomes=outcomes,
            ),
        ),
        quotes=tuple(quotes),
    )
    return HistoricalSnapshot(observed_at=page.timestamp, snapshot=snapshot)
