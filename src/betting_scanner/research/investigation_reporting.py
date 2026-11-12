"""Readable and machine-readable investigation artifacts, stored outside Git."""

import csv
import json
from collections import Counter
from pathlib import Path

from betting_scanner.research.investigation_models import (
    Investigation,
    RowAudit,
    Subset,
)


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _subset_table(rows: list[Subset]) -> list[str]:
    result = [
        "| Subset | Valid | Candidates | Rate % | ROI >50% |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    result.extend(
        f"| {_cell(r.key)} | {r.valid_matches} | {r.candidates} | "
        f"{r.candidate_percentage:.4f} | {r.extreme_over_50} |"
        for r in rows
    )
    return result


def markdown(report: Investigation) -> str:
    failures = [
        c
        for c in report.arithmetic_checks
        if not (
            c.arithmetic_matches and c.ideal_stakes_match and c.penny_allocation_optimal
        )
    ]
    invalid = sum(
        a.category == "confirmed_invalid_price" for a in report.confirmed_anomalies
    )
    missing = sum(
        a.category == "confirmed_missing_price" for a in report.confirmed_anomalies
    )
    rows = [
        "# Historical arbitrage data-quality investigation",
        "",
        "## Executive assessment",
        "",
        "Suitable for exploratory source-data and mathematical research only. "
        "It cannot establish executable arbitrage. The high candidate rate is "
        "reproduced, but its source-level cause is not established. Best-price "
        "outliers and disagreements with other price-related fields require "
        "original collection evidence, not speculative cleaning.",
        "",
        f"Rows: {report.total_rows}; valid: {report.valid_matches}; "
        f"candidates: {report.candidate_count} ({report.candidate_percentage:.4f}%). "
        f"Confirmed invalid-price records: {invalid}; confirmed missing-price "
        f"records: {missing}. Suspicious candidate records under the documented "
        f"review heuristics: {report.suspected_case_count} (overlapping flags).",
        "",
        f"Baseline: {report.baseline_comparison}. Baseline SHA-256: "
        f"{report.baseline_sha256 or 'not supplied'}.",
        "",
        "## Schema, provenance and collection evidence",
        "",
    ]
    rows.extend(
        f"- **{key}**: {value}" for key, value in report.schema_findings.items()
    )
    rows += [
        "",
        "Local documentation files: "
        + (", ".join(report.documentation_files) or "none alongside supplied CSVs"),
        "",
        "These findings refer only to supplied local evidence. No external "
        "source, bookmaker or licence has been inferred or looked up.",
        "",
    ]
    for source in report.inputs:
        rows += [
            f"### {source.source_file}",
            "",
            f"Rows: {source.rows}; SHA-256: `{source.sha256}`.",
            "",
            "Actual columns: " + ", ".join(f"`{c}`" for c in source.columns),
            "",
        ]
    rows += [
        "## Full candidate ROI distribution",
        "",
        "Percentiles use inclusive linear interpolation at (n−1)p. "
        "Values are percent ROI, not monetary returns.",
        "",
        "| Scope | Candidates | Min | Median | Mean | P75 | P90 | P95 | P99 | Max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scope, stats in report.distributions.items():
        values = [
            stats.minimum,
            stats.median,
            stats.mean,
            stats.p75,
            stats.p90,
            stats.p95,
            stats.p99,
            stats.maximum,
        ]
        formatted = " | ".join(f"{v:.6f}" if v is not None else "N/A" for v in values)
        rows.append(f"| {scope} | {stats.count} | {formatted} |")
    rows += [
        "",
        "| Scope | ROI band | Count | % of scope candidates |",
        "| --- | --- | ---: | ---: |",
    ]
    rows.extend(
        f"| {b.scope} | {b.band} | {b.count} | {b.percentage_of_candidates:.4f} |"
        for b in report.bands
    )
    rows += [
        "",
        "## Concentration by subset",
        "",
        "All subset counts are available in statistics.json. Tournament "
        "rate ranking below requires at least 10 valid rows; small groups "
        "and naming variants are not merged or treated as statistical proof.",
        "",
    ]
    rows += _subset_table([s for s in report.subsets if s.scope == "tour"])
    tournaments = sorted(
        (
            s
            for s in report.subsets
            if s.scope == "tournament" and s.valid_matches >= 10
        ),
        key=lambda s: s.candidate_percentage,
        reverse=True,
    )
    rows += ["", "### Highest tournament rates (minimum 10 valid rows)", ""]
    rows += _subset_table(tournaments[:15])
    for scope in ("month", "odds_range", "bookmaker_count"):
        rows += ["", f"### {scope}", ""]
        rows += _subset_table([s for s in report.subsets if s.scope == scope])
    dates = sorted(
        (s for s in report.subsets if s.scope == "date" and s.extreme_over_50),
        key=lambda s: s.extreme_over_50,
        reverse=True,
    )
    rows += ["", "### Dates with most extreme (>50% ROI) cases", ""]
    rows += _subset_table(dates[:15])
    rows += [
        "",
        "## Top 30 row-by-row investigation",
        "",
        "Every row below is a valid mathematical candidate, not a confirmed "
        "source-price error. Source line includes the header and is the CSV "
        "record's ending physical line. Original column names are "
        "winner_best_close_odds / loser_best_close_odds. Match start is "
        "not an odds timestamp. Full original cells are in top30.csv.",
        "",
    ]
    for index, a in enumerate(report.top_candidates, 1):
        rows += [
            f"### {index}. {_cell(a.match_id)} — ROI {a.theoretical_roi:.6f}%",
            "",
            f"Source: `{a.source_file}:{a.line}`; tour: {a.tour}; "
            f"tournament: {_cell(a.tournament)}; "
            f"match start: {a.match_start or 'missing'}; "
            f"tournament date: {a.tournament_date}.",
            "",
            f"Winner: {_cell(a.winner_name)}; loser: {_cell(a.loser_name)}. "
            f"Original best closing prices: {a.winner_odds} / {a.loser_odds}. "
            f"S = {a.reciprocal_sum}; implied best-price overround = "
            f"{a.best_implied_overround_pct:.6f}%; reported margin_pct = "
            f"{a.reported_margin_pct or 'missing'} (definition unknown).",
            "",
            "Other source fields: "
            + "; ".join(
                f"{k}={a.raw.get(k, '') or 'missing'}"
                for k in (
                    "winner_open_prob",
                    "winner_close_prob",
                    "winner_close_fair_odds",
                    "loser_close_fair_odds",
                    "bookmakers",
                )
            )
            + ".",
            "",
            f"Best/fair ratios: winner={a.winner_best_to_fair_ratio}; "
            f"loser={a.loser_best_to_fair_ratio}. Review flags: "
            f"{', '.join(a.suspected_issues) or 'none under selected heuristics'}.",
            "",
            "Exact raw-string arithmetic reproduces the prices. Wrong-player "
            "assignment cannot be resolved by swapping this pair (S is symmetric). "
            "Missing price interpretation, entry errors, mismatched outcomes, "
            "mixed observation times/markets and independent maxima remain "
            "unverified hypotheses. Large underdog prices alone do not prove "
            "an error. No row is removed or repaired.",
            "",
        ]
    rows += [
        "## Independent calculation and rounding validation",
        "",
        f"All {report.all_valid_rows_sign_and_decimal_checks} valid rows checked "
        f"for exact raw Decimal conversion and rational S<1 classification: "
        f"{report.sign_or_decimal_discrepancies} discrepancies.",
        "",
        f"Detailed deterministic sample: {len(report.arithmetic_checks)} rows; "
        f"{len(failures)} arithmetic/staking discrepancies. Sample sizes: "
        f"{dict(Counter(c.group for c in report.arithmetic_checks))}.",
        "",
        "| Group | Source | Match | Rational ROI % | "
        "Penny payout GBP | All checks pass |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    rows.extend(
        f"| {c.group} | {c.source_file}:{c.line} | {c.match_id} | "
        f"{c.independent_roi:.8f} | {c.independent_worst_payout:.2f} | "
        f"{c not in failures} |"
        for c in report.arithmetic_checks
    )
    rows += [
        "",
        "## Skipped-record audit",
        "",
        "Every skipped row is retained in statistics.json: "
        f"{report.skipped_classifications}.",
        "",
        "Alternative fair-odds pairs finite and above one: "
        f"{sum(r.alternate_pair_finite_gt_one for r in report.skipped_audits)}. "
        "Even valid fair odds would not establish bookmaker quotes, so no "
        "substitution is performed. All original reasons, price values and "
        "alternative columns are preserved for each row. Blank fields confirm "
        "missingness; neither blanks nor a value of 1.0 establish data corruption "
        "or its cause. Duplicate classifications are not silently included.",
        "",
        "Observed selected-price string pairs across all rejected rows: "
        + repr(
            dict(
                Counter(
                    (
                        r.raw.get("winner_best_close_odds", ""),
                        r.raw.get("loser_best_close_odds", ""),
                    )
                    for r in report.skipped_audits
                )
            )
        )
        + ".",
        "",
        "Missing-price rows with all other probability/fair-price/margin fields blank: "
        + str(
            sum(
                r.classification == "missing_prices"
                and all(
                    not r.raw.get(k, "").strip()
                    for k in (
                        "winner_open_prob",
                        "winner_close_prob",
                        "winner_close_fair_odds",
                        "loser_close_fair_odds",
                        "margin_pct",
                    )
                )
                for r in report.skipped_audits
            )
        )
        + ". No fair-price fallback is made.",
        "",
        "Source duplicate/malformed-width findings: "
        + repr(
            {
                k: report.source_quality.get(k, 0)
                for k in (
                    "exact_duplicate_row",
                    "conflicting_duplicate_identity",
                    "malformed_csv_width",
                )
            }
        )
        + ". Raw missing-value conventions remain in the individual audit records.",
        "",
        "## Internal consistency diagnostics",
        "",
        json.dumps(report.internal_checks, indent=2),
        "",
        "Probability/fair checks use a 0.001 absolute heuristic tolerance. "
        "Agreement suggests internally related fields but does not establish "
        "their source or methodology. Positive reported margin versus negative "
        "best-price overround is an observed difference between fields with "
        "unknown definitions, not proof of corrupted data.",
        "",
        "Suspicion counts (overlapping; no filtering): "
        + json.dumps(report.suspected_issue_counts, sort_keys=True),
        "",
        "## Reliability questions and next step",
        "",
        f"1. Calculations: {len(failures)} detailed sample discrepancies and "
        f"{report.sign_or_decimal_discrepancies} full-data "
        "conversion/sign discrepancies. "
        "Production mathematics is unchanged.",
        "2. Internal consistency: best prices contain substantial outliers "
        "relative to other fields; their correct alignment cannot be established. "
        f"The {invalid} invalid-price records are confirmed invalid inputs, "
        "not diagnosed collection errors.",
        "3. Bookmaker identities: not established from these files.",
        "4. Simultaneity: not established; match start cannot substitute.",
        "5. Settlement compatibility: not established; market/rule evidence absent.",
        "6. Executable arbitrage claims: unsupported. No trades or realised returns.",
        "7. Research suitability: useful for exploratory mathematical and data-quality "
        "analysis, not execution or predictive performance claims.",
        "8. Needed evidence: original publisher/download URL and licence, collection "
        "code/methodology, per-bookmaker outcome/market mappings, timestamped raw "
        "price snapshots, settlement/retirement rules and available stake/acceptance "
        "evidence. Obtain these before extending opportunity verification.",
        "",
        "The unusually high rate and maximum are explained algebraically by the "
        "recorded pairs, not causally by available collection evidence. Do not "
        "correct, filter or upgrade them based on speculation.",
        "",
        "## Reproducibility methods",
        "",
    ]
    rows.extend(f"- {method}" for method in report.methods)
    return "\n".join(rows) + "\n"


def export_investigation(report: Investigation, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "investigation.md").write_text(markdown(report), encoding="utf-8")
    (output / "statistics.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    for name, records, columns in (
        ("top30.csv", report.top_candidates, tuple(RowAudit.model_fields)),
        (
            "roi_bands.csv",
            report.bands,
            ("scope", "band", "count", "percentage_of_candidates"),
        ),
        (
            "confirmed_anomalies.csv",
            report.confirmed_anomalies,
            ("source_file", "line", "tour", "raw", "category", "details"),
        ),
    ):
        with (output / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for record in records:
                values = record.model_dump(mode="json")
                writer.writerow(
                    {
                        k: json.dumps(v, ensure_ascii=False)
                        if isinstance(v, (dict, list))
                        else v
                        for k, v in values.items()
                    }
                )
