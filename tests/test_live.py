import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from betting_scanner.backtesting.models import ReplayConfig
from betting_scanner.backtesting.replay import run_replay
from betting_scanner.config import Settings
from betting_scanner.interfaces import live
from betting_scanner.interfaces.backtest import main as backtest_main
from betting_scanner.live.models import (
    LiveManifest,
    LiveScanPlan,
    LiveScanReport,
)
from betting_scanner.live.reporting import scan_summary
from betting_scanner.live.scanner import discover_live, scan_live
from betting_scanner.providers.odds_api.client import (
    ClientOptions,
    OddsApiClient,
)
from betting_scanner.providers.odds_api.normalise import normalise_live
from betting_scanner.providers.odds_api.reader import SavedHistoricalReader
from betting_scanner.providers.odds_api.schema import QuotaHeaders, ResponseRecord
from betting_scanner.storage.files import directory_lock
from betting_scanner.strategies.arbitrage.results import ReasonCode

FIXTURES = Path(__file__).parent / "fixtures" / "live"
NOW = datetime(2024, 7, 1, 12, tzinfo=UTC)
ATP = "tennis_atp_fictional_open"
WTA = "tennis_wta_fictional_open"
SECRET = "dummy-live-credential-not-real"
QUOTA = {
    "x-requests-last": "1",
    "x-requests-used": "100",
    "x-requests-remaining": "900",
}


def odds():
    return json.loads((FIXTURES / "odds.json").read_text(), parse_float=Decimal)


def sports():
    return json.loads((FIXTURES / "sports.json").read_text())


def handler_for(calls, data=None, listing=None, *, headers=None, status=200):
    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200,
                json=sports() if listing is None else listing,
                headers={**QUOTA, "x-requests-last": "0"},
            )
        if status != 200:
            return httpx.Response(
                status, json={"message": SECRET}, headers=headers or QUOTA
            )
        payload = odds() if data is None else json.loads(json.dumps(data, default=str))
        key = request.url.path.split("/")[-2]
        for event in payload:
            event["sport_key"] = key
        return httpx.Response(
            200,
            text=json.dumps(payload, default=str),
            headers=QUOTA if headers is None else headers,
        )

    return handler


def factory(handler, *, clock=lambda: NOW, retries=0):
    return lambda ledger: OddsApiClient(
        Settings(odds_api_key=SECRET),
        ledger,
        options=ClientOptions(max_retries=retries),
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=lambda _: None,
    )


def run(path, data=None, **kwargs):
    calls = []
    report = scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        path,
        execute=True,
        client_factory=factory(handler_for(calls, data=data, **kwargs)),
        clock=lambda: NOW,
    )
    return report, calls


def test_active_singles_discovery_is_dynamic_and_free(tmp_path: Path) -> None:
    calls = []
    listing = discover_live(tmp_path, factory(handler_for(calls)), clock=lambda: NOW)
    assert [s.key for s in listing] == [ATP, WTA]
    assert len(calls) == 1 and calls[0].url.path.endswith("/sports")
    manifest = LiveManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text()
    )
    assert manifest.budget.actual_credits == 0
    assert manifest.requests == ()


@pytest.mark.parametrize(
    "listing",
    [
        [],
        [{"key": ATP, "group": "Tennis", "title": "Unknown format", "active": True}],
        [
            {
                "key": ATP,
                "group": "Tennis",
                "title": "Doubles",
                "description": "Men's Doubles",
                "active": True,
            }
        ],
    ],
)
def test_no_confirmed_active_singles_does_not_scan(tmp_path: Path, listing) -> None:
    calls = []
    report = scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        execute=True,
        client_factory=factory(handler_for(calls, listing=listing)),
        clock=lambda: NOW,
    )
    assert len(calls) == 1 and report.events_analysed == 0
    assert report.errors and report.run_budget.actual_credits == 0


