"""Small deterministic evidence sets; no downloaded files or network required."""

import csv
import hashlib
import json
import socket
from collections import Counter
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from betting_scanner.interfaces.research import main
from betting_scanner.research.analysis import analyse
from betting_scanner.research.importer import import_directory
from betting_scanner.research.independent import verify
from betting_scanner.research.investigation import (
    audit_skipped,
    investigate,
    percentile,
    ranked,
    roi_band,
)
from betting_scanner.research.investigation_models import Investigation
from betting_scanner.research.investigation_reporting import export_investigation
from betting_scanner.research.models import SkippedRow
from betting_scanner.research.reporting import export_report
from betting_scanner.strategies.arbitrage.calculation import calculate_prices


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    directory = tmp_path / "inputs"
    directory.mkdir()
    fixture = Path(__file__).parent / "fixtures/research/atp_matches_2025.csv"
    with fixture.open(newline="", encoding="utf-8") as handle:
        base = next(csv.DictReader(handle))
    for tour in ("atp", "wta"):
        with (directory / f"{tour}_matches_2025.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(base))
            writer.writeheader()
            # WTA header-only tests empty per-tour statistics alongside ATP records.
            if tour == "wta":
                continue
            for i in range(45):
                a, b = (
                    ("1.9", "2.2")
                    if i < 10
                    else ("17", "8")
                    if i < 20
                    else ("1.9", "1.9")
                    if i < 30
                    else ("2.5", "2.5")
                )
                writer.writerow(
                    {
                        **base,
                        "match_id": f"match{i}",
                        "match_num": str(i),
                        "winner_best_close_odds": a,
                        "loser_best_close_odds": b,
                    }
                )
            writer.writerow(
                {
                    **base,
                    "match_id": "missing",
                    "match_num": "100",
                    "winner_best_close_odds": "",
                    "loser_best_close_odds": "",
                }
            )
            writer.writerow(
                {
                    **base,
                    "match_id": "invalid",
                    "match_num": "101",
                    "winner_best_close_odds": "1.0",
                }
            )
    return directory


@pytest.mark.parametrize(
    "roi,band",
    [
        ("0.01", "0-1%"),
        ("1", "1-2%"),
        ("2", "2-5%"),
        ("5", "5-10%"),
        ("10", "10-20%"),
        ("20", "20-50%"),
        ("50", "20-50%"),
        ("50.0001", ">50%"),
    ],
)
def test_band_boundaries(roi: str, band: str) -> None:
    assert roi_band(Decimal(roi)) == band


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_invalid_band(value: str) -> None:
    with pytest.raises(ValueError):
        roi_band(Decimal(value))


def test_interpolated_percentiles() -> None:
    values = (Decimal(30), Decimal(0), Decimal(20), Decimal(10))
    assert percentile(values, 0) == 0
    assert percentile(values, 50) == 15
    assert percentile(values, 75) == Decimal("22.5")
    assert percentile(values, 90) == 27
    assert percentile(values, 95) == Decimal("28.5")
    assert percentile(values, 99) == Decimal("29.7")
    assert percentile(values, 100) == 30
    assert percentile((), 95) is None
    assert percentile((Decimal(7),), 99) == 7
    with pytest.raises(ValueError):
        percentile(values, 101)
    with pytest.raises(ValueError):
        percentile((Decimal("NaN"),), 50)
    with localcontext() as context:
        context.prec = 2
        assert percentile(values, 99) == Decimal("29.7")


def test_full_investigation_traceability_and_reports(
    inputs: Path, tmp_path: Path
) -> None:
    before = {p: p.read_bytes() for p in inputs.glob("*.csv")}
    report = investigate(inputs)
    assert report.candidate_count == 35 and report.valid_matches == 45
    assert report.total_rows == 47
    assert len(report.top_candidates) == 30
    assert report.top_candidates[0].match_id == "match10"
    assert report.top_candidates[0].line == 12
    assert report.top_candidates[0].winner_odds == "17"
    assert report.top_candidates[0].theoretical_roi == 444
    assert report.distributions["all"].maximum == 444
    assert report.distributions["wta"].count == 0
    assert report.distributions["wta"].median is None
    assert len(report.arithmetic_checks) == 30
    assert Counter(c.group for c in report.arithmetic_checks) == {
        "normal": 10,
        "high": 10,
        "non_candidate": 10,
    }
    assert all(
        c.arithmetic_matches and c.penny_allocation_optimal and c.ideal_stakes_match
        for c in report.arithmetic_checks
    )
    assert report.sign_or_decimal_discrepancies == 0
    assert sum(b.count for b in report.bands if b.scope == "all") == 35
    assert report.skipped_classifications == {"missing_prices": 1, "invalid_prices": 1}
    assert len(report.confirmed_anomalies) == 2
    assert {a.raw["match_id"] for a in report.confirmed_anomalies} == {
        "missing",
        "invalid",
    }
    assert all(not a.confirmed_source_price_error for a in report.top_candidates)
    assert any(a.suspected_issues for a in report.top_candidates)
    # The two rejected records remain rejected, never folded into suspicious candidates.
    assert report.suspected_case_count <= report.candidate_count
    output = tmp_path / "out"
    export_investigation(report, output)
    assert (
        Investigation.model_validate_json((output / "statistics.json").read_text())
        == report
    )
    text = (output / "investigation.md").read_text(encoding="utf-8")
    assert "444.000000" in text and "not established" in text
    assert "0 arithmetic/staking discrepancies" in text
    for name, count in [
        ("top30.csv", 30),
        ("roi_bands.csv", 21),
        ("confirmed_anomalies.csv", 2),
    ]:
        with (output / name).open(newline="", encoding="utf-8") as handle:
            records = list(csv.DictReader(handle))
        assert len(records) == count
        if name == "top30.csv":
            assert json.loads(records[0]["raw"])["match_id"] == "match10"
            assert records[0]["winner_odds_column"] == "winner_best_close_odds"
    assert all(path.read_bytes() == content for path, content in before.items())


def test_baseline_preserved_and_compared(inputs: Path, tmp_path: Path) -> None:
    baseline_dir = tmp_path / "baseline"
    export_report(analyse(import_directory(inputs)), baseline_dir)
    path = baseline_dir / "report.json"
    before = path.read_bytes()
    report = investigate(inputs, path)
    assert report.baseline_comparison.startswith("Exact unfiltered")
    assert report.baseline_sha256 == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    export_report(
        analyse(import_directory(inputs), minimum_roi=Decimal(100)), baseline_dir
    )
    assert investigate(inputs, path).baseline_comparison.startswith("Baseline differs")


def test_skipped_independent_classification() -> None:
    row = SkippedRow(
        source_file="x.csv",
        line=2,
        tour="atp",
        raw={
            "winner_best_close_odds": "NA",
            "loser_best_close_odds": "2",
            "winner_close_fair_odds": "1.5",
            "loser_close_fair_odds": "3",
        },
        reasons=("validation error",),
    )
    audit = audit_skipped(row)
    assert audit.classification == "invalid_prices"
    assert audit.invalid_price_columns == ("winner_best_close_odds",)
    assert audit.alternate_pair_finite_gt_one
    duplicate = row.model_copy(
        update={
            "raw": {"winner_best_close_odds": "2", "loser_best_close_odds": "2"},
            "reasons": ("exact_duplicate_row",),
        }
    )
    assert audit_skipped(duplicate).classification == "duplicate"


def test_independent_checker_detects_production_defect(
    inputs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    match = import_directory(inputs).matches[0]
    good = calculate_prices(
        {match.winner_id: match.winner_odds, match.loser_id: match.loser_odds},
        Decimal(100),
    )
    bad = good.model_copy(
        update={"theoretical_roi": Decimal(999), "worst_case_payout": Decimal(1)}
    )
    monkeypatch.setattr(
        "betting_scanner.research.independent.calculate_prices", lambda *args: bad
    )
    check = verify(match, "injected_defect")
    assert not check.arithmetic_matches and not check.penny_allocation_optimal


def test_stable_ranking_under_low_decimal_precision(inputs: Path) -> None:
    candidates = analyse(import_directory(inputs)).candidates[:2]
    first = candidates[0].model_copy(
        update={
            "calculation": candidates[0].calculation.model_copy(
                update={"theoretical_roi": Decimal("1.00000000000000000001")}
            )
        }
    )
    second = candidates[1].model_copy(
        update={
            "calculation": candidates[1].calculation.model_copy(
                update={"theoretical_roi": Decimal("1.00000000000000000002")}
            )
        }
    )
    with localcontext() as context:
        context.prec = 3
        assert ranked((first, second))[0] == second


def test_empty_and_malformed(inputs: Path, tmp_path: Path) -> None:
    for path in inputs.glob("*.csv"):
        path.write_text(path.read_text().splitlines()[0] + "\n")
    report = investigate(inputs)
    assert not report.top_candidates and not report.arithmetic_checks
    assert report.distributions["all"].mean is None
    export_investigation(report, tmp_path / "empty")
    assert (tmp_path / "empty/top30.csv").read_text().count("\n") == 1
    (inputs / "atp_matches_2025.csv").write_text("wrong,header\na,b\n")
    with pytest.raises(ValueError, match="columns"):
        investigate(inputs)


def test_offline_cli(
    inputs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("No network permitted")

    monkeypatch.setattr(socket, "socket", forbidden)
    assert (
        main(["investigate", str(inputs), "--output-dir", str(tmp_path / "out")]) == 0
    )
    assert (tmp_path / "out/investigation.md").exists()
