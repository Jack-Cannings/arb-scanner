"""Deterministic read-only dashboard tests with real Streamlit reruns."""

import hashlib
import shutil
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import requests
import streamlit as st
from streamlit.testing.v1 import AppTest

from betting_scanner.backtesting.models import BacktestReport, ReplayConfig
from betting_scanner.backtesting.reader import JsonHistoricalReader
from betting_scanner.backtesting.replay import run_replay
from betting_scanner.backtesting.reporting import export_report as export_backtest
from betting_scanner.dashboard import app
from betting_scanner.dashboard.loading import (
    Paths,
    backtest_directories,
    confined,
    load_csv,
    load_live,
    load_model,
)
from betting_scanner.dashboard.presentation import (
    credits,
    filter_markets,
    gbp,
    market_views,
    monitoring_rows,
    pct,
    quote_rows,
    research_rows,
    text,
    trade_rows,
)
from betting_scanner.live.models import (
    LiveManifest,
    LiveObservation,
    LiveScanPlan,
    LiveScanReport,
    ObservationIndex,
)
from betting_scanner.live.monitor_models import (
    MonitoringReport,
    MonitoringScan,
    MonitorPlan,
    StopReason,
)
from betting_scanner.providers.odds_api.client import ApiAttempt, BudgetState
from betting_scanner.providers.odds_api.normalise import normalise_live
from betting_scanner.providers.odds_api.schema import QuotaHeaders, ResponseRecord
from betting_scanner.research.analysis import ResearchReport, analyse
from betting_scanner.research.importer import import_directory
from betting_scanner.research.investigation import investigate
from betting_scanner.research.investigation_reporting import export_investigation
from betting_scanner.research.reporting import export_report as export_research
from betting_scanner.strategies.arbitrage import detect_arbitrage
from betting_scanner.strategies.arbitrage.results import VerificationStatus

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2024, 7, 1, 12, tzinfo=UTC)


