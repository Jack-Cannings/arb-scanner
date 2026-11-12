from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal, localcontext
from itertools import product
from pathlib import Path

import pytest
from pydantic import ValidationError

from betting_scanner.core.models import OddsQuote, OddsSnapshot
from betting_scanner.strategies.arbitrage import (
    ArbitrageResult,
    EngineConfig,
    calculate_arbitrage,
    detect_arbitrage,
)
from betting_scanner.strategies.arbitrage.results import (
    ActionabilityStatus,
    ReasonCode,
    VerificationStatus,
)

EVALUATED_AT = datetime(2026, 6, 1, 12, tzinfo=UTC)
PENNY = Decimal("0.01")
FIXTURES = Path(__file__).parent / "fixtures"


def fixture_snapshot(name: str = "a_atp_candidate.json") -> OddsSnapshot:
    return OddsSnapshot.model_validate_json(
        (FIXTURES / name).read_text(encoding="utf-8")
    )


def priced_snapshot(prices: tuple[str, ...] = ("2.20", "2.40")) -> OddsSnapshot:
    """Choose one fictional bookmaker per outcome from the existing fixtures."""
    name = "h_three_outcomes.json" if len(prices) == 3 else "a_atp_candidate.json"
    data = fixture_snapshot(name).model_dump(mode="json")
    quotes = []
    for index, (outcome_id, price) in enumerate(
        zip(data["markets"][0]["expected_outcome_ids"], prices, strict=True), 1
    ):
        quote = next(
            q
            for q in data["quotes"]
            if q["outcome_id"] == outcome_id and q["bookmaker_id"] == f"book-{index}"
        )
        quote["decimal_odds"] = price
        quotes.append(quote)
    data["quotes"] = quotes
    return OddsSnapshot.model_validate(data)


def evaluate(snapshot: OddsSnapshot, budget: str = "100") -> ArbitrageResult:
    return detect_arbitrage(
        snapshot,
        snapshot.markets[0].id,
        total_stake=Decimal(budget),
        evaluated_at=EVALUATED_AT,
    )


def codes(result: ArbitrageResult) -> set[ReasonCode]:
    return {reason.code for reason in result.reasons}


def test_worked_two_outcome_example() -> None:
    result = evaluate(priced_snapshot())
    assert result.actionability_status == ActionabilityStatus.ACTIONABLE
    assert result.verification_status == VerificationStatus.VERIFIED
    assert result.market_eligible
    calculation = result.eligible
    assert calculation is not None
    assert calculation.mathematical_arbitrage
    assert calculation.profitable_rounded_stakes
    assert abs(
        calculation.reciprocal_sum - Decimal("0.871212121212121212121212121212")
    ) < Decimal("1e-29")
    assert abs(
        calculation.theoretical_roi - Decimal("14.7826086956521739130434782609")
    ) < Decimal("1e-28")
    assert [leg.stake for leg in calculation.legs] == [
        Decimal("52.17"),
        Decimal("47.83"),
    ]
    assert [leg.raw_payout for leg in calculation.legs] == [
        Decimal("114.774"),
        Decimal("114.792"),
    ]
    assert [leg.payout for leg in calculation.legs] == [
        Decimal("114.77"),
        Decimal("114.79"),
    ]
    assert calculation.worst_case_profit == Decimal("14.77")
    assert calculation.worst_case_roi == Decimal("14.77")
    assert sum((leg.stake for leg in calculation.legs), Decimal(0)) == Decimal("100.00")
    assert all(leg.quote.provider_updated_at is not None for leg in calculation.legs)
    assert len({leg.quote.bookmaker_id for leg in calculation.legs}) == 2


@pytest.mark.parametrize("prices", [("1.80", "1.90"), ("2", "2"), ("3", "3", "3")])
def test_no_arbitrage_and_exact_break_even(prices: tuple[str, ...]) -> None:
    result = evaluate(priced_snapshot(prices))
    assert result.actionability_status == ActionabilityStatus.NO_ARBITRAGE
    assert ReasonCode.NO_ARBITRAGE in codes(result)
    assert result.eligible is not None
    assert not result.eligible.mathematical_arbitrage
    assert result.eligible.theoretical_profit <= 0
    assert result.eligible.worst_case_profit <= 0
    if len(set(prices)) == 1:
        assert result.eligible.reciprocal_sum == 1
        assert result.eligible.theoretical_roi == 0


