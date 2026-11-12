import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from betting_scanner.core.models import (
    Bookmaker,
    Competition,
    Event,
    EventStatus,
    Market,
    OddsQuote,
    OddsSnapshot,
    Outcome,
    OutcomeDefinitionStatus,
    OutcomeType,
    Participant,
    SettlementCategory,
    SettlementRule,
    Sport,
    normalise_utc,
)

FIXTURES = Path(__file__).parent / "fixtures"
VALID_FIXTURES = sorted(path for path in FIXTURES.glob("*.json") if path.name[0] != "d")


def read_snapshot(name: str = "a_atp_candidate.json") -> OddsSnapshot:
    return OddsSnapshot.model_validate_json(
        (FIXTURES / name).read_text(encoding="utf-8")
    )


@pytest.fixture
def quote_data() -> dict[str, object]:
    return read_snapshot().quotes[0].model_dump()


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda path: path.stem)
def test_valid_fixture_round_trip(path: Path) -> None:
    snapshot = OddsSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    restored = OddsSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot
    assert restored.quotes[0].metadata == {
        "synthetic": True,
        "fixture": path.name[0],
        "source_note": "Fictional data only",
    }
    assert restored.event.scheduled_start.tzinfo == UTC
    assert all(isinstance(q.decimal_odds, Decimal) for q in restored.quotes)


def test_valid_model_construction() -> None:
    snapshot = read_snapshot()
    assert isinstance(snapshot.sport, Sport)
    assert isinstance(snapshot.competition, Competition)
    assert isinstance(snapshot.event, Event)
    assert isinstance(snapshot.event.participants[0], Participant)
    assert isinstance(snapshot.bookmakers[0], Bookmaker)
    assert isinstance(snapshot.markets[0], Market)
    assert isinstance(snapshot.markets[0].outcomes[0], Outcome)
    assert snapshot.event.status == EventStatus.SCHEDULED
    assert snapshot.markets[0].outcomes[0].outcome_type == OutcomeType.PARTICIPANT


@pytest.mark.parametrize(
    "identifier", ["", " ", "bad id", "bad/id", "-start", "x\n", "x" * 129, 4]
)
def test_invalid_identifiers(identifier: object) -> None:
    with pytest.raises(ValidationError):
        Sport.model_validate({"id": identifier, "name": "Example"})


@pytest.mark.parametrize("name", ["id", "sport_id", "competition_id"])
def test_invalid_event_reference_identifiers(name: str) -> None:
    data = read_snapshot().event.model_dump()
    data[name] = "invalid id"
    with pytest.raises(ValidationError):
        Event.model_validate(data)


@pytest.mark.parametrize(
    "name", ["provider_id", "event_id", "bookmaker_id", "market_id", "outcome_id"]
)
def test_invalid_quote_identifiers(name: str, quote_data: dict[str, object]) -> None:
    quote_data[name] = "bad id"
    with pytest.raises(ValidationError):
        OddsQuote.model_validate(quote_data)


@pytest.mark.parametrize("reference", ["", "   "])
def test_provider_event_reference_required(
    reference: str, quote_data: dict[str, object]
) -> None:
    quote_data["provider_event_reference"] = reference
    with pytest.raises(ValidationError):
        OddsQuote.model_validate(quote_data)


def test_deliberately_invalid_fixture() -> None:
    rows = json.loads((FIXTURES / "d_invalid_odds.json").read_text(encoding="utf-8"))
    assert len(rows) == 7
    for row in rows:
        with pytest.raises(ValidationError) as error:
            OddsQuote.model_validate(row)
        assert error.value.errors()[0]["loc"] == ("decimal_odds",)


@pytest.mark.parametrize(
    "odds",
    [
        0,
        1,
        -2,
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "oops",
        Decimal("NaN"),
        Decimal("Infinity"),
        float("nan"),
        float("inf"),
        2.1,
        True,
    ],
)
def test_invalid_odds(odds: object, quote_data: dict[str, object]) -> None:
    quote_data["decimal_odds"] = odds
    with pytest.raises(ValidationError):
        OddsQuote.model_validate(quote_data)


