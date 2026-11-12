"""Deterministic pre-match selection, verification and detection pipeline."""

import json
from datetime import datetime
from decimal import Decimal

from pydantic import TypeAdapter, ValidationError

from betting_scanner.core.models import (
    EventStatus,
    Identifier,
    Market,
    MarketType,
    OddsQuote,
    OddsSnapshot,
    OutcomeDefinitionStatus,
    SettlementCategory,
    normalise_utc,
)
from betting_scanner.strategies.arbitrage.calculation import (
    calculate_arbitrage,
    validate_budget,
)
from betting_scanner.strategies.arbitrage.results import (
    ActionabilityStatus,
    ArbitrageCalculation,
    ArbitrageResult,
    DetectionIssue,
    EngineConfig,
    ReasonCode,
    VerificationStatus,
)


def _quote_identity(quote: OddsQuote) -> str:
    return json.dumps(quote.model_dump(mode="json"), sort_keys=True)


def _select(
    quotes: list[OddsQuote], expected: tuple[str, ...]
) -> tuple[OddsQuote, ...] | None:
    selected: list[OddsQuote] = []
    for outcome_id in sorted(expected):
        available = [q for q in quotes if q.outcome_id == outcome_id]
        if not available:
            return None
        # Stable bookmaker/provider IDs, then full canonical quote, break ties.
        selected.append(
            min(
                available,
                key=lambda q: (
                    q.decimal_odds.copy_negate(),
                    q.bookmaker_id,
                    q.provider_id,
                    _quote_identity(q),
                ),
            )
        )
    return tuple(selected)


def _issue(
    code: ReasonCode, message: str, quote: OddsQuote | None = None
) -> DetectionIssue:
    return DetectionIssue(
        code=code,
        message=message,
        bookmaker_id=quote.bookmaker_id if quote else None,
        outcome_id=quote.outcome_id if quote else None,
        provider_id=quote.provider_id if quote else None,
    )


def _market_checks(
    snapshot: OddsSnapshot, market: Market, evaluated_at: datetime
) -> list[DetectionIssue]:
    reasons: list[DetectionIssue] = []
    if not market.mutually_exclusive or not market.collectively_exhaustive:
        reasons.append(
            _issue(
                ReasonCode.UNVERIFIED_OUTCOME_SPACE,
                "Mutual exclusivity and exhaustiveness must be explicitly verified.",
            )
        )
    if market.period is None:
        reasons.append(
            _issue(
                ReasonCode.UNKNOWN_MARKET_SCOPE,
                "A canonical market period must be explicitly supplied.",
            )
        )
    if (
        market.market_type not in {MarketType.MATCH_WINNER, MarketType.THREE_WAY_RESULT}
        or market.parameters
    ):
        reasons.append(
            _issue(
                ReasonCode.UNSUPPORTED_MARKET,
                "This engine supports unparameterised winner/result markets only.",
            )
        )
    if (
        snapshot.event.status != EventStatus.SCHEDULED
        or snapshot.event.scheduled_start <= evaluated_at
    ):
        reasons.append(
            _issue(
                ReasonCode.EVENT_NOT_PREMATCH,
                "The event must be scheduled and start after evaluation time.",
            )
        )
    return reasons