def test_three_outcome_positive_arbitrage() -> None:
    result = evaluate(priced_snapshot(("3.50", "3.60", "3.70")))
    assert result.actionability_status == ActionabilityStatus.ACTIONABLE
    assert result.eligible is not None
    assert len(result.eligible.legs) == 3
    assert sum((leg.stake for leg in result.eligible.legs), Decimal(0)) == Decimal(
        "100"
    )
    assert result.eligible.worst_case_profit > 0
    assert len({leg.quote.outcome_id for leg in result.eligible.legs}) == 3


@pytest.mark.parametrize("budget", ["100", "10", "1", "0.01", "0.02", "100.000"])
def test_budget_and_exact_pennies(budget: str) -> None:
    result = evaluate(priced_snapshot(), budget)
    assert result.eligible is not None
    calculation = result.eligible
    assert sum((leg.stake for leg in calculation.legs), Decimal(0)) == Decimal(budget)
    assert all(leg.stake.as_tuple().exponent == -2 for leg in calculation.legs)
    assert calculation.worst_case_payout == min(leg.payout for leg in calculation.legs)
    assert calculation.worst_case_profit == calculation.worst_case_payout - Decimal(
        budget
    )
    assert (
        calculation.worst_case_roi
        == calculation.worst_case_profit / Decimal(budget) * 100
    )
    if Decimal(budget) >= 1:
        assert result.actionability_status == ActionabilityStatus.ACTIONABLE
    else:
        assert result.actionability_status == ActionabilityStatus.MATHEMATICAL_CANDIDATE
        assert ReasonCode.ROUNDING_REMOVES_PROFIT in codes(result)


def test_tiny_theoretical_margin_disappears_after_rounding() -> None:
    result = evaluate(priced_snapshot(("2.00001", "2.00001")), "1")
    assert result.eligible is not None
    assert result.eligible.theoretical_profit > 0
    assert result.eligible.worst_case_profit == 0
    assert not result.eligible.profitable_rounded_stakes
    assert result.actionability_status == ActionabilityStatus.MATHEMATICAL_CANDIDATE
    assert ReasonCode.ROUNDING_REMOVES_PROFIT in codes(result)


def test_budget_too_small_to_cover_all_outcomes() -> None:
    result = evaluate(priced_snapshot(), "0.01")
    assert ReasonCode.ZERO_STAKE in codes(result)
    assert result.eligible is not None
    assert result.eligible.worst_case_profit == Decimal("-0.01")


@pytest.mark.parametrize(
    "prices",
    [
        ("2.20", "2.40"),
        ("1.05", "27.1"),
        ("3.50", "3.60", "3.70"),
        ("1.01", "99", "101"),
    ],
)
@pytest.mark.parametrize("pennies", [1, 5, 13])
def test_penny_allocation_matches_exhaustive_optimum(
    prices: tuple[str, ...], pennies: int
) -> None:
    quotes = priced_snapshot(prices).quotes
    budget = Decimal(pennies) * PENNY
    result = calculate_arbitrage(quotes, budget)
    ordered_prices = [leg.quote.decimal_odds for leg in result.legs]
    best = Decimal("-1")
    for allocation in product(range(pennies + 1), repeat=len(prices) - 1):
        last = pennies - sum(allocation)
        if last < 0:
            continue
        stakes = (*allocation, last)
        payout = min(
            (Decimal(stake) * PENNY * price).quantize(PENNY, rounding=ROUND_FLOOR)
            for stake, price in zip(stakes, ordered_prices, strict=True)
        )
        best = max(best, payout)
    assert result.worst_case_payout == best


def test_budget_allocation_does_not_only_floor_ideal_stakes() -> None:
    result = calculate_arbitrage(
        priced_snapshot(("1.01", "206", "202")).quotes, Decimal("1.01")
    )
    assert all(leg.stake > 0 for leg in result.legs)
    assert result.worst_case_payout > 0


def test_missing_outcome_quotes_rejected() -> None:
    snapshot = fixture_snapshot("c_missing_outcome.json")
    data = snapshot.model_dump(mode="json")
    data["quotes"] = [q for q in data["quotes"] if q["outcome_id"] == "c-win-a"]
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.candidate is None
    assert result.eligible is None
    assert result.actionability_status == ActionabilityStatus.REJECTED
    assert ReasonCode.MISSING_OUTCOME_QUOTES in codes(result)


