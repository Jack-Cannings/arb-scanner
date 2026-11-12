"""Deterministic offline CSV research and shared-calculator regression checks."""

import csv
import json
import os
import shutil
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from betting_scanner.core.models import OddsQuote
from betting_scanner.interfaces.research import main
from betting_scanner.research.analysis import ResearchReport, analyse
from betting_scanner.research.importer import import_directory
from betting_scanner.research.models import Candidate
from betting_scanner.research.reporting import export_report, terminal_summary
from betting_scanner.strategies.arbitrage.calculation import calculate_prices

FIXTURES = Path(__file__).parent / "fixtures" / "research"


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    target = tmp_path / "inputs"
    shutil.copytree(FIXTURES, target)
    return target


def rewrite(
    directory: Path, changes: dict[str, str], copies: int = 1, conflict: bool = False
) -> None:
    path = directory / "atp_matches_2025.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        original = next(reader)
    updated = {**original, **changes}
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(original))
        writer.writeheader()
        if conflict:
            writer.writerow(original)
        for _ in range(copies):
            writer.writerow(updated)


@pytest.mark.parametrize("tour,count", [("atp", 1), ("wta", 1), ("both", 2)])
def test_import_actual_schema(inputs: Path, tour: str, count: int) -> None:
    imported = import_directory(inputs, tour)
    assert len(imported.matches) == count
    match = imported.matches[0]
    assert match.winner_odds == Decimal("2.20")
    assert match.raw["winner_best_close_odds"] == "2.20"
    assert match.tournament_date.isoformat() == "2025-01-01"
    assert match.match_start is not None and match.match_start.day == 2
    assert match.odds_observed_at is None and match.bookmaker_id is None
    assert match.bookmaker_count == 18
    assert len(imported.inputs[0].sha256) == 64


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "-2", "0", "1", "oops"])
def test_reject_missing_or_invalid_odds(inputs: Path, value: str) -> None:
    rewrite(inputs, {"winner_best_close_odds": value})
    imported = import_directory(inputs, "atp")
    assert not imported.matches and len(imported.skipped) == 1
    assert imported.skipped[0].raw["winner_best_close_odds"] == value
    assert imported.skipped[0].reasons


@pytest.mark.parametrize("key", ["match_id", "winner_id", "loser_id", "tourney_id"])
def test_missing_identifiers(inputs: Path, key: str) -> None:
    rewrite(inputs, {key: ""})
    assert len(import_directory(inputs, "atp").skipped) == 1


def test_duplicate_rows_once(inputs: Path) -> None:
    rewrite(inputs, {}, copies=2)
    imported = import_directory(inputs, "atp")
    assert len(imported.matches) == len(imported.skipped) == 1
    assert imported.skipped[0].reasons == ("exact_duplicate_row",)
    assert len(analyse(imported).candidates) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"winner_best_close_odds": "4"},
        {"winner_best_close_odds": "invalid"},
        {"match_id": "other"},
    ],
)
def test_conflicting_duplicates_reject_all(
    inputs: Path, changes: dict[str, str]
) -> None:
    rewrite(inputs, changes, conflict=True)
    imported = import_directory(inputs, "atp")
    assert not imported.matches and len(imported.skipped) == 2


def test_missing_optional_evidence_and_unusual_price(inputs: Path) -> None:
    rewrite(
        inputs,
        {
            "start_time_utc": "",
            "bookmakers": "",
            "score": "",
            "winner_best_close_odds": "101",
        },
    )
    imported = import_directory(inputs, "atp")
    match = imported.matches[0]
    assert match.match_start is match.odds_observed_at is match.bookmaker_id is None
    assert set(match.warnings) == {
        "missing_match_start",
        "missing_result",
        "unusual_price_at_least_100",
    }
    assert len(analyse(imported).candidates) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"start_time_utc": "2025-01-01T00:00:00"},
        {"tourney_date": "20250230"},
        {"loser_id": "p1"},
    ],
)
def test_invalid_identity_and_dates(inputs: Path, changes: dict[str, str]) -> None:
    rewrite(inputs, changes)
    assert len(import_directory(inputs, "atp").skipped) == 1


@pytest.mark.parametrize(
    "prices,expected", [("2.2", True), ("1.9", False), ("2", False)]
)
def test_arb_noarb_break_even(prices: str, expected: bool) -> None:
    result = calculate_prices(
        {"a": Decimal(prices), "b": Decimal(prices)}, Decimal(100)
    )
    assert result.mathematical_arbitrage is expected
    assert sum(leg.stake for leg in result.legs) == 100
    if expected:
        assert result.theoretical_roi == 10
        assert result.worst_case_profit == 10


@pytest.mark.parametrize("budget", ["100", "0.01", "0.02", "0.03"])
def test_penny_allocation_optimal(budget: str) -> None:
    total = Decimal(budget)
    result = calculate_prices({"a": Decimal("2.13"), "b": Decimal("2.17")}, total)
    penny = Decimal("0.01")
    n = int(total / penny)
    expected = max(
        min(
            (i * penny * Decimal("2.13")).quantize(penny, rounding="ROUND_FLOOR"),
            ((n - i) * penny * Decimal("2.17")).quantize(penny, rounding="ROUND_FLOOR"),
        )
        for i in range(n + 1)
    )
    assert result.worst_case_payout == expected
    assert sum(leg.stake for leg in result.legs) == total