def save(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    st.cache_data.clear()
    root = tmp_path / "project"
    live = root / "data/live-tennis"
    plan = LiveScanPlan(sport_keys=("tennis_atp_fictional_open",))
    response = ResponseRecord(
        endpoint="sports/tennis_atp_fictional_open/odds",
        requested_at=NOW,
        collected_at=NOW,
        status_code=200,
        headers={},
        quota=QuotaHeaders(remaining=99, used=1, last=1),
        body=(FIXTURES / "live/odds.json").read_text(),
    )
    page = normalise_live(response, plan.sport_keys[0])
    observation = LiveObservation(
        id="observation-fixture",
        sport_key=plan.sport_keys[0],
        observed_at=NOW,
        normalised=page,
        analyses=tuple(
            detect_arbitrage(s.snapshot, m.id, total_stake=plan.stake, evaluated_at=NOW)
            for s in page.snapshots
            for m in s.snapshot.markets
        ),
        excluded_started=(),
        plan=plan,
    )
    budget = BudgetState(
        max_credits=5,
        attempts=(
            ApiAttempt(
                endpoint=response.endpoint,
                requested_at=NOW,
                reserved_credits=1,
                actual_credits=1,
                status="received",
                http_status=200,
                quota=response.quota,
                scope="monitor-fixture",
            ),
        ),
    )
    manifest = LiveManifest(
        created_at=NOW,
        updated_at=NOW,
        budget=budget,
        observations={
            "fixture": ObservationIndex(
                observation_id=observation.id,
                body_digest=hashlib.sha256(response.body.encode()).hexdigest(),
                observed_at=NOW,
            )
        },
    )
    save(live / "manifest.json", manifest.model_dump_json())
    save(live / f"observations/{observation.id}.json", observation.model_dump_json())
    scan = LiveScanReport(
        plan=plan,
        observations=(observation,),
        errors=(),
        run_budget=budget,
        archive_budget=budget,
        events_analysed=len(page.snapshots),
        unique_bookmakers=2,
        quotes_processed=sum(len(s.snapshot.quotes) for s in page.snapshots),
        mathematical_candidates=1,
        eligible_opportunities=0,
        unverified_candidates=1,
        rejected_markets=0,
    )
    session = MonitoringReport(
        id="monitor-fixture",
        plan=MonitorPlan(scan=plan),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        stop_reason=StopReason.MAX_SCANS,
        scans=(
            MonitoringScan(
                number=1,
                started_at=NOW,
                report=scan,
                new_candidates=("candidate-fixture",),
            ),
        ),
        session_budget=budget,
        archive_budget=budget,
    )
    save(live / "monitoring/monitor-000001.json", session.model_dump_json())
    inputs = root / "data/historical-imports"
    shutil.copytree(FIXTURES / "research", inputs)
    export_research(analyse(import_directory(inputs)), root / "data/research-output")
    export_investigation(investigate(inputs), root / "data/investigation-output")
    for name, fixture in (
        ("settled", "a_success.json"),
        ("open", "i_incomplete_results.json"),
    ):
        report = run_replay(
            JsonHistoricalReader(FIXTURES / "histories" / fixture).read(),
            ReplayConfig(),
        )
        export_backtest(report, root / "backtest-output" / name)
    return root


def test_saved_live_load_and_history(project: Path) -> None:
    data = load_live(Paths(project))
    assert data.manifest is not None and len(data.observations) == 1
    assert len(data.sessions) == 1 and not data.notices
    rows = monitoring_rows(data)
    assert rows[0]["New"] == 1 and rows[0]["Continuing"] == 0
    assert rows[0]["Accounted credits"] == 1


def test_missing_and_empty_archives(tmp_path: Path) -> None:
    data = load_live(Paths(tmp_path))
    assert not data.observations and data.manifest is None
    assert data.notices
    budget = BudgetState(max_credits=0)
    save(
        tmp_path / "data/live-tennis/manifest.json",
        LiveManifest(created_at=NOW, updated_at=NOW, budget=budget).model_dump_json(),
    )
    data = load_live(Paths(tmp_path))
    assert data.manifest is not None and not data.observations


def test_malformed_and_changed_cache(project: Path) -> None:
    paths = Paths(project)
    original = load_live(paths).manifest
    assert original is not None
    manifest_path = paths.live / "manifest.json"
    save(
        manifest_path,
        original.model_copy(
            update={"budget": BudgetState(max_credits=42)}
        ).model_dump_json(),
    )
    changed = load_live(paths).manifest
    assert changed is not None and changed.budget.max_credits == 42
    save(manifest_path, '{"ODDS_API_KEY": "sensitive-payload",')
    invalid = load_model(paths.live, "manifest.json", LiveManifest)
    assert invalid.value is None and "sensitive-payload" not in str(invalid.notice)
    save(paths.live / "monitoring/monitor-broken.json", "{")
    assert load_live(paths).notices
    save(paths.data / "research-output/candidates.csv", "a,a\n1,2\n")
    assert load_csv(paths.data, "research-output/candidates.csv").value is None


def test_quote_filter_and_decimal_stakes(project: Path) -> None:
    observations = load_live(Paths(project)).observations
    before = observations[0].model_dump_json()
    views = market_views(observations, Decimal(100))
    candidates = [
        v
        for v in views
        if v.result.candidate and v.result.candidate.mathematical_arbitrage
    ]
    assert candidates
    candidate = candidates[0]
    assert candidate.result.verification_status == VerificationStatus.UNVERIFIED
    assert any(
        r.code.value == "unverified_settlement" for r in candidate.result.reasons
    )
    assert not filter_markets(views, competition="not present")
    assert not filter_markets(views, minimum_roi=Decimal(1000))
    assert filter_markets(views, match=candidate.label)
    books = candidate.snapshot.bookmakers
    rows = quote_rows((candidate,), NOW + timedelta(days=1), books[0].name)
    assert rows and all(r["Bookmaker"] == books[0].name for r in rows)
    assert all("Stale now" in str(r["Freshness now"]) for r in rows)
    smaller = market_views(observations, Decimal("0.03"))[0].result.candidate
    assert smaller is not None and sum(leg.stake for leg in smaller.legs) == Decimal(
        "0.03"
    )
    with pytest.raises(ValueError):
        market_views(observations, Decimal("0.001"))
    assert observations[0].model_dump_json() == before


def test_credit_display_unknown_charges(project: Path) -> None:
    manifest = load_live(Paths(project)).manifest
    assert manifest is not None
    row = credits(manifest.budget, manifest.updated_at)
    assert row["Last reported provider balance"] == 99
    assert row["Balance request time"] == "2024-07-01 12:00:00 UTC"
    assert row["Archive accounted credits"] == 1 and row["Archive headroom"] == 4
    uncertain = manifest.budget.model_copy(
        update={
            "attempts": (
                *manifest.budget.attempts,
                ApiAttempt(
                    endpoint="interrupted", requested_at=NOW, reserved_credits=1
                ),
            )
        }
    )
    result = credits(uncertain, NOW)
    assert result["Confirmed actual credits"] is None
    assert result["Archive accounted credits"] == 2
    assert result["Last reported provider balance"] == 99


def test_research_statistics_and_separation(project: Path) -> None:
    report = load_model(
        Paths(project).data, "research-output/report.json", ResearchReport
    ).value
    assert report is not None and report.average_theoretical_roi == Decimal(10)
    rows = research_rows(report, None, tour="ATP", minimum_roi=Decimal(9))
    assert len(rows) == 1 and rows[0]["Source"] == "atp_matches_2025.csv:2"
    assert rows[0]["Verification"] == "Unverified historical CSV"
    assert "Realised hypothetical P&L GBP" not in rows[0]
    assert not research_rows(report, None, tournament="missing")
    assert not research_rows(report, None, minimum_roi=Decimal(11))
    assert research_rows(report, None, anomaly="Flagged")
    assert pct(report.average_theoretical_roi) == "10.0000%"
    assert gbp(Decimal("1234.5")) == "£1,234.50"


def test_backtest_open_and_settled_reporting(project: Path) -> None:
    directories = backtest_directories(Paths(project))
    assert len(directories) == 2
    reports = [load_model(d, "report.json", BacktestReport).value for d in directories]
    assert all(reports)
    opened = next(r for r in reports if r is not None and r.metrics.unsettled_positions)
    settled = next(r for r in reports if r is not None and r.metrics.settled_positions)
    assert opened.metrics.unsettled_exposure > 0
    assert opened.metrics.realised_pnl == 0
    assert trade_rows(opened)[0]["Realised hypothetical P&L GBP"] is None
    assert trade_rows(settled)[0]["Realised hypothetical P&L GBP"] is not None
    # An inconsistent legacy record must still not render unbooked profit.
    bad = opened.model_copy(
        update={
            "trades": tuple(
                t.model_copy(update={"realised_profit": Decimal(999)})
                for t in opened.trades
            )
        }
    )
    assert trade_rows(bad)[0]["Realised hypothetical P&L GBP"] is None


@pytest.mark.parametrize(
    "relative", ["../.env", "a/../../secret", "C:/Windows/win.ini"]
)
def test_safe_paths(project: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        confined(Paths(project).data, relative)


def test_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "private-key-value")
    assert text("failure private-key-value") == "failure [redacted]"


def test_all_pages_rerun_offline_and_preserve_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BETTING_DASHBOARD_ROOT", str(project))
    monkeypatch.setenv("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    monkeypatch.setenv("ODDS_API_KEY", "must-never-render-this")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Dashboard must not make HTTP or API requests")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    before = hashes(project)
    tested = AppTest.from_file(app.__file__, default_timeout=30).run()
    assert not tested.exception
    for page in app.PAGES:
        tested.sidebar.radio[0].set_value(page).run()
        assert not tested.exception, page
        assert "must-never-render-this" not in str(tested)
        if page == "Live Odds":
            tested.text_input[0].set_value("25.00").run()
            assert not tested.exception
            assert any(m.value == "£25.00" for m in tested.metric)
            tested.text_input[0].set_value("NaN").run()
            assert tested.warning and not tested.exception
        if page == "Historical Research":
            assert any(
                m.label == "Median theoretical ROI" and m.value == "10.0000%"
                for m in tested.metric
            )
            next(w for w in tested.selectbox if w.label == "Tour").set_value(
                "WTA"
            ).run()
            assert not tested.exception
    tested.sidebar.button[0].click().run()
    assert not tested.exception
    assert hashes(project) == before


@pytest.mark.parametrize("page", app.PAGES)
def test_empty_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    monkeypatch.setenv("BETTING_DASHBOARD_ROOT", str(tmp_path))
    tested = AppTest.from_file(app.__file__, default_timeout=15).run()
    tested.sidebar.radio[0].set_value(page).run()
    assert not tested.exception
    assert tested.info


@pytest.mark.parametrize("page", app.PAGES)
def test_partially_written_files_render_gracefully(
    project: Path, monkeypatch: pytest.MonkeyPatch, page: str
) -> None:
    for path in project.rglob("*.json"):
        path.write_text('{"partial":', encoding="utf-8")
    monkeypatch.setenv("BETTING_DASHBOARD_ROOT", str(project))
    tested = AppTest.from_file(app.__file__, default_timeout=20).run()
    tested.sidebar.radio[0].set_value(page).run()
    assert not tested.exception
    assert tested.info


def test_confined_symlink(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating Windows symlinks needs Developer Mode or elevation")
    with pytest.raises(ValueError):
        confined(project, "escape/report.json")


def test_launcher_localhost_and_telemetry_disabled(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from betting_scanner.interfaces.dashboard import main

    captured: list[list[str]] = []

    def fake_call(command: list[str], *, env: dict[str, str]) -> int:
        captured.append(command)
        assert env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] == "false"
        assert env["BETTING_DASHBOARD_ROOT"] == str(project.resolve())
        return 0

    monkeypatch.setattr(
        "betting_scanner.interfaces.dashboard.subprocess.call", fake_call
    )
    assert main(["--headless", "--port", "8502", "--project-root", str(project)]) == 0
    assert "127.0.0.1" in captured[0] and "8502" in captured[0]
    assert "--server.headless" in captured[0] and "true" in captured[0]
