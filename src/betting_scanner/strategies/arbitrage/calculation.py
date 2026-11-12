"""Pure Decimal mathematics and deterministic optimal penny stake allocation."""

from collections.abc import Mapping
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, localcontext
from math import prod

from pydantic import TypeAdapter

from betting_scanner.core.models import DecimalOdds, Identifier, OddsQuote
from betting_scanner.strategies.arbitrage.results import (
    ArbitrageCalculation,
    PriceCalculation,
    PriceStakeLeg,
    StakeLeg,
)

PENNY = Decimal("0.01")
ONE = Decimal(1)
ZERO = Decimal(0)


def validate_budget(total_stake: Decimal) -> Decimal:
    """Accept a finite positive GBP amount representable in whole pennies."""
    if not isinstance(total_stake, Decimal) or not total_stake.is_finite():
        raise ValueError("Total stake must be a finite Decimal GBP amount")
    if total_stake <= 0:
        raise ValueError("Total stake must be strictly positive")
    # Inspect digits rather than quantising under the caller's Decimal context.
    _, digits, exponent = total_stake.as_tuple()
    assert isinstance(exponent, int)
    if exponent < -2 and any(digits[exponent + 2 :]):
        raise ValueError("Total stake must be a whole number of pennies")
    return total_stake


def _precision(values: tuple[Decimal, ...]) -> int:
    # Enough for exact finite products/sums and guard digits for division.
    return max(
        60, sum(len(v.as_tuple().digits) + abs(v.adjusted()) for v in values) + 30
    )


def _penny_stakes(
    odds: tuple[Decimal, ...],
    budget: Decimal,
    ideal: tuple[Decimal, ...],
    payout: Decimal,
) -> tuple[Decimal, ...]:
    """Maximise the minimum payout in whole pennies using monotone feasibility.

    A target payout of p pennies requires ceil(p / odds_i) stake pennies on
    each outcome. Binary search finds the largest target affordable by budget.
    This also handles tiny budgets where flooring all ideal stakes is suboptimal.
    """
    budget_pennies = budget / PENNY
    lower = ZERO
    upper = (payout / PENNY).to_integral_value(rounding=ROUND_FLOOR) + ONE
    while upper - lower > ONE:
        middle = ((lower + upper) / 2).to_integral_value(rounding=ROUND_FLOOR)
        required = sum(
            (
                (middle / price).to_integral_value(rounding=ROUND_CEILING)
                for price in odds
            ),
            ZERO,
        )
        if required <= budget_pennies:
            lower = middle
        else:
            upper = middle
    pennies = [
        (lower / price).to_integral_value(rounding=ROUND_CEILING) for price in odds
    ]
    # At most N-1 pennies remain: raising the target by one penny increases
    # each required stake by at most one penny because every price is > 1.
    remainder = int(budget_pennies - sum(pennies, ZERO))
    for _ in range(remainder):
        index = max(range(len(odds)), key=lambda i: (ideal[i] / PENNY - pennies[i], -i))
        pennies[index] += ONE
    return tuple((amount * PENNY).quantize(PENNY) for amount in pennies)


def calculate_arbitrage(
    quotes: tuple[OddsQuote, ...], total_stake: Decimal
) -> ArbitrageCalculation:
    """Calculate a hypothetical combination, without claiming market eligibility.

    Outcomes must be unique and refer to one canonical event/market. This pure
    function deliberately makes no claims about completeness, rules or freshness.
    """
    validate_budget(total_stake)
    quotes = tuple(OddsQuote.model_validate(q.model_dump()) for q in quotes)
    if len(quotes) < 2 or len({q.outcome_id for q in quotes}) != len(quotes):
        raise ValueError("Select at least two distinct outcome identifiers")
    if len({(q.event_id, q.market_id) for q in quotes}) != 1:
        raise ValueError("Selected quotes must share an event and canonical market")
    quotes = tuple(sorted(quotes, key=lambda q: q.outcome_id))
    numeric = calculate_prices(
        {q.outcome_id: q.decimal_odds for q in quotes}, total_stake
    )
    return ArbitrageCalculation.model_validate(
        {
            **numeric.model_dump(exclude={"legs"}),
            "legs": [
                StakeLeg(
                    quote=quote,
                    **leg.model_dump(exclude={"outcome_id", "decimal_odds"}),
                )
                for quote, leg in zip(quotes, numeric.legs, strict=True)
            ],
        }
    )


def calculate_prices(
    prices: Mapping[str, Decimal], total_stake: Decimal
) -> PriceCalculation:
    """Shared pure maths; prices alone do not establish executable opportunities."""
    validate_budget(total_stake)
    validated = TypeAdapter(dict[Identifier, DecimalOdds]).validate_python(dict(prices))
    if len(validated) < 2:
        raise ValueError("Select at least two distinct outcome identifiers")
    ordered = sorted(validated.items())
    odds = tuple(price for _, price in ordered)
    with localcontext(Context(prec=_precision((*odds, total_stake)))):
        denominator = prod(odds, start=ONE)
        numerators = tuple(
            prod(odds[:i] + odds[i + 1 :], start=ONE) for i in range(len(odds))
        )
        numerator = sum(numerators, ZERO)
        # Cross-products classify the sign exactly, including 3/3/3 break-even.
        reciprocal_sum = numerator / denominator
        theoretical_payout = total_stake * denominator / numerator
        theoretical_profit = total_stake * (denominator - numerator) / numerator
        theoretical_roi = (denominator - numerator) / numerator * 100
        ideal = tuple(total_stake * value / numerator for value in numerators)
        stakes = _penny_stakes(odds, total_stake, ideal, theoretical_payout)
        legs = tuple(
            PriceStakeLeg(
                outcome_id=outcome_id,
                decimal_odds=price,
                ideal_stake=ideal_stake,
                stake=stake,
                raw_payout=stake * price,
                payout=(stake * price).quantize(PENNY, rounding=ROUND_FLOOR),
            )
            for (outcome_id, price), ideal_stake, stake in zip(
                ordered, ideal, stakes, strict=True
            )
        )
        worst_payout = min(leg.payout for leg in legs)
        return PriceCalculation(
            reciprocal_sum=reciprocal_sum,
            mathematical_arbitrage=numerator < denominator,
            total_stake=total_stake.quantize(PENNY),
            theoretical_payout=theoretical_payout,
            theoretical_profit=theoretical_profit,
            theoretical_roi=theoretical_roi,
            legs=legs,
            worst_case_payout=worst_payout,
            worst_case_profit=worst_payout - total_stake,
            worst_case_roi=(worst_payout - total_stake) / total_stake * 100,
        )