def test_single_scan_uses_existing_engine_and_preserves_all_quotes(
    tmp_path: Path,
) -> None:
    report, calls = run(tmp_path)
    assert len(calls) == 2
    assert calls[1].url.path == f"/v4/sports/{ATP}/odds"
    assert dict(calls[1].url.params) == {
        "regions": "uk",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    assert (
        report.events_analysed == 1
        and report.unique_bookmakers == 3
        and report.quotes_processed == 6
    )
    assert report.mathematical_candidates == report.unverified_candidates == 1
    assert report.eligible_opportunities == 0
    calculation = report.observations[0].analyses[0].candidate
    assert calculation is not None
    assert {leg.quote.decimal_odds: leg.stake for leg in calculation.legs} == {
        Decimal("2.20"): Decimal("52.17"),
        Decimal("2.40"): Decimal("47.83"),
    }
    assert calculation.worst_case_profit == Decimal("14.77")
    assert sum((leg.stake for leg in calculation.legs), Decimal(0)) == Decimal("100")
    observation = report.observations[0]
    assert observation.observed_at == NOW
    quotes = observation.normalised.snapshots[0].snapshot.quotes
    assert all(q.collected_at == q.observed_at == NOW for q in quotes)
    assert all(
        q.settlement_rule.category == "unknown" and not q.settlement_rule.verified
        for q in quotes
    )
    assert all(q.metadata["actual_snapshot"] is None for q in quotes)
    assert report.run_budget.actual_credits == 1


def test_terminal_candidate_contains_manual_investigation_details(
    tmp_path: Path,
) -> None:
    report, _ = run(tmp_path)
    output = scan_summary(report)
    for text in [
        "Fictional ATP Open",
        "Fictional Ada Ace",
        "Fictional Bea Baseline",
        "Start:",
        "Fictional One",
        "odds=2.2",
        "hypothetical stake=GBP 52.17",
        "Theoretical ROI=14.7826%",
        "profit=GBP 14.77",
        "age=60s",
        "updated=",
        "verification=unverified",
        "unverified_settlement",
        "Eligible opportunities: 0",
    ]:
        assert text in output


def test_multiple_explicit_keys_and_duplicate_selection(tmp_path: Path) -> None:
    calls = []
    plan = LiveScanPlan(sport_keys=(WTA, ATP, ATP))
    report = scan_live(
        plan,
        tmp_path,
        execute=True,
        max_credits=2,
        client_factory=factory(handler_for(calls)),
        clock=lambda: NOW,
    )
    assert len(calls) == 3 and report.events_analysed == 2
    assert report.unique_bookmakers == 3 and report.quotes_processed == 12
    assert report.run_budget.actual_credits == 2


@pytest.mark.parametrize("offset,analysed", [(-1, 0), (0, 0), (1, 1)])
def test_started_and_starting_soon_filter(
    tmp_path: Path, offset: int, analysed: int
) -> None:
    data = odds()
    data[0]["commence_time"] = (NOW + timedelta(seconds=offset)).isoformat()
    report, _ = run(tmp_path, data)
    assert report.events_analysed == analysed
    assert len(report.observations[0].excluded_started) == 1 - analysed
    assert len(report.observations[0].normalised.snapshots[0].snapshot.quotes) == 6
    if not analysed:
        with pytest.raises(ValueError, match="no valid pre-match"):
            SavedHistoricalReader(tmp_path).read()


def test_filter_uses_receipt_time_after_network_delay(tmp_path: Path) -> None:
    data = odds()
    data[0]["commence_time"] = (NOW + timedelta(seconds=1)).isoformat()
    ticks = iter((NOW, NOW, NOW + timedelta(seconds=2)))
    report = scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        execute=True,
        client_factory=factory(handler_for([], data=data), clock=lambda: next(ticks)),
        clock=lambda: NOW,
    )
    assert report.events_analysed == 0


def test_stale_updates_excluded_and_never_refreshed_by_collection(
    tmp_path: Path,
) -> None:
    data = odds()
    for book in data[0]["bookmakers"]:
        book["last_update"] = "2024-07-01T11:00:00Z"
    report, _ = run(tmp_path, data)
    assert report.mathematical_candidates == 0
    assert ReasonCode.STALE_QUOTE in {
        r.code for r in report.observations[0].analyses[0].reasons
    }


def test_missing_provider_times_still_only_unverified_candidate(tmp_path: Path) -> None:
    data = odds()
    for book in data[0]["bookmakers"]:
        book.pop("last_update")
    report, _ = run(tmp_path, data)
    assert report.mathematical_candidates == 1 and report.eligible_opportunities == 0
    assert "freshness unverified" in scan_summary(report)