@pytest.mark.parametrize(
    "odds",
    ["2.10000000000000000000000000000100", "1.000000000000000000000000000001", "3.00"],
)
def test_decimal_precision(odds: str, quote_data: dict[str, object]) -> None:
    quote_data["decimal_odds"] = odds
    quote = OddsQuote.model_validate(quote_data)
    payload = quote.model_dump_json()
    assert json.loads(payload)["decimal_odds"] == odds
    restored = OddsQuote.model_validate_json(payload)
    assert restored.decimal_odds.as_tuple() == Decimal(odds).as_tuple()


@pytest.mark.parametrize("field", ["provider_updated_at", "collected_at"])
@pytest.mark.parametrize(
    "value", [datetime(2026, 6, 1), "2026-06-01T12:00:00", 1780315200]
)
def test_reject_naive_or_numeric_timestamps(
    field: str, value: object, quote_data: dict[str, object]
) -> None:
    quote_data[field] = value
    with pytest.raises(ValidationError):
        OddsQuote.model_validate(quote_data)


def test_start_timestamp_validation() -> None:
    data = read_snapshot().event.model_dump()
    data["scheduled_start"] = "2026-06-02T12:00:00"
    with pytest.raises(ValidationError):
        Event.model_validate(data)
    data["scheduled_start"] = "2026-06-02T14:00:00+02:00"
    event = Event.model_validate(data)
    assert event.scheduled_start == datetime(2026, 6, 2, 12, tzinfo=UTC)
    assert event.scheduled_start.tzinfo == UTC


def test_timestamp_normalisation(quote_data: dict[str, object]) -> None:
    quote_data["provider_updated_at"] = "2026-06-01T14:00:00+02:00"
    quote_data["collected_at"] = datetime(
        2026, 6, 1, 13, tzinfo=timezone(timedelta(hours=1))
    )
    quote = OddsQuote.model_validate(quote_data)
    assert quote.provider_updated_at == quote.collected_at
    assert quote.collected_at.tzinfo == UTC
    assert quote.provider_updated_at is not None
    assert quote.provider_updated_at.tzinfo == UTC
    assert json.loads(quote.model_dump_json())["collected_at"].endswith("Z")
    with pytest.raises(ValueError, match="timezone"):
        normalise_utc(datetime(2026, 6, 1))


def test_missing_provider_timestamp(quote_data: dict[str, object]) -> None:
    quote_data.pop("provider_updated_at")
    assert OddsQuote.model_validate(quote_data).provider_updated_at is None
    quote_data["provider_updated_at"] = None
    assert OddsQuote.model_validate(quote_data).provider_updated_at is None


def test_collection_timestamp_required(quote_data: dict[str, object]) -> None:
    quote_data.pop("collected_at")
    with pytest.raises(ValidationError):
        OddsQuote.model_validate(quote_data)


def test_stale_and_missing_updates_preserved() -> None:
    snapshot = read_snapshot("e_stale_quote.json")
    old, missing = snapshot.quotes[:2]
    assert old.provider_updated_at == datetime(2026, 5, 1, 12, tzinfo=UTC)
    assert old.collected_at == datetime(2026, 6, 1, 12, tzinfo=UTC)
    assert missing.provider_updated_at is None


def test_unknown_settlement_rules() -> None:
    snapshot = read_snapshot("g_unknown_settlement.json")
    for quote in snapshot.quotes:
        assert quote.settlement_rule.category == SettlementCategory.UNKNOWN
        assert quote.settlement_rule.verified is False
    with pytest.raises(ValidationError, match="cannot be verified"):
        SettlementRule(category=SettlementCategory.UNKNOWN, verified=True)


@pytest.mark.parametrize("category", list(SettlementCategory))
def test_settlement_evidence_requirements(category: SettlementCategory) -> None:
    with pytest.raises(ValidationError):
        SettlementRule(category=category, verified=True)
    if category != SettlementCategory.UNKNOWN:
        rule = SettlementRule(
            category=category,
            verified=True,
            description="Fictional exact rule",
            documentation="Fixture rulebook v1",
        )
        assert SettlementRule.model_validate_json(rule.model_dump_json()) == rule


