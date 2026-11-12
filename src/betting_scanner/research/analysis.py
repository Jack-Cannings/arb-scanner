"""Unverified closing-price algebra, deliberately separate from replay execution."""

from decimal import Context, Decimal, localcontext

from betting_scanner.core.models import DomainModel
from betting_scanner.research.models import Candidate, ImportResult
from betting_scanner.strategies.arbitrage.calculation import (
    calculate_prices,
    validate_budget,
)

LIMITATIONS = (
    (
        "Research-only mathematical candidates; no verified simultaneous "
        "bookmaker opportunities or executed trades."
    ),
    (
        "Best closing odds are labelled by the CSV, not independently verified "
        "quotations; opening probabilities and fair odds are not used."
    ),
    (
        "Bookmakers is a count, not bookmaker identities. Quote observation "
        "times, provenance, price simultaneity and settlement compatibility are "
        "unknown."
    ),
    (
        "Match start and tournament date are not odds observation timestamps. "
        "Missing observation evidence cannot establish freshness or "
        "executability."
    ),
    (
        "Winner/loser labels and scores contain future result information. This "
        "symmetric algebra is not a predictive backtest; results do not "
        "establish execution."
    ),
    (
        "No acceptance, liquidity, limits, commission, delays, retirement rules "
        "or price availability evidence is supplied."
    ),
    (
        "Summed hypothetical profit is a non-realistic conditional upper bound "
        "assuming every selected candidate was executable at these prices, not "
        "realised profit or a guaranteed bound on trading returns."
    ),
    (
        "Source and licence are unknown. Obtain attribution and redistribution "
        "permission from the original source before sharing datasets."
    ),
    (
        "Unusual prices are flagged and retained. Identity/name variants are "
        "findings, not evidence of equivalent people or tournaments."
    ),
)


class ResearchReport(DomainModel):
    imported: ImportResult
    stake: Decimal
    minimum_roi: Decimal
    total_rows: int
    valid_matches: int
    skipped_rows: int
    atp_matches: int
    wta_matches: int
    mathematical_candidates_before_roi_filter: int
    candidates: tuple[Candidate, ...]
    candidate_percentage: Decimal
    average_theoretical_roi: Decimal
    highest_theoretical_roi: Decimal
    non_realistic_conditional_upper_bound_profit: Decimal
    limitations: tuple[str, ...] = LIMITATIONS


def analyse(
    imported: ImportResult,
    stake: Decimal = Decimal("100"),
    minimum_roi: Decimal = Decimal(0),
) -> ResearchReport:
    validate_budget(stake)
    if not minimum_roi.is_finite() or minimum_roi < 0:
        raise ValueError("Minimum ROI must be finite and nonnegative")
    candidates: list[Candidate] = []
    mathematical = 0
    for match in imported.matches:
        calculation = calculate_prices(
            {match.winner_id: match.winner_odds, match.loser_id: match.loser_odds},
            stake,
        )
        if calculation.mathematical_arbitrage:
            mathematical += 1
            if calculation.theoretical_roi >= minimum_roi:
                candidates.append(
                    Candidate(
                        match=match,
                        calculation=calculation,
                        warnings=(
                            ("theoretical_roi_at_least_10_percent_review_source",)
                            if calculation.theoretical_roi >= 10
                            else ()
                        ),
                    )
                )
    # Aggregate under a private context so callers cannot truncate report metrics.
    with localcontext(Context(prec=80)):
        roi_sum = sum((c.calculation.theoretical_roi for c in candidates), Decimal(0))
        average = roi_sum / len(candidates) if candidates else Decimal(0)
        percentage = (
            Decimal(len(candidates)) / len(imported.matches) * 100
            if imported.matches
            else Decimal(0)
        )
        profit = sum((c.calculation.theoretical_profit for c in candidates), Decimal(0))
    return ResearchReport(
        imported=imported,
        stake=stake,
        minimum_roi=minimum_roi,
        total_rows=sum(source.rows for source in imported.inputs),
        valid_matches=len(imported.matches),
        skipped_rows=len(imported.skipped),
        atp_matches=sum(m.tour == "atp" for m in imported.matches),
        wta_matches=sum(m.tour == "wta" for m in imported.matches),
        mathematical_candidates_before_roi_filter=mathematical,
        candidates=tuple(candidates),
        candidate_percentage=percentage,
        average_theoretical_roi=average,
        highest_theoretical_roi=max(
            (c.calculation.theoretical_roi for c in candidates), default=Decimal(0)
        ),
        non_realistic_conditional_upper_bound_profit=profit,
    )