def test_one_partial_bookmaker_does_not_remove_other_coverage() -> None:
    result = evaluate(fixture_snapshot("c_missing_outcome.json"))
    assert result.actionability_status == ActionabilityStatus.ACTIONABLE
    assert result.eligible is not None
    assert len(result.eligible.legs) == 2


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("expected_outcome_ids", None, ReasonCode.UNKNOWN_OUTCOME_SPACE),
        ("mutually_exclusive", False, ReasonCode.UNVERIFIED_OUTCOME_SPACE),
        ("collectively_exhaustive", False, ReasonCode.UNVERIFIED_OUTCOME_SPACE),
        ("period", None, ReasonCode.UNKNOWN_MARKET_SCOPE),
        ("market_type", "handicap", ReasonCode.UNSUPPORTED_MARKET),
        ("parameters", {"line": "1.5"}, ReasonCode.UNSUPPORTED_MARKET),
    ],
)
def test_unverified_or_unsupported_markets_not_actionable(
    field: str, value: object, reason: ReasonCode
) -> None:
    data = priced_snapshot().model_dump(mode="json")
    data["markets"][0][field] = value
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.actionability_status != ActionabilityStatus.ACTIONABLE
    assert result.eligible is None
    assert reason in codes(result)


def test_incomplete_outcome_definition_rejected() -> None:
    data = priced_snapshot().model_dump(mode="json")
    missing_id = data["markets"][0]["outcomes"].pop()["id"]
    data["quotes"] = [q for q in data["quotes"] if q["outcome_id"] != missing_id]
    result = evaluate(OddsSnapshot.model_validate(data))
    assert ReasonCode.INCOMPLETE_OUTCOME_DEFINITION in codes(result)
    assert result.candidate is None


@pytest.mark.parametrize(
    "status", ["live", "completed", "postponed", "cancelled", "unknown"]
)
def test_only_scheduled_prematch_events_actionable(status: str) -> None:
    data = priced_snapshot().model_dump(mode="json")
    data["event"]["status"] = status
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.candidate is not None and result.candidate.mathematical_arbitrage
    assert result.eligible is None
    assert ReasonCode.EVENT_NOT_PREMATCH in codes(result)


def test_scheduled_event_at_start_time_not_prematch() -> None:
    data = priced_snapshot().model_dump(mode="json")
    data["event"]["scheduled_start"] = EVALUATED_AT.isoformat()
    assert ReasonCode.EVENT_NOT_PREMATCH in codes(
        evaluate(OddsSnapshot.model_validate(data))
    )


def test_stale_quote_excluded_and_not_replaced_by_collection_time() -> None:
    result = evaluate(fixture_snapshot("e_stale_quote.json"))
    assert ReasonCode.STALE_QUOTE in codes(result)
    assert ReasonCode.MISSING_UPDATE_TIME in codes(result)
    assert result.eligible is not None
    assert not any(leg.quote.bookmaker_id == "book-1" for leg in result.eligible.legs)


@pytest.mark.parametrize(
    "offset,accepted", [(-300, True), (-301, False), (5, True), (6, False)]
)
def test_freshness_and_skew_boundaries(offset: int, accepted: bool) -> None:
    data = priced_snapshot().model_dump(mode="json")
    for quote in data["quotes"]:
        quote["provider_updated_at"] = (
            EVALUATED_AT + timedelta(seconds=offset)
        ).isoformat()
    result = evaluate(OddsSnapshot.model_validate(data))
    assert (result.actionability_status == ActionabilityStatus.ACTIONABLE) is accepted
    if not accepted:
        assert (
            ReasonCode.STALE_QUOTE if offset < 0 else ReasonCode.FUTURE_QUOTE
        ) in codes(result)


def test_configurable_threshold_and_clock_skew() -> None:
    snapshot = priced_snapshot()
    result = detect_arbitrage(
        snapshot,
        snapshot.markets[0].id,
        total_stake=Decimal("100"),
        evaluated_at=EVALUATED_AT,
        config=EngineConfig(
            max_quote_age=timedelta(seconds=30), clock_skew_tolerance=timedelta(0)
        ),
    )
    assert result.candidate is None
    assert ReasonCode.STALE_QUOTE in codes(result)


def test_missing_provider_update_still_allows_mathematical_candidate() -> None:
    data = priced_snapshot().model_dump(mode="json")
    for quote in data["quotes"]:
        quote.pop("provider_updated_at")
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.candidate is not None and result.candidate.mathematical_arbitrage
    assert result.eligible is None
    assert result.verification_status == VerificationStatus.UNVERIFIED
    assert ReasonCode.MISSING_UPDATE_TIME in codes(result)


def test_future_collection_excluded() -> None:
    data = priced_snapshot().model_dump(mode="json")
    data["quotes"][0]["collected_at"] = "2026-06-01T12:00:06Z"
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.candidate is None
    assert ReasonCode.FUTURE_QUOTE in codes(result)