def test_missing_outcome_does_not_invent_coverage(tmp_path: Path) -> None:
    data = odds()
    for book in data[0]["bookmakers"]:
        book["markets"][0]["outcomes"] = book["markets"][0]["outcomes"][:1]
    report, _ = run(tmp_path, data)
    assert report.quotes_processed == 3 and report.mathematical_candidates == 0
    assert report.rejected_markets == 1


@pytest.mark.parametrize("price", ["0", "1", "NaN", "Infinity", "invalid"])
def test_invalid_prices_are_archived_but_never_opportunities(
    tmp_path: Path, price: str
) -> None:
    data = odds()
    data[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = price
    report, _ = run(tmp_path, data)
    assert report.events_analysed == report.mathematical_candidates == 0
    assert report.observations[0].normalised.issues
    assert len(list((tmp_path / "raw").glob("*.json"))) == 1


def test_zero_opportunities_summary_is_useful(tmp_path: Path) -> None:
    data = odds()
    for book in data[0]["bookmakers"]:
        for outcome in book["markets"][0]["outcomes"]:
            outcome["price"] = "1.80"
    report, _ = run(tmp_path, data)
    assert report.mathematical_candidates == 0
    assert "Pre-match events analysed: 1" in scan_summary(report)
    assert "Mathematical candidates: 0" in scan_summary(report)


def test_empty_response_saved_without_fake_replay_data(tmp_path: Path) -> None:
    report, _ = run(tmp_path, [], headers={**QUOTA, "x-requests-last": "0"})
    assert report.events_analysed == 0 and report.run_budget.actual_credits == 0
    manifest = LiveManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text()
    )
    assert len(manifest.observations) == 1
    assert manifest.requests[0].status == "empty"


def test_malformed_response_saved_safely(tmp_path: Path) -> None:
    def handler(request):
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200, json=sports(), headers={**QUOTA, "x-requests-last": "0"}
            )
        return httpx.Response(200, text="malformed", headers=QUOTA)

    report = scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        execute=True,
        client_factory=factory(handler),
        clock=lambda: NOW,
    )
    assert report.errors and report.events_analysed == 0
    assert (tmp_path / "raw" / "request-000001.json").exists()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication"),
        (403, "access_denied"),
        (429, "rate_limited"),
        (503, "http_error"),
    ],
)
def test_errors_preserve_quota_and_never_keys(
    tmp_path: Path, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        report, calls = run(tmp_path, status=status)
    assert code in report.errors[0]
    assert report.archive_budget.actual_credits == 1
    assert len(calls) == 2
    assert SECRET not in report.model_dump_json() and SECRET not in caplog.text
    assert SECRET not in (tmp_path / "raw" / "request-000001.json").read_text()


def test_retry_allowance_and_actual_cost_are_separate(tmp_path: Path) -> None:
    calls = []
    base = handler_for(calls)
    paid = 0

    def handler(request):
        nonlocal paid
        if not request.url.path.endswith("/sports"):
            paid += 1
            if paid == 1:
                calls.append(request)
                return httpx.Response(429, headers={**QUOTA, "x-requests-last": "0"})
        return base(request)

    report = scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        max_credits=2,
        execute=True,
        client_factory=factory(handler, retries=1),
        clock=lambda: NOW,
    )
    assert paid == 2 and report.archive_budget.actual_credits == 1
    assert len(report.run_budget.attempts) == 3


def test_budget_spans_competitions_and_repeated_commands(tmp_path: Path) -> None:
    calls = []
    plan = LiveScanPlan(sport_keys=(ATP, WTA))
    report = scan_live(
        plan,
        tmp_path,
        max_credits=1,
        execute=True,
        client_factory=factory(handler_for(calls)),
        clock=lambda: NOW,
    )
    assert len(calls) == 2 and report.events_analysed == 1
    assert "credit_budget" in report.errors[-1]
    again = scan_live(
        plan,
        tmp_path,
        max_credits=1,
        execute=True,
        client_factory=factory(handler_for(calls)),
        clock=lambda: NOW,
    )
    assert len(calls) == 3 and again.events_analysed == 0
    assert again.archive_budget.actual_credits == 1


