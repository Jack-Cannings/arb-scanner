"""Exact rational validation, separate from production cross-product maths."""

from decimal import Context, Decimal, localcontext
from fractions import Fraction

from betting_scanner.research.investigation_models import ArithmeticCheck
from betting_scanner.research.models import ResearchMatch
from betting_scanner.strategies.arbitrage.calculation import calculate_prices


def decimal_value(value: Fraction) -> Decimal:
    with localcontext(Context(prec=80)):
        return Decimal(value.numerator) / Decimal(value.denominator)


def independent_values(match: ResearchMatch) -> tuple[Fraction, Fraction]:
    a = Fraction(match.raw["winner_best_close_odds"].strip())
    b = Fraction(match.raw["loser_best_close_odds"].strip())
    s = 1 / a + 1 / b
    return s, (1 / s - 1) * 100


def close(actual: Decimal, expected: Fraction) -> bool:
    """Production divisions use >=60 digits; compare at 45 significant digits."""
    return abs(Fraction(actual) - expected) <= max(Fraction(1), abs(expected)) / 10**45


def verify(match: ResearchMatch, group: str) -> ArithmeticCheck:
    """Exhaustively check all 10,001 two-leg GBP100 penny allocations."""
    budget = Decimal(100)
    production = calculate_prices(
        {match.winner_id: match.winner_odds, match.loser_id: match.loser_odds}, budget
    )
    s, roi = independent_values(match)
    prices = {
        match.winner_id: Fraction(match.raw["winner_best_close_odds"]),
        match.loser_id: Fraction(match.raw["loser_best_close_odds"]),
    }
    a, b = prices[match.winner_id], prices[match.loser_id]
    # Payout pennies = floor(stake pennies * decimal price). No Decimal rounding,
    # no production stake allocator, and no binary search reused here.
    optimum = max(
        min((i * a).__floor__(), ((10000 - i) * b).__floor__()) for i in range(10001)
    )
    independent_payout = Decimal(optimum) / 100
    ideal_match = all(
        close(leg.ideal_stake, Fraction(100) / (prices[leg.outcome_id] * s))
        for leg in production.legs
    )
    spends_budget = (
        sum((Fraction(leg.stake) for leg in production.legs), Fraction(0)) == 100
    )
    floors_match = all(
        Fraction(leg.payout)
        == Fraction(
            (Fraction(leg.stake) * prices[leg.outcome_id] * 100).__floor__(), 100
        )
        for leg in production.legs
    )
    legal_stakes = all(
        Fraction(leg.stake) >= 0
        and (Fraction(leg.stake) * 100).denominator == 1
        and Fraction(leg.raw_payout) == Fraction(leg.stake) * prices[leg.outcome_id]
        for leg in production.legs
    )
    optimal = (
        spends_budget
        and floors_match
        and legal_stakes
        and production.worst_case_payout == min(leg.payout for leg in production.legs)
        and production.worst_case_payout == independent_payout
    )
    matches = (
        close(production.reciprocal_sum, s)
        and close(production.theoretical_roi, roi)
        and production.mathematical_arbitrage == (s < 1)
        and close(production.theoretical_payout, 100 / s)
        and close(production.theoretical_profit, 100 / s - 100)
        and production.worst_case_profit == independent_payout - 100
        and production.worst_case_roi == independent_payout - 100
        and production.total_stake == 100
    )
    return ArithmeticCheck(
        group=group,
        source_file=match.source_file,
        line=match.line,
        match_id=match.match_id,
        independent_s=decimal_value(s),
        production_s=production.reciprocal_sum,
        independent_roi=decimal_value(roi),
        production_roi=production.theoretical_roi,
        independent_worst_payout=independent_payout,
        production_worst_payout=production.worst_case_payout,
        ideal_stakes_match=ideal_match,
        penny_allocation_optimal=optimal,
        arithmetic_matches=matches,
    )
