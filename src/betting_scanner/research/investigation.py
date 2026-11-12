"""Descriptive source-data investigation without cleaning or executing candidates."""

import hashlib
from collections import Counter, defaultdict
from decimal import Context, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from pathlib import Path

from betting_scanner.research.analysis import ResearchReport, analyse
from betting_scanner.research.importer import import_directory
from betting_scanner.research.independent import (
    decimal_value,
    independent_values,
    verify,
)
from betting_scanner.research.investigation_models import (
    Anomaly,
    BandCount,
    Distribution,
    Investigation,
    RowAudit,
    SkippedAudit,
    Subset,
)
from betting_scanner.research.models import Candidate, ResearchMatch, SkippedRow

BANDS = ("0-1%", "1-2%", "2-5%", "5-10%", "10-20%", "20-50%", ">50%")
PRICE_COLUMNS = ("winner_best_close_odds", "loser_best_close_odds")
FAIR_COLUMNS = ("winner_close_fair_odds", "loser_close_fair_odds")


def roi_band(roi: Decimal) -> str:
    if not roi.is_finite() or roi <= 0:
        raise ValueError("ROI bands require a finite positive candidate ROI")
    for upper, band in zip((1, 2, 5, 10, 20), BANDS[:5], strict=True):
        if roi < upper:
            return band
    return "20-50%" if roi <= 50 else ">50%"


def percentile(values: tuple[Decimal, ...], percentage: int) -> Decimal | None:
    """Linear interpolation at (n-1)*p/100 (inclusive/type-7 quantiles)."""
    if not 0 <= percentage <= 100 or any(not v.is_finite() for v in values):
        raise ValueError("Finite values and a percentile in [0,100] are required")
    if not values:
        return None
    ordered = sorted(Fraction(value) for value in values)
    position = Fraction((len(ordered) - 1) * percentage, 100)
    lower = position.__floor__()
    upper = min(lower + 1, len(ordered) - 1)
    return decimal_value(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    )


def distribution(candidates: tuple[Candidate, ...]) -> Distribution:
    values = tuple(c.calculation.theoretical_roi for c in candidates)
    mean = (
        decimal_value(sum((Fraction(v) for v in values), Fraction(0)) / len(values))
        if values
        else None
    )
    return Distribution(
        count=len(values),
        minimum=min(values) if values else None,
        median=percentile(values, 50),
        mean=mean,
        p75=percentile(values, 75),
        p90=percentile(values, 90),
        p95=percentile(values, 95),
        p99=percentile(values, 99),
        maximum=max(values) if values else None,
    )