def test_provider_remaining_quota_prevents_request_even_with_local_headroom(
    tmp_path: Path,
) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json=sports(),
            headers={**QUOTA, "x-requests-last": "0", "x-requests-remaining": "0"},
        )

    report = scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        max_credits=5,
        execute=True,
        client_factory=factory(handler),
        clock=lambda: NOW,
    )
    assert len(calls) == 1 and report.archive_budget.accounted_credits == 0
    assert report.errors


def test_missing_quota_keeps_a_conservative_reservation(tmp_path: Path) -> None:
    report, _ = run(tmp_path, headers={})
    assert report.archive_budget.actual_credits is None
    assert report.archive_budget.accounted_credits == 1


def test_dry_run_never_constructs_client_or_writes_files(tmp_path: Path) -> None:
    def forbidden(_):
        raise AssertionError("No client on dry run")

    output = tmp_path / "unused"
    assert (
        scan_live(LiveScanPlan(sport_keys=(ATP,)), output, client_factory=forbidden)
        is None
    )
    assert not output.exists()


def test_duplicate_observations_are_not_duplicated_in_replay(tmp_path: Path) -> None:
    report, _ = run(tmp_path)
    run(tmp_path)
    manifest = LiveManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text()
    )
    assert len(manifest.requests) == 2 and len(manifest.observations) == 1
    assert manifest.requests[-1].status == "duplicate"
    assert manifest.budget.actual_credits == 2
    assert len(SavedHistoricalReader(tmp_path).read().snapshots) == 1
    assert len(list((tmp_path / "observations").glob("*.json"))) == 1
    assert LiveScanReport.model_validate_json(report.model_dump_json()) == report


def test_same_time_conflicting_payloads_are_not_replayable(tmp_path: Path) -> None:
    run(tmp_path)
    data = odds()
    data[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = "2.30"
    report, _ = run(tmp_path, data)
    assert report.errors
    manifest = LiveManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text()
    )
    assert manifest.conflicted_observations and not manifest.observations
    assert not (tmp_path / "replay.json").exists()


def test_genuinely_new_observation_time_preserved_even_when_prices_unchanged(
    tmp_path: Path,
) -> None:
    run(tmp_path)
    calls = []
    later = NOW + timedelta(seconds=30)
    scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        execute=True,
        client_factory=factory(handler_for(calls), clock=lambda: later),
        clock=lambda: later,
    )
    snapshots = SavedHistoricalReader(tmp_path).read().snapshots
    assert [s.observed_at for s in snapshots] == [NOW, later]
    assert all(q.collected_at == later for q in snapshots[-1].snapshot.quotes)


def test_offline_replay_does_not_call_api_or_invent_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("No HTTP for replay")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    dataset = SavedHistoricalReader(tmp_path).read()
    first = run_replay(dataset, ReplayConfig(execution_delay=timedelta(seconds=30)))
    assert first == run_replay(
        dataset, ReplayConfig(execution_delay=timedelta(seconds=30))
    )
    assert first.metrics.mathematical_candidates == 1
    assert first.metrics.execution_attempts == first.metrics.realised_pnl == 0
    assert backtest_main([str(tmp_path), "--output-dir", str(tmp_path / "report")]) == 0


def test_interruption_is_reserved_and_recovery_does_not_invent_new_receipt(
    tmp_path: Path,
) -> None:
    def handler(request):
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200, json=sports(), headers={**QUOTA, "x-requests-last": "0"}
            )
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        scan_live(
            LiveScanPlan(sport_keys=(ATP,)),
            tmp_path,
            max_credits=1,
            execute=True,
            client_factory=factory(handler),
            clock=lambda: NOW,
        )
    calls = []
    report = scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        max_credits=1,
        execute=True,
        client_factory=factory(handler_for(calls)),
        clock=lambda: NOW,
    )
    assert len(calls) == 1 and report.archive_budget.accounted_credits == 1
    assert report.archive_budget.actual_credits is None
    manifest = LiveManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text()
    )
    assert manifest.requests[0].status == "uncertain"