def test_unknown_settlement_is_only_a_candidate() -> None:
    result = evaluate(fixture_snapshot("g_unknown_settlement.json"))
    assert result.candidate is not None and result.candidate.mathematical_arbitrage
    assert result.actionability_status == ActionabilityStatus.MATHEMATICAL_CANDIDATE
    assert result.verification_status == VerificationStatus.UNVERIFIED
    assert result.eligible is None
    assert ReasonCode.UNVERIFIED_SETTLEMENT in codes(result)


def test_incompatible_settlement_cannot_be_combined() -> None:
    result = evaluate(fixture_snapshot("f_incompatible_settlement.json"))
    assert result.candidate is not None and result.candidate.mathematical_arbitrage
    assert result.actionability_status == ActionabilityStatus.MATHEMATICAL_CANDIDATE
    assert ReasonCode.INCOMPATIBLE_SETTLEMENT in codes(result)
    assert result.eligible is not None
    assert not result.eligible.mathematical_arbitrage
    assert (
        len(
            {
                leg.quote.settlement_rule.model_dump_json()
                for leg in result.eligible.legs
            }
        )
        == 1
    )


def test_same_category_does_not_prove_rule_compatibility() -> None:
    data = priced_snapshot().model_dump(mode="json")
    data["quotes"][1]["settlement_rule"]["description"] += " Different exact terms."
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.eligible is None
    assert result.candidate is not None and result.candidate.mathematical_arbitrage
    assert ReasonCode.INCOMPATIBLE_SETTLEMENT in codes(result)


def test_better_unverified_prices_do_not_hide_verified_selection() -> None:
    data = fixture_snapshot().model_dump(mode="json")
    extra = dict(data["quotes"][0])
    extra.update(decimal_odds="9.99", settlement_rule={"category": "unknown"})
    data["quotes"].append(extra)
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.actionability_status == ActionabilityStatus.ACTIONABLE
    assert result.candidate is not None and result.eligible is not None
    assert any(
        leg.quote.decimal_odds == Decimal("9.99") for leg in result.candidate.legs
    )
    assert all(leg.quote.decimal_odds < Decimal("9.99") for leg in result.eligible.legs)


def test_complete_compatible_group_is_used_instead_of_mixed_best_prices() -> None:
    data = fixture_snapshot().model_dump(mode="json")
    for quote in data["quotes"]:
        if quote["bookmaker_id"] == "book-2":
            quote["decimal_odds"] = "4.00"
            quote["settlement_rule"]["description"] += " Separate exact rule."
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.actionability_status == ActionabilityStatus.ACTIONABLE
    assert result.eligible is not None
    assert all(leg.quote.bookmaker_id == "book-2" for leg in result.eligible.legs)


def test_best_odds_can_come_from_same_bookmaker() -> None:
    data = priced_snapshot().model_dump(mode="json")
    for quote in data["quotes"]:
        quote["bookmaker_id"] = "book-1"
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.actionability_status == ActionabilityStatus.ACTIONABLE
    assert result.eligible is not None
    assert {leg.quote.bookmaker_id for leg in result.eligible.legs} == {"book-1"}


def test_deterministic_equal_prices_and_input_order() -> None:
    data = fixture_snapshot().model_dump(mode="json")
    for quote in data["quotes"]:
        quote["decimal_odds"] = "2.20"
    forward = evaluate(OddsSnapshot.model_validate(data))
    data["quotes"].reverse()
    data["bookmakers"].reverse()
    data["markets"][0]["outcomes"].reverse()
    data["markets"][0]["expected_outcome_ids"].reverse()
    backward = evaluate(OddsSnapshot.model_validate(data))
    assert forward == backward
    assert forward.eligible is not None
    assert all(leg.quote.bookmaker_id == "book-1" for leg in forward.eligible.legs)


def test_recurring_decimals_and_caller_context_independence() -> None:
    snapshot = priced_snapshot(("2.37", "2.73"))
    reference = evaluate(snapshot)
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        actual = evaluate(snapshot)
    assert actual == reference
    assert actual.eligible is not None
    assert all(isinstance(leg.stake, Decimal) for leg in actual.eligible.legs)


def test_high_precision_odds_do_not_lose_tiny_positive_margin() -> None:
    price = "2." + "0" * 90 + "1"
    result = evaluate(priced_snapshot((price, price)))
    assert result.candidate is not None
    assert result.candidate.mathematical_arbitrage
    assert result.candidate.theoretical_profit > 0
    assert result.actionability_status == ActionabilityStatus.MATHEMATICAL_CANDIDATE


def test_result_json_round_trip() -> None:
    result = evaluate(priced_snapshot())
    assert ArbitrageResult.model_validate_json(result.model_dump_json()) == result
    assert result.event_id == "event-a"
    assert result.market_id == "market-a"
    assert result.strategy_id == "cross_bookmaker_arbitrage"


