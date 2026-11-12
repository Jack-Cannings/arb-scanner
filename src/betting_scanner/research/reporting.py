"""Portable JSON and CSV research exports. Decimal values remain strings."""

import csv
import json
from pathlib import Path

from betting_scanner.research.analysis import ResearchReport


def export_report(report: ResearchReport, output: Path) -> None:
    # Never let exports overwrite raw source files, even if pointed at input dir.
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    columns = [
        "tour",
        "match_id",
        "tournament",
        "tournament_date",
        "match_start",
        "winner_id",
        "winner_name",
        "loser_id",
        "loser_name",
        "winner_odds",
        "loser_odds",
        "reciprocal_sum",
        "theoretical_roi_pct",
        "theoretical_profit",
        "worst_case_payout",
        "worst_case_profit",
        "allocations_json",
        "verification",
        "executed",
        "odds_observed_at",
        "bookmaker_id",
        "source_file",
        "line",
        "warnings_json",
        "raw_json",
    ]
    with (output / "candidates.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for candidate in report.candidates:
            match, calc = candidate.match, candidate.calculation
            writer.writerow(
                {
                    "tour": match.tour,
                    "match_id": match.match_id,
                    "tournament": match.tournament_name,
                    "tournament_date": match.tournament_date,
                    "match_start": match.match_start.isoformat()
                    if match.match_start
                    else "",
                    "winner_id": match.winner_id,
                    "winner_name": match.winner_name,
                    "loser_id": match.loser_id,
                    "loser_name": match.loser_name,
                    "winner_odds": match.winner_odds,
                    "loser_odds": match.loser_odds,
                    "reciprocal_sum": calc.reciprocal_sum,
                    "theoretical_roi_pct": calc.theoretical_roi,
                    "theoretical_profit": calc.theoretical_profit,
                    "worst_case_payout": calc.worst_case_payout,
                    "worst_case_profit": calc.worst_case_profit,
                    "allocations_json": json.dumps(
                        [leg.model_dump(mode="json") for leg in calc.legs]
                    ),
                    "verification": candidate.verification,
                    "executed": False,
                    "odds_observed_at": "",
                    "bookmaker_id": "",
                    "source_file": match.source_file,
                    "line": match.line,
                    "warnings_json": json.dumps(match.warnings + candidate.warnings),
                    "raw_json": json.dumps(match.raw),
                }
            )
    with (output / "skipped.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source_file", "line", "tour", "reasons_json", "raw_json"],
        )
        writer.writeheader()
        for row in report.imported.skipped:
            writer.writerow(
                {
                    "source_file": row.source_file,
                    "line": row.line,
                    "tour": row.tour,
                    "reasons_json": json.dumps(row.reasons),
                    "raw_json": json.dumps(row.raw),
                }
            )


def terminal_summary(report: ResearchReport) -> str:
    return (
        "UNVERIFIED historical closing-price research (no executed trades)\n"
        f"Rows: {report.total_rows}; valid: {report.valid_matches}; "
        f"skipped: {report.skipped_rows}\n"
        f"ATP analysed: {report.atp_matches}; WTA analysed: {report.wta_matches}\n"
        "Mathematical candidates before ROI filter: "
        f"{report.mathematical_candidates_before_roi_filter}\n"
        f"Selected candidates: {len(report.candidates)} "
        f"({report.candidate_percentage:.4f}% of valid matches)\n"
        f"Average theoretical ROI: {report.average_theoretical_roi:.4f}%; "
        f"highest: {report.highest_theoretical_roi:.4f}%\n"
        "Non-realistic conditional upper-bound hypothetical profit: GBP "
        f"{report.non_realistic_conditional_upper_bound_profit:.2f}\n"
        "Assumes every selected candidate was executable; no realised profit.\n"
        "Bookmaker identities, observation times, simultaneity and rules are unknown.\n"
        "Quality findings, raw records, schema, hashes and limitations: report.json"
    )