def test_unverified_known_rule_is_explicit() -> None:
    rule = SettlementRule(category=SettlementCategory.MATCH_STARTED)
    assert rule.verified is False
    with pytest.raises(ValidationError):
        SettlementRule(category=SettlementCategory.OTHER_DOCUMENTED)


def test_incompatible_rules_remain_distinct() -> None:
    snapshot = read_snapshot("f_incompatible_settlement.json")
    rules = {q.bookmaker_id: q.settlement_rule for q in snapshot.quotes}
    assert rules["book-1"].category == SettlementCategory.MATCH_COMPLETION_REQUIRED
    assert rules["book-2"].category == SettlementCategory.MINIMUM_ONE_SET_COMPLETED
    assert rules["book-1"].description != rules["book-2"].description
    assert all(rule.verified for rule in rules.values())


def test_complete_two_outcome_definition() -> None:
    snapshot = read_snapshot()
    market = snapshot.markets[0]
    assert market.definition_status == OutcomeDefinitionStatus.COMPLETE
    assert len(market.expected_outcome_ids or ()) == 2
    assert all(q.settlement_rule.verified for q in snapshot.quotes)


def test_missing_outcome_definition() -> None:
    data = read_snapshot().markets[0].model_dump()
    data["outcomes"] = data["outcomes"][:1]
    market = Market.model_validate(data)
    assert market.definition_status == OutcomeDefinitionStatus.MISSING_EXPECTED_OUTCOMES
    assert Market.model_validate_json(market.model_dump_json()) == market


def test_unknown_outcome_definition_even_with_two_outcomes() -> None:
    data = read_snapshot().markets[0].model_dump()
    data["expected_outcome_ids"] = None
    market = Market.model_validate(data)
    assert len(market.outcomes) == 2
    assert market.definition_status == OutcomeDefinitionStatus.UNKNOWN


def test_missing_quotes_do_not_change_definition() -> None:
    snapshot = read_snapshot("c_missing_outcome.json")
    market = snapshot.markets[0]
    assert market.definition_status == OutcomeDefinitionStatus.COMPLETE
    expected = set(market.expected_outcome_ids or ())
    coverage = {q.outcome_id for q in snapshot.quotes if q.bookmaker_id == "book-3"}
    assert expected - coverage == {"c-win-b"}
    assert len(snapshot.quotes) == 5


def test_three_outcome_market() -> None:
    snapshot = read_snapshot("h_three_outcomes.json")
    market = snapshot.markets[0]
    assert snapshot.sport.id == "football"
    assert market.definition_status == OutcomeDefinitionStatus.COMPLETE
    assert len(market.outcomes) == 3
    assert len(market.expected_outcome_ids or ()) == 3
    draw = next(o for o in market.outcomes if o.outcome_type == OutcomeType.DRAW)
    assert draw.participant_id is None
    assert len(snapshot.quotes) == 9


def test_events_allow_more_than_two_participants() -> None:
    data = read_snapshot().event.model_dump(mode="json")
    data["participants"].append({"id": "third", "name": "Fictional third competitor"})
    assert len(Event.model_validate(data).participants) == 3


def test_provider_references_and_metadata_preserved() -> None:
    snapshot = read_snapshot()
    restored = OddsSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored.event.id == "event-a"
    assert restored.event.provider_references == {"synthetic": "opaque event/a"}
    assert restored.bookmakers[0].provider_references == {"synthetic": "opaque-book/1"}
    assert restored.quotes[0].provider_event_reference == "opaque event/a"
    assert restored.quotes[0].provider_id == "synthetic"


@pytest.mark.parametrize(
    "field",
    ["event_id", "bookmaker_id", "market_id", "outcome_id", "provider_event_reference"],
)
def test_snapshot_rejects_broken_quote_references(field: str) -> None:
    data = read_snapshot().model_dump()
    data["quotes"][0][field] = "unknown"
    with pytest.raises(ValidationError):
        OddsSnapshot.model_validate(data)


