"""Traceable investigation evidence; suspicion is not a confirmed source error."""

from decimal import Decimal
from typing import Literal

from betting_scanner.core.models import DomainModel
from betting_scanner.research.models import InputEvidence, SourceRow


class Distribution(DomainModel):
    count: int
    minimum: Decimal | None
    median: Decimal | None
    mean: Decimal | None
    p75: Decimal | None
    p90: Decimal | None
    p95: Decimal | None
    p99: Decimal | None
    maximum: Decimal | None


class BandCount(DomainModel):
    scope: str
    band: str
    count: int
    percentage_of_candidates: Decimal


class Subset(DomainModel):
    scope: str
    key: str
    valid_matches: int
    candidates: int
    candidate_percentage: Decimal
    extreme_over_50: int


class RowAudit(SourceRow):
    match_id: str
    tournament: str
    match_start: str
    tournament_date: str
    winner_name: str
    loser_name: str
    winner_odds_column: str = "winner_best_close_odds"
    loser_odds_column: str = "loser_best_close_odds"
    winner_odds: str
    loser_odds: str
    reciprocal_sum: Decimal
    theoretical_roi: Decimal
    best_implied_overround_pct: Decimal
    reported_margin_pct: str
    winner_best_to_fair_ratio: Decimal | None
    loser_best_to_fair_ratio: Decimal | None
    suspected_issues: tuple[str, ...]
    confirmed_source_price_error: Literal[False] = False


class Anomaly(SourceRow):
    category: Literal["confirmed_invalid_price", "confirmed_missing_price"]
    details: tuple[str, ...]


class SkippedAudit(SourceRow):
    classification: str
    original_reasons: tuple[str, ...]
    missing_price_columns: tuple[str, ...]
    invalid_price_columns: tuple[str, ...]
    alternate_odds_values: dict[str, str]
    alternate_pair_finite_gt_one: bool
    note: str = "Alternative fair odds are not executable quotes; no substitution"


class ArithmeticCheck(DomainModel):
    group: str
    source_file: str
    line: int
    match_id: str
    independent_s: Decimal
    production_s: Decimal
    independent_roi: Decimal
    production_roi: Decimal
    independent_worst_payout: Decimal
    production_worst_payout: Decimal
    ideal_stakes_match: bool
    penny_allocation_optimal: bool
    arithmetic_matches: bool


class Investigation(DomainModel):
    inputs: tuple[InputEvidence, ...]
    total_rows: int
    valid_matches: int
    candidate_count: int
    candidate_percentage: Decimal
    distributions: dict[str, Distribution]
    bands: tuple[BandCount, ...]
    subsets: tuple[Subset, ...]
    top_candidates: tuple[RowAudit, ...]
    suspected_case_count: int
    suspected_issue_counts: dict[str, int]
    confirmed_anomalies: tuple[Anomaly, ...]
    skipped_audits: tuple[SkippedAudit, ...]
    skipped_classifications: dict[str, int]
    arithmetic_checks: tuple[ArithmeticCheck, ...]
    all_valid_rows_sign_and_decimal_checks: int
    sign_or_decimal_discrepancies: int
    source_quality: dict[str, int]
    internal_checks: dict[str, int]
    schema_findings: dict[str, str]
    baseline_comparison: str
    baseline_sha256: str | None
    documentation_files: tuple[str, ...]
    methods: tuple[str, ...]