def detect_arbitrage(
    snapshot: OddsSnapshot,
    market_id: str,
    *,
    total_stake: Decimal,
    evaluated_at: datetime,
    config: EngineConfig | None = None,
) -> ArbitrageResult:
    """Evaluate one canonical market using an injected clock and GBP budget.

    Invalid normalised data returns a structured rejection. Invalid caller
    configuration (budget, evaluation time, thresholds) raises a validation error.
    `actionable` means eligible under these modelled hypothetical checks only;
    no executable offer, bookmaker acceptance or realised profit is guaranteed.
    """
    validate_budget(total_stake)
    evaluated_at = normalise_utc(evaluated_at)
    config = EngineConfig.model_validate((config or EngineConfig()).model_dump())
    try:
        # Revalidate nested dictionaries too, including unchecked model_copy updates.
        snapshot = OddsSnapshot.model_validate(snapshot.model_dump())
        market_id = TypeAdapter(Identifier).validate_python(market_id)
    except ValidationError:
        return ArbitrageResult(
            event_id=None,
            market_id=None,
            evaluated_at=evaluated_at,
            total_stake=total_stake,
            reasons=(
                _issue(
                    ReasonCode.INVALID_INPUT,
                    "Normalised records or identifiers failed validation.",
                ),
            ),
        )

    base = ArbitrageResult(
        event_id=snapshot.event.id,
        market_id=market_id,
        evaluated_at=evaluated_at,
        total_stake=total_stake,
    )
    market = next((m for m in snapshot.markets if m.id == market_id), None)
    if market is None:
        return base.model_copy(
            update={
                "reasons": (
                    _issue(
                        ReasonCode.UNKNOWN_MARKET, "Market ID is not in this snapshot."
                    ),
                )
            }
        )
    if (
        market.definition_status != OutcomeDefinitionStatus.COMPLETE
        or len(market.outcomes) < 2
    ):
        code = (
            ReasonCode.UNKNOWN_OUTCOME_SPACE
            if market.expected_outcome_ids is None
            else ReasonCode.INCOMPLETE_OUTCOME_DEFINITION
        )
        return base.model_copy(
            update={
                "reasons": (
                    _issue(
                        code,
                        "A complete, explicit definition with at least two "
                        "outcomes is required.",
                    ),
                )
            }
        )
    assert market.expected_outcome_ids is not None
    expected = market.expected_outcome_ids
    blockers = _market_checks(snapshot, market, evaluated_at)
    reasons = list(blockers)
    candidate_quotes: list[OddsQuote] = []
    verified_groups: dict[str, list[OddsQuote]] = {}
    for quote in sorted(snapshot.quotes, key=_quote_identity):
        if quote.market_id != market_id:
            continue
        updated = quote.provider_updated_at
        if quote.collected_at > evaluated_at + config.clock_skew_tolerance:
            reasons.append(
                _issue(
                    ReasonCode.FUTURE_QUOTE,
                    "Collection time exceeds allowed clock skew.",
                    quote,
                )
            )
            continue
        if updated is not None and updated > evaluated_at + config.clock_skew_tolerance:
            reasons.append(
                _issue(
                    ReasonCode.FUTURE_QUOTE,
                    "Provider update exceeds allowed clock skew.",
                    quote,
                )
            )
            continue
        if updated is not None and evaluated_at - updated > config.max_quote_age:
            reasons.append(
                _issue(
                    ReasonCode.STALE_QUOTE,
                    "Provider update is older than the freshness threshold.",
                    quote,
                )
            )
            continue
        candidate_quotes.append(quote)
        if updated is None:
            reasons.append(
                _issue(
                    ReasonCode.MISSING_UPDATE_TIME,
                    "Missing provider update time cannot be replaced "
                    "with collection time.",
                    quote,
                )
            )
        rule = quote.settlement_rule
        verified_rule = rule.verified and rule.category != SettlementCategory.UNKNOWN
        if not verified_rule:
            reasons.append(
                _issue(
                    ReasonCode.UNVERIFIED_SETTLEMENT,
                    "Settlement rules are unknown or unverified.",
                    quote,
                )
            )
        if updated is not None and verified_rule:
            # Conservative contract: all declared rule details AND evidence must
            # match. Category equality alone never establishes compatibility.
            key = rule.model_dump_json()
            verified_groups.setdefault(key, []).append(quote)
    if len(verified_groups) > 1:
        reasons.append(
            _issue(
                ReasonCode.INCOMPATIBLE_SETTLEMENT,
                "Distinct verified rule contracts were evaluated separately, "
                "never combined.",
            )
        )
    candidate_selection = _select(candidate_quotes, expected)
    if candidate_selection is None:
        reasons.append(
            _issue(
                ReasonCode.MISSING_OUTCOME_QUOTES,
                "Not all expected outcomes have a current usable quote.",
            )
        )
        return base.model_copy(update={"reasons": tuple(reasons)})
    candidate = calculate_arbitrage(candidate_selection, total_stake)
    eligible_calculations: list[ArbitrageCalculation] = []
    if not blockers:
        for key in sorted(verified_groups):
            selection = _select(verified_groups[key], expected)
            if selection is not None:
                eligible_calculations.append(
                    calculate_arbitrage(selection, total_stake)
                )
    eligible = max(
        eligible_calculations,
        key=lambda c: (c.worst_case_profit, c.theoretical_profit),
        default=None,
    )
    if eligible is None:
        reasons.append(
            _issue(
                ReasonCode.NO_VERIFIED_COVERAGE,
                "No verified compatible group covers the whole market "
                "under the required checks.",
            )
        )
    elif not eligible.mathematical_arbitrage:
        reasons.append(
            _issue(
                ReasonCode.NO_ELIGIBLE_ARBITRAGE,
                "The best verified compatible combination is not "
                "mathematically profitable.",
            )
        )
    elif not eligible.profitable_rounded_stakes:
        reasons.append(
            _issue(
                ReasonCode.ROUNDING_REMOVES_PROFIT,
                "Penny allocation and conservative penny payouts leave "
                "no strictly positive worst-case profit.",
            )
        )
        if any(leg.stake == 0 for leg in eligible.legs):
            reasons.append(
                _issue(
                    ReasonCode.ZERO_STAKE,
                    "The budget cannot fund a positive stake on every outcome.",
                )
            )
    if not candidate.mathematical_arbitrage:
        reasons.append(
            _issue(
                ReasonCode.NO_ARBITRAGE,
                "The reciprocal odds sum is not strictly below one.",
            )
        )
    if (
        eligible is not None
        and eligible.mathematical_arbitrage
        and eligible.profitable_rounded_stakes
    ):
        actionability = ActionabilityStatus.ACTIONABLE
    elif candidate.mathematical_arbitrage:
        actionability = ActionabilityStatus.MATHEMATICAL_CANDIDATE
    else:
        actionability = ActionabilityStatus.NO_ARBITRAGE
    return ArbitrageResult(
        event_id=snapshot.event.id,
        market_id=market_id,
        evaluated_at=evaluated_at,
        total_stake=total_stake,
        candidate=candidate,
        eligible=eligible,
        market_eligible=eligible is not None,
        verification_status=(
            VerificationStatus.VERIFIED
            if eligible is not None
            else VerificationStatus.UNVERIFIED
        ),
        actionability_status=actionability,
        reasons=tuple(sorted(reasons, key=lambda reason: reason.model_dump_json())),
    )