def test_outcome_identity_independent_of_labels_and_order() -> None:
    data = read_snapshot().markets[0].model_dump(mode="json")
    for outcome in data["outcomes"]:
        outcome["name"] = "Same display label"
    data["outcomes"].reverse()
    market = Market.model_validate(data)
    assert market.definition_status == OutcomeDefinitionStatus.COMPLETE
    assert {o.id for o in market.outcomes} == {"a-win-a", "a-win-b"}


@pytest.mark.parametrize(
    "change",
    ["duplicate", "wrong_market", "unexpected", "empty_expected", "duplicate_expected"],
)
def test_invalid_market_definitions(change: str) -> None:
    data = read_snapshot().markets[0].model_dump(mode="json")
    if change == "duplicate":
        data["outcomes"].append(data["outcomes"][0])
    elif change == "wrong_market":
        data["outcomes"][0]["market_id"] = "different"
    elif change == "unexpected":
        data["expected_outcome_ids"] = ["different"]
    elif change == "empty_expected":
        data["expected_outcome_ids"] = []
    else:
        data["expected_outcome_ids"] = ["a-win-a", "a-win-a"]
    with pytest.raises(ValidationError):
        Market.model_validate(data)


def test_empty_names_and_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Sport(id="tennis", name=" ")
    with pytest.raises(ValidationError):
        Sport.model_validate({"id": "tennis", "name": "Tennis", "typo": "value"})


def test_fixture_inventory() -> None:
    assert len(VALID_FIXTURES) == 7
    assert len(list(FIXTURES.glob("*.json"))) == 8


@pytest.mark.parametrize(
    "change",
    [
        "sport",
        "competition",
        "market_event",
        "participant",
        "duplicate_bookmaker",
        "duplicate_market",
    ],
)
def test_snapshot_rejects_inconsistent_records(change: str) -> None:
    data = read_snapshot().model_dump(mode="json")
    if change == "sport":
        data["competition"]["sport_id"] = "different"
    elif change == "competition":
        data["event"]["competition_id"] = "different"
    elif change == "market_event":
        data["markets"][0]["event_id"] = "different"
    elif change == "participant":
        data["markets"][0]["outcomes"][0]["participant_id"] = "different"
    elif change == "duplicate_bookmaker":
        data["bookmakers"].append(data["bookmakers"][0])
    else:
        data["markets"].append(data["markets"][0])
    with pytest.raises(ValidationError):
        OddsSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "participants", [[], [{"id": "same", "name": "A"}, {"id": "same", "name": "B"}]]
)
def test_invalid_participants(participants: list[dict[str, str]]) -> None:
    data = read_snapshot().event.model_dump()
    data["participants"] = participants
    with pytest.raises(ValidationError):
        Event.model_validate(data)


def test_participant_outcomes_require_reference() -> None:
    data = read_snapshot().markets[0].outcomes[0].model_dump()
    data["participant_id"] = None
    with pytest.raises(ValidationError):
        Outcome.model_validate(data)
    data["outcome_type"] = "draw"
    data["participant_id"] = "a-a"
    with pytest.raises(ValidationError):
        Outcome.model_validate(data)


def test_no_market_type_imposes_two_outcomes() -> None:
    data = read_snapshot("h_three_outcomes.json").markets[0].model_dump()
    data["market_type"] = "match_winner"
    assert (
        Market.model_validate(data).definition_status
        == OutcomeDefinitionStatus.COMPLETE
    )


def test_metadata_nested_json_round_trip(quote_data: dict[str, object]) -> None:
    quote_data["metadata"] = {
        "source": {"line": "0.50000000000000001", "tags": ["test", 2, None, True]}
    }
    quote = OddsQuote.model_validate(quote_data)
    assert (
        OddsQuote.model_validate_json(quote.model_dump_json()).metadata
        == quote.metadata
    )