def test_quote_free_math_validation() -> None:
    with pytest.raises(ValueError):
        calculate_prices({"a": Decimal(1), "b": Decimal(3)}, Decimal(100))
    with pytest.raises(ValueError):
        calculate_prices({"a": Decimal(2)}, Decimal(100))
    with pytest.raises(ValueError):
        calculate_prices({"a": Decimal(2), "b": Decimal(2)}, Decimal("0.001"))
    result = calculate_prices(
        {"c": Decimal(3), "b": Decimal(3), "a": Decimal(3)}, Decimal(100)
    )
    assert not result.mathematical_arbitrage


def test_reports_and_separation(inputs: Path, tmp_path: Path) -> None:
    rewrite(inputs, {}, copies=2)
    report = analyse(import_directory(inputs))
    assert (
        report.total_rows == 3
        and report.valid_matches == 2
        and report.skipped_rows == 1
    )
    assert report.average_theoretical_roi == report.highest_theoretical_roi == 10
    assert report.candidate_percentage == 100
    assert report.non_realistic_conditional_upper_bound_profit == 20
    output = tmp_path / "reports"
    export_report(report, output)
    restored = ResearchReport.model_validate_json((output / "report.json").read_text())
    assert restored == report
    for name, count in [("candidates.csv", 2), ("skipped.csv", 1)]:
        with (output / name).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == count
    candidate = report.candidates[0]
    assert not candidate.executed and candidate.verification == "unverified"
    with pytest.raises(ValidationError):
        Candidate.model_validate({**candidate.model_dump(), "executed": True})
    with pytest.raises(ValidationError):
        OddsQuote.model_validate(candidate.model_dump())
    assert "no realised profit" in terminal_summary(report)


def test_empty_and_filtered_reports(inputs: Path, tmp_path: Path) -> None:
    filtered = analyse(import_directory(inputs), minimum_roi=Decimal(11))
    assert filtered.mathematical_candidates_before_roi_filter == 2
    assert not filtered.candidates and filtered.highest_theoretical_roi == 0
    for path in inputs.glob("*.csv"):
        path.write_text(path.read_text().splitlines()[0] + "\n")
    report = analyse(import_directory(inputs))
    assert report.total_rows == report.valid_matches == report.skipped_rows == 0
    export_report(report, tmp_path / "empty")
    assert json.loads((tmp_path / "empty/report.json").read_text())["candidates"] == []
    assert (tmp_path / "empty/candidates.csv").read_text().count("\n") == 1


def test_cli_offline_no_credentials(
    inputs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Research must not use the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    assert (
        main(
            [
                "analyse",
                str(inputs),
                "--tour",
                "wta",
                "--stake",
                "100",
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    assert "WTA analysed: 1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "option,value",
    [
        ("--stake", "0"),
        ("--stake", "0.001"),
        ("--stake", "oops"),
        ("--min-roi", "NaN"),
        ("--min-roi", "-1"),
    ],
)
def test_cli_errors(inputs: Path, option: str, value: str) -> None:
    with pytest.raises(SystemExit) as error:
        main(["analyse", str(inputs), option, value])
    assert error.value.code == 2


def test_optional_local_smoke() -> None:
    if os.environ.get("RUN_LOCAL_CSV_SMOKE") != "1":
        pytest.skip("Set RUN_LOCAL_CSV_SMOKE=1 to inspect ignored downloaded CSVs")
    directory = Path("data/historical-imports")
    report = analyse(import_directory(directory))
    assert report.atp_matches > 0 and report.wta_matches > 0
    assert report.total_rows == report.valid_matches + report.skipped_rows
    assert all(not c.executed for c in report.candidates)


def test_schema_and_width_errors(inputs: Path) -> None:
    path = inputs / "atp_matches_2025.csv"
    original = path.read_text()
    path.write_text(original + original.splitlines()[1] + ",extra\n")
    imported = import_directory(inputs, "atp")
    assert len(imported.matches) == len(imported.skipped) == 1
    assert "__extra_cells__" in imported.skipped[0].raw
    path.write_text("match_id,match_id\na,a\n")
    with pytest.raises(ValueError, match="columns"):
        import_directory(inputs, "atp")


def test_quality_variants_and_input_unchanged(inputs: Path, tmp_path: Path) -> None:
    rewrite(
        inputs,
        {
            "match_id": "second",
            "match_num": "2",
            "winner_name": "Variant A",
            "tourney_name": "Example Playoffs",
            "start_time_utc": "2024-12-31T23:00:00Z",
        },
        conflict=True,
    )
    before = {path: path.read_bytes() for path in inputs.glob("*.csv")}
    imported = import_directory(inputs)
    assert imported.quality["player_id_multiple_names"] == 1
    assert imported.quality["tournament_id_multiple_names"] == 1
    assert imported.quality["match_start_outside_filename_year_2025"] == 1
    export_report(analyse(imported), tmp_path / "output")
    assert all(path.read_bytes() == value for path, value in before.items())


def test_prices_not_fair_odds_or_results(inputs: Path) -> None:
    rewrite(
        inputs,
        {
            "winner_best_close_odds": "1.9",
            "loser_best_close_odds": "1.9",
            "winner_close_fair_odds": "5",
            "loser_close_fair_odds": "5",
        },
    )
    report = analyse(import_directory(inputs, "atp"))
    assert not report.candidates
    rewrite(inputs, {"winner_best_close_odds": "2.5", "loser_best_close_odds": "2.1"})
    first = analyse(import_directory(inputs, "atp")).candidates[0].calculation
    rewrite(
        inputs,
        {
            "winner_best_close_odds": "2.1",
            "loser_best_close_odds": "2.5",
            "score": "RET",
        },
    )
    second = analyse(import_directory(inputs, "atp")).candidates[0].calculation
    assert first.theoretical_roi == second.theoretical_roi
    assert first.worst_case_profit == second.worst_case_profit