@pytest.mark.parametrize(
    "budget",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("0.001"),
        Decimal("1.001"),
        1.0,
    ],
)
def test_invalid_budgets(budget: Decimal) -> None:
    with pytest.raises(ValueError):
        detect_arbitrage(
            priced_snapshot(), "market-a", total_stake=budget, evaluated_at=EVALUATED_AT
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_quote_age", timedelta(0)),
        ("max_quote_age", timedelta(seconds=-1)),
        ("clock_skew_tolerance", timedelta(seconds=-1)),
    ],
)
def test_invalid_config(field: str, value: timedelta) -> None:
    with pytest.raises(ValidationError):
        EngineConfig.model_validate({field: value})


def test_naive_evaluation_time_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        detect_arbitrage(
            priced_snapshot(),
            "market-a",
            total_stake=Decimal("100"),
            evaluated_at=datetime(2026, 6, 1, 12),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("decimal_odds", Decimal("0")),
        ("decimal_odds", Decimal("NaN")),
        ("outcome_id", ""),
        ("event_id", "wrong-event"),
        ("market_id", "wrong-market"),
    ],
)
def test_unchecked_invalid_quotes_produce_structured_rejection(
    field: str, value: object
) -> None:
    snapshot = priced_snapshot()
    invalid = snapshot.quotes[0].model_copy(update={field: value})
    snapshot = snapshot.model_copy(update={"quotes": (invalid, *snapshot.quotes[1:])})
    result = evaluate(snapshot)
    assert ReasonCode.INVALID_INPUT in codes(result)
    assert result.candidate is None and result.eligible is None


def test_unchecked_duplicate_outcome_definition_rejected() -> None:
    snapshot = priced_snapshot()
    market = snapshot.markets[0]
    invalid = market.model_copy(
        update={"outcomes": (*market.outcomes, market.outcomes[0])}
    )
    result = evaluate(snapshot.model_copy(update={"markets": (invalid,)}))
    assert ReasonCode.INVALID_INPUT in codes(result)


@pytest.mark.parametrize(
    "market_id,code",
    [("not-present", ReasonCode.UNKNOWN_MARKET), ("bad id", ReasonCode.INVALID_INPUT)],
)
def test_unknown_or_invalid_market_id(market_id: str, code: ReasonCode) -> None:
    result = detect_arbitrage(
        priced_snapshot(),
        market_id,
        total_stake=Decimal("100"),
        evaluated_at=EVALUATED_AT,
    )
    assert code in codes(result)
    assert result.candidate is None


def test_distinct_market_ids_never_combined_even_with_identical_labels() -> None:
    data = priced_snapshot().model_dump(mode="json")
    other = dict(data["markets"][0])
    other.update(
        id="other-market",
        period="first_set",
        expected_outcome_ids=["other-a", "other-b"],
    )
    other["outcomes"] = [
        dict(o, id=oid, market_id="other-market")
        for o, oid in zip(other["outcomes"], other["expected_outcome_ids"], strict=True)
    ]
    data["markets"].append(other)
    quote = dict(data["quotes"].pop())
    quote.update(market_id="other-market", outcome_id="other-b", decimal_odds="9.99")
    data["quotes"].append(quote)
    result = evaluate(OddsSnapshot.model_validate(data))
    assert result.candidate is None
    assert ReasonCode.MISSING_OUTCOME_QUOTES in codes(result)


def test_calculator_rejects_duplicate_outcomes_or_mixed_markets() -> None:
    quotes = priced_snapshot().quotes
    with pytest.raises(ValueError):
        calculate_arbitrage((quotes[0], quotes[0]), Decimal("100"))
    data = quotes[1].model_dump()
    data["market_id"] = "other"
    with pytest.raises(ValueError):
        calculate_arbitrage((quotes[0], OddsQuote.model_validate(data)), Decimal("100"))


@pytest.mark.parametrize(
    "name",
    sorted(
        path.name for path in FIXTURES.glob("*.json") if not path.name.startswith("d_")
    ),
)
def test_all_valid_prompt_two_fixtures_evaluate_and_serialise(name: str) -> None:
    result = evaluate(fixture_snapshot(name))
    assert ArbitrageResult.model_validate_json(result.model_dump_json()) == result


def test_wta_fixture_has_no_arbitrage() -> None:
    assert (
        evaluate(fixture_snapshot("b_wta_no_arbitrage.json")).actionability_status
        == ActionabilityStatus.NO_ARBITRAGE
    )