def number(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def audit_skipped(row: SkippedRow) -> SkippedAudit:
    missing = tuple(k for k in PRICE_COLUMNS if not row.raw.get(k, "").strip())
    invalid = tuple(
        k
        for k in PRICE_COLUMNS
        if row.raw.get(k, "").strip() and ((v := number(row.raw[k])) is None or v <= 1)
    )
    classification = (
        "missing_prices"
        if missing
        else "invalid_prices"
        if invalid
        else "duplicate"
        if any("duplicate" in r for r in row.reasons)
        else "other_rejection"
    )
    alternate = {k: row.raw.get(k, "") for k in FAIR_COLUMNS}
    usable = all(
        (v := number(value)) is not None and v > 1 for value in alternate.values()
    )
    return SkippedAudit(
        **row.model_dump(exclude={"reasons"}),
        classification=classification,
        original_reasons=row.reasons,
        missing_price_columns=missing,
        invalid_price_columns=invalid,
        alternate_odds_values=alternate,
        alternate_pair_finite_gt_one=usable,
    )


def audit_candidate(candidate: Candidate) -> RowAudit:
    m, calc = candidate.match, candidate.calculation
    ratios: list[Decimal | None] = []
    issues: list[str] = []
    for side, price in (("winner", m.winner_odds), ("loser", m.loser_odds)):
        fair = number(m.raw.get(f"{side}_close_fair_odds", ""))
        ratio = (
            decimal_value(Fraction(price) / Fraction(fair))
            if fair and fair > 1
            else None
        )
        ratios.append(ratio)
        if ratio is not None and ratio >= 2:
            issues.append(f"{side}_best_at_least_twice_fair_review_mapping_or_timing")
    if min(m.winner_odds, m.loser_odds) > 2:
        issues.append("both_best_prices_above_2")
    if calc.theoretical_roi > 50:
        issues.append("roi_over_50_percent")
    probability = number(m.raw.get("winner_close_prob", ""))
    if (
        probability is not None
        and 0 < probability < 1
        and (
            (probability > Decimal("0.5") and m.winner_odds > m.loser_odds)
            or (probability < Decimal("0.5") and m.winner_odds < m.loser_odds)
        )
    ):
        issues.append("best_price_favourite_order_differs_from_close_probability")
    if max(m.winner_odds, m.loser_odds) >= 100:
        issues.append("unusual_price_at_least_100")
    s, _ = independent_values(m)
    return RowAudit(
        **m.model_dump(include={"source_file", "line", "tour", "raw"}),
        match_id=m.match_id,
        tournament=m.tournament_name,
        match_start=m.raw.get("start_time_utc", ""),
        tournament_date=m.raw["tourney_date"],
        winner_name=m.winner_name,
        loser_name=m.loser_name,
        winner_odds=m.raw[PRICE_COLUMNS[0]],
        loser_odds=m.raw[PRICE_COLUMNS[1]],
        reciprocal_sum=decimal_value(s),
        theoretical_roi=calc.theoretical_roi,
        best_implied_overround_pct=decimal_value((s - 1) * 100),
        reported_margin_pct=m.raw.get("margin_pct", ""),
        winner_best_to_fair_ratio=ratios[0],
        loser_best_to_fair_ratio=ratios[1],
        suspected_issues=tuple(issues),
    )


def _reference(match: ResearchMatch) -> tuple[str, int]:
    return match.source_file, match.line


def ranked(candidates: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    stable = sorted(candidates, key=lambda c: (c.match.source_file, c.match.line))
    return tuple(
        sorted(stable, key=lambda c: c.calculation.theoretical_roi, reverse=True)
    )


def _odds_range(match: ResearchMatch) -> str:
    price = max(match.winner_odds, match.loser_odds)
    for limit in (2, 5, 10, 20, 50):
        if price < limit:
            return f"max_price_below_{limit}"
    return "max_price_at_least_50"


def subsets(report: ResearchReport) -> tuple[Subset, ...]:
    groups: dict[tuple[str, str], list[ResearchMatch]] = defaultdict(list)
    candidates = {_reference(c.match): c for c in report.candidates}
    for m in report.imported.matches:
        date = m.match_start.date().isoformat() if m.match_start else "unknown"
        keys = {
            "tour": m.tour,
            "tournament": f"{m.tour}:{m.tournament_id}:{m.tournament_name}",
            "month": f"{m.tour}:{date[:7]}",
            "date": f"{m.tour}:{date}",
            "odds_range": f"{m.tour}:{_odds_range(m)}",
            "bookmaker_count": f"{m.tour}:{m.bookmaker_count}",
        }
        for scope, key in keys.items():
            groups[(scope, key)].append(m)
    result: list[Subset] = []
    for (scope, key), matches in sorted(groups.items()):
        found = [
            candidates[_reference(m)] for m in matches if _reference(m) in candidates
        ]
        result.append(
            Subset(
                scope=scope,
                key=key,
                valid_matches=len(matches),
                candidates=len(found),
                candidate_percentage=decimal_value(
                    Fraction(100 * len(found), len(matches))
                ),
                extreme_over_50=sum(c.calculation.theoretical_roi > 50 for c in found),
            )
        )
    return tuple(result)


SCHEMA_FINDINGS = {
    "best_odds": (
        "Confirmed column labels: winner_best_close_odds and "
        "loser_best_close_odds; closing designation is source-labelled only."
    ),
    "fair_odds": (
        "winner_close_fair_odds and loser_close_fair_odds are distinct from "
        "best odds; method, de-vig procedure and underlying prices are "
        "undocumented."
    ),
    "probabilities": (
        "winner_open_prob and winner_close_prob are labelled opening/closing "
        "probabilities, not opening decimal quotes."
    ),
    "aggregation": (
        "Numeric bookmakers counts and best labels suggest aggregation; "
        "independent per-player selection and collection method cannot be "
        "confirmed."
    ),
    "identities": (
        "No bookmaker identity columns in the supplied 61-column files; "
        "identities cannot be recovered from counts."
    ),
    "time": (
        "start_time_utc is match start; tourney_date is tournament date. "
        "Neither is a quote timestamp. No quote observation timestamp column "
        "exists."
    ),
    "markets": (
        "No explicit price market IDs or settlement rules; identical match- "
        "winner market coverage cannot be established."
    ),
    "margin": (
        "margin_pct is retained, but its definition is unknown; it must not "
        "be assumed to equal overround of independently selected best prices."
    ),
    "documentation": (
        "Only supplied local sidecars are inventoried; no external provenance "
        "or collection methodology is inferred or fetched."
    ),
    "causality": (
        "Wrong-player mapping, entry errors, mixed markets, different times "
        "and independent best-price selection remain hypotheses; outliers "
        "alone prove none of them."
    ),
}
METHODS = (
    (
        "All unfiltered Prompt 8 mathematical candidates retained; production "
        "maths unchanged."
    ),
    (
        "Percentiles use sorted ROI with linear interpolation at (n-1)*p/100; "
        "empty statistics are null."
    ),
    (
        "ROI bands are [0,1), [1,2), [2,5), [5,10), [10,20), [20,50], "
        "(50,infinity); only positive candidate ROI enters bands. Percentages "
        "use candidates within each scope."
    ),
    "Top 30 use ROI descending then filename and line ascending to break ties.",
    (
        "Independent checks use exact Fraction from original price strings; "
        "production values agree within 45 significant digits. All valid raw "
        "conversions and candidate signs checked."
    ),
    (
        "Sample: ten lowest positive ROI candidates below 10% (normal), ten "
        "highest ROI candidates, ten non-candidates by filename/line. Missing "
        "sample members are not invented."
    ),
    (
        "GBP100 sample allocations are independently verified by exhaustive "
        "enumeration of all 10001 penny splits, exact payout flooring and "
        "ideal allocation comparison."
    ),
    (
        "Suspicions: ROI>50, both prices>2, best/fair ratio>=2, favourite- "
        "order disagreement, or price>=100. These review heuristics "
        "are not confirmed errors and do not filter candidates."
    ),
    (
        "Tournament groups retain tour, source tournament ID and name; "
        "month/date groups use match start UTC, not tournament or quote date. "
        "Rates use valid matches. Small groups are descriptive, not "
        "statistically significant findings."
    ),
    (
        "Confirmed anomaly CSV records missing or invalid selected prices "
        "only. Blank prices are confirmed missingness, not proven corruption. "
        "Invalid prices are field-domain errors, not evidence of their cause."
    ),
)


def investigate(directory: Path, baseline: Path | None = None) -> Investigation:
    imported = import_directory(directory)
    report = analyse(imported)
    ordered = ranked(report.candidates)
    audits = tuple(audit_candidate(c) for c in ordered)
    skipped = tuple(audit_skipped(row) for row in imported.skipped)
    anomalies: list[Anomaly] = []
    for row in skipped:
        for category, columns in (
            ("confirmed_missing_price", row.missing_price_columns),
            ("confirmed_invalid_price", row.invalid_price_columns),
        ):
            if columns:
                anomalies.append(
                    Anomaly.model_validate(
                        {
                            **row.model_dump(
                                include={"source_file", "line", "tour", "raw"}
                            ),
                            "category": category,
                            "details": [f"{k}={row.raw.get(k, '')!r}" for k in columns],
                        }
                    )
                )
    candidate_refs = {_reference(c.match) for c in ordered}
    selections = (
        (
            "normal",
            tuple(
                c.match for c in reversed(ordered) if c.calculation.theoretical_roi < 10
            )[:10],
        ),
        ("high", tuple(c.match for c in ordered[:10])),
        (
            "non_candidate",
            tuple(
                sorted(
                    (
                        m
                        for m in imported.matches
                        if _reference(m) not in candidate_refs
                    ),
                    key=_reference,
                )
            )[:10],
        ),
    )
    checks = tuple(verify(m, group) for group, matches in selections for m in matches)
    discrepancies = 0
    for m in imported.matches:
        s, _ = independent_values(m)
        if (
            (s < 1) != (_reference(m) in candidate_refs)
            or Fraction(m.raw[PRICE_COLUMNS[0]]) != Fraction(m.winner_odds)
            or Fraction(m.raw[PRICE_COLUMNS[1]]) != Fraction(m.loser_odds)
        ):
            discrepancies += 1
    distributions: dict[str, Distribution] = {}
    bands: list[BandCount] = []
    for scope in ("all", "atp", "wta"):
        selected = tuple(c for c in ordered if scope == "all" or c.match.tour == scope)
        distributions[scope] = distribution(selected)
        counts = Counter(roi_band(c.calculation.theoretical_roi) for c in selected)
        for band in BANDS:
            bands.append(
                BandCount(
                    scope=scope,
                    band=band,
                    count=counts[band],
                    percentage_of_candidates=decimal_value(
                        Fraction(100 * counts[band], len(selected))
                    )
                    if selected
                    else Decimal(0),
                )
            )
    comparison, digest = (
        "No baseline supplied; existing Prompt 8 reports untouched",
        None,
    )
    if baseline is not None:
        content = baseline.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        previous = ResearchReport.model_validate_json(content)
        same = (
            previous.imported.inputs == report.imported.inputs
            and previous.candidates == report.candidates
            and previous.total_rows == report.total_rows
        )
        comparison = (
            "Exact unfiltered candidate/input match to supplied Prompt 8 baseline"
            if same
            else "Baseline differs; inspect inputs, stake or filters; "
            "no baseline overwritten"
        )
    with localcontext(Context(prec=80)):
        percentage = (
            Decimal(100 * len(ordered)) / len(imported.matches)
            if imported.matches
            else Decimal(0)
        )
    internal: Counter[str] = Counter()
    internal["winner_probability_vs_inverse_fair_gap_over_0.001"] = 0
    internal["fair_reciprocal_sum_gap_over_0.001"] = 0
    for m in imported.matches:
        probability = number(m.raw.get("winner_close_prob", ""))
        fair_a = number(m.raw.get(FAIR_COLUMNS[0], ""))
        fair_b = number(m.raw.get(FAIR_COLUMNS[1], ""))
        if probability is not None and fair_a and fair_a > 1:
            internal["winner_probability_fair_pairs_checked"] += 1
            if abs(Fraction(probability) - 1 / Fraction(fair_a)) > Fraction(1, 1000):
                internal["winner_probability_vs_inverse_fair_gap_over_0.001"] += 1
        if fair_a and fair_b and fair_a > 1 and fair_b > 1:
            internal["fair_pairs_checked"] += 1
            if abs(1 / Fraction(fair_a) + 1 / Fraction(fair_b) - 1) > Fraction(1, 1000):
                internal["fair_reciprocal_sum_gap_over_0.001"] += 1
        if _reference(m) in candidate_refs:
            margin = number(m.raw.get("margin_pct", ""))
            if margin is not None and margin > 0:
                internal[
                    "candidates_positive_reported_margin_negative_best_overround"
                ] += 1
    return Investigation(
        inputs=imported.inputs,
        total_rows=report.total_rows,
        valid_matches=report.valid_matches,
        candidate_count=len(ordered),
        candidate_percentage=percentage,
        distributions=distributions,
        bands=tuple(bands),
        subsets=subsets(report),
        top_candidates=audits[:30],
        suspected_case_count=sum(bool(a.suspected_issues) for a in audits),
        suspected_issue_counts=dict(
            Counter(issue for a in audits for issue in a.suspected_issues)
        ),
        confirmed_anomalies=tuple(anomalies),
        skipped_audits=skipped,
        skipped_classifications=dict(Counter(a.classification for a in skipped)),
        arithmetic_checks=checks,
        all_valid_rows_sign_and_decimal_checks=len(imported.matches),
        sign_or_decimal_discrepancies=discrepancies,
        source_quality=imported.quality,
        internal_checks=dict(internal),
        schema_findings=SCHEMA_FINDINGS,
        baseline_comparison=comparison,
        baseline_sha256=digest,
        documentation_files=tuple(
            str(p.relative_to(directory))
            for p in sorted(directory.rglob("*"))
            if p.is_file() and p.suffix.lower() in {".md", ".txt", ".rst", ".json"}
        ),
        methods=METHODS,
    )