def test_raw_receipt_recovery_uses_original_timestamp(tmp_path: Path) -> None:
    run(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = LiveManifest.model_validate_json(path.read_text())
    pending = manifest.requests[0].model_copy(
        update={"status": "in_flight", "observation_id": None}
    )
    manifest = manifest.model_copy(update={"requests": (pending,), "observations": {}})
    path.write_text(manifest.model_dump_json())
    discover_live(
        tmp_path, factory(handler_for([])), clock=lambda: NOW + timedelta(hours=1)
    )
    dataset = SavedHistoricalReader(tmp_path).read()
    assert len(dataset.snapshots) == 1 and dataset.snapshots[0].observed_at == NOW


def test_archive_lock_rejects_concurrent_scan(tmp_path: Path) -> None:
    with directory_lock(tmp_path), pytest.raises(ValueError, match="already in use"):
        run(tmp_path)


def test_cli_defaults_to_free_discovery_and_scan_requires_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []

    def client(settings, ledger, options):
        return factory(handler_for(calls))(ledger)

    monkeypatch.setattr(live, "OddsApiClient", client)
    monkeypatch.chdir(tmp_path)
    assert live.main([]) == 0
    assert (
        len(calls) == 1
        and "match odds availability not checked" in capsys.readouterr().out
    )
    assert (
        live.main(["scan", "--sport-key", ATP, "--output-dir", str(tmp_path / "dry")])
        == 0
    )
    assert len(calls) == 1 and not (tmp_path / "dry").exists()
    assert "Dry run" in capsys.readouterr().out
    assert (
        live.main(
            [
                "scan",
                "--sport-key",
                ATP,
                "--stake",
                "10",
                "--max-credits",
                "2",
                "--output-dir",
                str(tmp_path / "scan"),
                "--execute",
            ]
        )
        == 0
    )
    assert len(calls) == 3 and "Mathematical candidates: 1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "update",
    [
        {"sport_keys": ()},
        {"stake": "0"},
        {"stake": "1.001"},
        {"sport_keys": ("soccer_fake",)},
        {"engine": {"max_quote_age": timedelta(0)}},
    ],
)
def test_invalid_scan_plan_rejected(update) -> None:
    data = {"sport_keys": (ATP,)}
    data.update(update)
    with pytest.raises(ValidationError):
        LiveScanPlan.model_validate(data)


def test_numeric_live_json_keeps_decimal_precision() -> None:
    precise = "2.2000000000000000000000000000000000000001"
    text = (
        (FIXTURES / "odds.json")
        .read_text()
        .replace('"price": 2.2', f'"price": {precise}')
    )
    response = ResponseRecord(
        endpoint=f"sports/{ATP}/odds",
        requested_at=None,
        collected_at=NOW,
        status_code=200,
        headers={},
        quota=QuotaHeaders(),
        body=text,
    )
    page = normalise_live(response, ATP)
    assert (
        page.snapshots[0].snapshot.quotes[0].decimal_odds.as_tuple()
        == Decimal(precise).as_tuple()
    )


def test_rounding_can_remove_tiny_live_candidate_profit(tmp_path: Path) -> None:
    data = odds()
    for book in data[0]["bookmakers"]:
        for outcome in book["markets"][0]["outcomes"]:
            outcome["price"] = "2.00001"
    report, _ = run(tmp_path, data)
    calculation = report.observations[0].analyses[0].candidate
    assert calculation.mathematical_arbitrage and calculation.theoretical_profit > 0
    assert calculation.worst_case_profit == 0
    assert report.eligible_opportunities == 0


def test_missing_credentials_in_cli_make_no_http_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def client(settings, ledger, options):
        return OddsApiClient(
            settings,
            ledger,
            options=options,
            transport=httpx.MockTransport(handler_for(calls)),
        )

    monkeypatch.setattr(live, "OddsApiClient", client)
    monkeypatch.setattr(live, "load_settings", lambda: Settings())
    with pytest.raises(SystemExit) as error:
        live.main(
            ["scan", "--sport-key", ATP, "--output-dir", str(tmp_path), "--execute"]
        )
    assert error.value.code == 2 and calls == []
