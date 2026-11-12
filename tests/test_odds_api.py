import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from betting_scanner.backtesting.models import BacktestReport
from betting_scanner.backtesting.replay import run_replay
from betting_scanner.config import ConfigurationError, Settings
from betting_scanner.interfaces import history
from betting_scanner.interfaces.backtest import main as backtest_main
from betting_scanner.providers.odds_api.client import (
    BudgetState,
    ClientOptions,
    CreditBudgetExceeded,
    CreditLedger,
    HistoricalOddsClient,
    OddsApiError,
)
from betting_scanner.providers.odds_api.download import (
    DatasetManifest,
    DownloadPlan,
    _dataset_lock,
    download,
)
from betting_scanner.providers.odds_api.normalise import normalise
from betting_scanner.providers.odds_api.reader import SavedHistoricalReader
from betting_scanner.providers.odds_api.schema import (
    QuotaHeaders,
    ResponseRecord,
    parse_page,
)
from betting_scanner.strategies.arbitrage import detect_arbitrage
from betting_scanner.strategies.arbitrage.results import ActionabilityStatus, ReasonCode

FIXTURES = Path(__file__).parent / "fixtures" / "odds_api"
SPORT = "tennis_atp_fictional_open"
ACTUAL = datetime(2024, 7, 1, 12, tzinfo=UTC)
REQUESTED = ACTUAL + timedelta(minutes=2)
COLLECTED = datetime(2026, 11, 12, 12, tzinfo=UTC)
SECRET = "dummy-credential-not-a-real-api-key"
HEADERS = {
    "x-requests-last": "10",
    "x-requests-used": "50",
    "x-requests-remaining": "950",
}


def body() -> str:
    return (FIXTURES / "historical.json").read_text(encoding="utf-8")


def payload() -> dict[str, object]:
    return json.loads(body(), parse_float=Decimal)


def record(data: dict[str, object] | None = None) -> ResponseRecord:
    return ResponseRecord(
        endpoint=f"historical/sports/{SPORT}/odds",
        requested_at=REQUESTED,
        collected_at=COLLECTED,
        status_code=200,
        headers=HEADERS,
        quota=QuotaHeaders(last=10, used=50, remaining=950),
        body=body() if data is None else json.dumps(data, default=str),
    )


def plan(end: datetime = REQUESTED) -> DownloadPlan:
    return DownloadPlan(sport_key=SPORT, start=REQUESTED, end=end)


def factory(handler, retries: int = 0):
    return lambda ledger: HistoricalOddsClient(
        Settings(odds_api_key=SECRET),
        ledger,
        options=ClientOptions(max_retries=retries),
        transport=httpx.MockTransport(handler),
        clock=lambda: COLLECTED,
        sleep=lambda _: None,
    )


def timed_handler(calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        date = datetime.fromisoformat(request.url.params["date"])
        actual = date.replace(minute=(date.minute // 5) * 5, second=0)
        data = payload()
        data.update(
            timestamp=actual.isoformat(),
            previous_timestamp=(actual - timedelta(minutes=5)).isoformat(),
            next_timestamp=(actual + timedelta(minutes=5)).isoformat(),
        )
        return httpx.Response(200, text=json.dumps(data, default=str), headers=HEADERS)

    return handler


def test_precise_normalisation_and_all_timestamps() -> None:
    result = normalise(record(), SPORT, "uk")
    assert not result.issues
    snapshot = result.snapshots[0]
    assert snapshot.observed_at == ACTUAL
    assert len(snapshot.snapshot.bookmakers) == 3
    assert len(snapshot.snapshot.quotes) == 6
    quote = snapshot.snapshot.quotes[0]
    assert quote.decimal_odds == Decimal("2.20")
    assert quote.observed_at == ACTUAL
    assert quote.collected_at == COLLECTED
    assert quote.metadata["requested_snapshot"] == REQUESTED.isoformat()
    assert quote.provider_updated_at == ACTUAL - timedelta(minutes=1)
    assert quote.provider_event_reference == "fictional-provider-event-01"
    assert snapshot.snapshot.event.provider_references == {
        "the-odds-api": "fictional-provider-event-01"
    }
    assert snapshot.snapshot.event.scheduled_start == ACTUAL + timedelta(hours=2)
    assert snapshot.snapshot.event.status == "unknown"
    assert not snapshot.snapshot.markets[0].mutually_exclusive
    assert snapshot.snapshot.markets[0].period is None
    assert all(not q.settlement_rule.verified for q in snapshot.snapshot.quotes)


def test_numeric_json_precision_preserved() -> None:
    precise = "2.2000000000000000000000000000000000000001"
    source = record().model_copy(
        update={"body": body().replace('"price": 2.20', f'"price": {precise}')}
    )
    quote = normalise(source, SPORT, "uk").snapshots[0].snapshot.quotes[0]
    assert quote.decimal_odds.as_tuple() == Decimal(precise).as_tuple()


def test_stable_ids_do_not_depend_on_book_or_outcome_order() -> None:
    original = normalise(record(), SPORT, "uk").snapshots[0].snapshot
    data = payload()
    event = data["data"][0]
    event["home_team"], event["away_team"] = event["away_team"], event["home_team"]
    event["bookmakers"].reverse()
    for book in event["bookmakers"]:
        book["markets"][0]["outcomes"].reverse()
    changed = normalise(record(data), SPORT, "uk").snapshots[0].snapshot
    assert changed.event.id == original.event.id
    assert changed.markets[0].id == original.markets[0].id
    assert {q.outcome_id for q in changed.quotes} == {
        q.outcome_id for q in original.quotes
    }


def test_different_provider_events_never_merge_on_participant_names() -> None:
    data = payload()
    data["data"][0]["id"] = "another-event"
    first = normalise(record(), SPORT, "uk").snapshots[0].snapshot
    second = normalise(record(data), SPORT, "uk").snapshots[0].snapshot
    assert first.event.id != second.event.id
    assert first.markets[0].id != second.markets[0].id


def test_missing_outcomes_are_not_fabricated() -> None:
    data = payload()
    for book in data["data"][0]["bookmakers"]:
        book["markets"][0]["outcomes"] = book["markets"][0]["outcomes"][:1]
    snapshot = normalise(record(data), SPORT, "uk").snapshots[0].snapshot
    assert len(snapshot.quotes) == 3
    result = detect_arbitrage(
        snapshot,
        snapshot.markets[0].id,
        total_stake=Decimal("100"),
        evaluated_at=ACTUAL,
    )
    assert result.candidate is None
    assert ReasonCode.MISSING_OUTCOME_QUOTES in {r.code for r in result.reasons}


@pytest.mark.parametrize("price", ["0", "1", "-1", "NaN", "Infinity", "broken"])
def test_invalid_prices_reject_entire_event_with_issue(price: str) -> None:
    data = payload()
    data["data"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = price
    result = normalise(record(data), SPORT, "uk")
    assert result.snapshots == ()
    assert result.issues


@pytest.mark.parametrize(
    "change",
    [
        "unknown_outcome",
        "duplicate_outcome",
        "point",
        "duplicate_book",
        "duplicate_event",
        "wrong_sport",
        "same_players",
    ],
)
def test_ambiguous_provider_data_cannot_create_opportunities(change: str) -> None:
    data = payload()
    event = data["data"][0]
    outcomes = event["bookmakers"][0]["markets"][0]["outcomes"]
    if change == "unknown_outcome":
        outcomes[0]["name"] = "Draw"
    elif change == "duplicate_outcome":
        outcomes.append(outcomes[0])
    elif change == "point":
        outcomes[0]["point"] = "1.5"
    elif change == "duplicate_book":
        event["bookmakers"].append(event["bookmakers"][0])
    elif change == "duplicate_event":
        data["data"].append(event)
    elif change == "wrong_sport":
        event["sport_key"] = "soccer_other"
    else:
        event["away_team"] = event["home_team"]
    result = normalise(record(data), SPORT, "uk")
    assert not result.snapshots and result.issues


def test_non_h2h_markets_not_relabelled_match_winner() -> None:
    data = payload()
    for book in data["data"][0]["bookmakers"]:
        book["markets"][0]["key"] = "h2h_lay"
    result = normalise(record(data), SPORT, "uk")
    assert result.snapshots[0].snapshot.quotes == ()


def test_missing_and_market_specific_updates_preserved() -> None:
    data = payload()
    book = data["data"][0]["bookmakers"][0]
    book.pop("last_update")
    assert (
        normalise(record(data), SPORT, "uk")
        .snapshots[0]
        .snapshot.quotes[0]
        .provider_updated_at
        is None
    )
    book["markets"][0]["last_update"] = ACTUAL.isoformat()
    quote = normalise(record(data), SPORT, "uk").snapshots[0].snapshot.quotes[0]
    assert quote.provider_updated_at == ACTUAL
    assert quote.metadata["bookmaker_last_update"] is None


@pytest.mark.parametrize(
    "change", ["future_actual", "invalid_previous", "invalid_next"]
)
def test_invalid_snapshot_timing_rejected(change: str) -> None:
    data = payload()
    if change == "future_actual":
        data["timestamp"] = "2024-07-01T12:03:00Z"
    elif change == "invalid_previous":
        data["previous_timestamp"] = data["timestamp"]
    else:
        data["next_timestamp"] = data["timestamp"]
    with pytest.raises(ValueError):
        parse_page(record(data))


def test_current_download_time_does_not_leak_into_historical_decisions() -> None:
    snapshot = normalise(record(), SPORT, "uk").snapshots[0]
    from betting_scanner.backtesting.models import HistoricalDataset

    dataset = HistoricalDataset(
        name="Mock API history",
        description="Synthetic provider response",
        snapshots=(snapshot,),
    )
    report = run_replay(dataset)
    assert report.metrics.mathematical_candidates == 1
    assert report.metrics.execution_attempts == 0
    assert report.metrics.realised_pnl == report.metrics.unsettled_exposure == 0
    detection = report.observations[0].detection
    assert detection.actionability_status == ActionabilityStatus.MATHEMATICAL_CANDIDATE
    assert ReasonCode.FUTURE_QUOTE not in {r.code for r in detection.reasons}


def test_future_archive_observation_is_not_available_early() -> None:
    data = normalise(record(), SPORT, "uk").snapshots[0].snapshot.model_dump()
    for quote in data["quotes"]:
        quote["observed_at"] = ACTUAL + timedelta(minutes=1)
    from betting_scanner.core.models import OddsSnapshot

    result = detect_arbitrage(
        OddsSnapshot.model_validate(data),
        data["markets"][0]["id"],
        total_stake=Decimal("100"),
        evaluated_at=ACTUAL,
    )
    assert result.candidate is None


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication"),
        (403, "historical_access"),
        (404, "unavailable"),
        (429, "rate_limited"),
        (500, "http_error"),
    ],
)
def test_http_failures_are_safe_and_quota_is_tracked(status: int, code: str) -> None:
    ledger = CreditLedger(BudgetState(max_credits=10))
    with (
        factory(
            lambda _: httpx.Response(
                status, text=f'{{"message":"{SECRET}"}}', headers=HEADERS
            )
        )(ledger) as client,
        pytest.raises(OddsApiError) as error,
    ):
        client.odds(SPORT, REQUESTED)
    assert error.value.code == code
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value.response)
    assert ledger.state.actual_credits == 10


def test_paid_plan_error_code_on_401() -> None:
    with (
        factory(
            lambda _: httpx.Response(
                401, json={"error_code": "HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN"}
            )
        )(CreditLedger(BudgetState(max_credits=10))) as client,
        pytest.raises(OddsApiError) as error,
    ):
        client.odds(SPORT, REQUESTED)
    assert error.value.code == "historical_access"


def test_api_key_is_header_only_and_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[httpx.Request] = []
    with (
        caplog.at_level(logging.DEBUG),
        factory(timed_handler(calls))(
            CreditLedger(BudgetState(max_credits=10))
        ) as client,
    ):
        client.odds(SPORT, REQUESTED)
    assert calls[0].headers["x-api-key"] == SECRET
    assert "apiKey" not in calls[0].url.params
    assert SECRET not in caplog.text
    assert calls[0].url.params["oddsFormat"] == "decimal"
    assert calls[0].url.params["regions"] == "uk"


def test_rate_limit_retries_are_bounded_and_budgeted() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            429 if len(calls) == 1 else 200, text=body(), headers=HEADERS
        )

    ledger = CreditLedger(BudgetState(max_credits=20))
    with factory(handler, retries=1)(ledger) as client:
        client.odds(SPORT, REQUESTED)
    assert len(calls) == 2 and ledger.state.actual_credits == 20
    calls.clear()
    ledger = CreditLedger(BudgetState(max_credits=10))
    with (
        factory(handler, retries=1)(ledger) as client,
        pytest.raises(CreditBudgetExceeded),
    ):
        client.odds(SPORT, REQUESTED)
    assert len(calls) == 1


def test_timeout_not_retried_and_unknown_charge_reserved() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout(f"secret url {SECRET}")

    ledger = CreditLedger(BudgetState(max_credits=100))
    with (
        factory(handler, retries=3)(ledger) as client,
        pytest.raises(OddsApiError) as error,
    ):
        client.odds(SPORT, REQUESTED)
    assert SECRET not in str(error.value)
    assert len(calls) == 1 and ledger.state.accounted_credits == 10
    assert ledger.state.actual_credits is None


def test_missing_quota_is_not_reported_as_zero_charge() -> None:
    ledger = CreditLedger(BudgetState(max_credits=10))
    with factory(lambda _: httpx.Response(200, text=body()))(ledger) as client:
        client.odds(SPORT, REQUESTED)
    assert ledger.state.actual_credits is None
    assert ledger.state.accounted_credits == 10


def test_missing_credentials_do_not_trigger_http() -> None:
    with (
        pytest.raises(ConfigurationError),
        HistoricalOddsClient(Settings(), CreditLedger(BudgetState(max_credits=10))),
    ):
        pass


def test_dry_run_makes_no_requests_or_files(tmp_path: Path) -> None:
    def forbidden(_):
        raise AssertionError("No client during dry run")

    output = tmp_path / "unused"
    assert download(plan(), output, client_factory=forbidden) is None
    assert not output.exists()
    assert plan().estimate() == 20


def test_download_budget_and_resume(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    request_plan = plan(REQUESTED + timedelta(minutes=10))
    first = download(
        request_plan,
        tmp_path,
        execute=True,
        max_credits=20,
        client_factory=factory(timed_handler(calls)),
    )
    assert first.stop_reason == "credit_budget"
    assert len(calls) == first.number_of_snapshots == 2
    assert first.actual_api_credits == first.accounted_api_credits == 20
    assert any(e.status == "not_requested" for e in first.missing_snapshots)
    resumed = download(
        request_plan,
        tmp_path,
        execute=True,
        max_credits=30,
        client_factory=factory(timed_handler(calls)),
    )
    assert len(calls) == resumed.number_of_snapshots == 3
    assert resumed.actual_api_credits == 30
    before = SavedHistoricalReader(tmp_path).read()
    download(
        request_plan,
        tmp_path,
        execute=True,
        max_credits=30,
        client_factory=factory(timed_handler(calls)),
    )
    assert len(calls) == 3
    assert SavedHistoricalReader(tmp_path).read() == before


def test_interrupted_request_is_not_resubmitted(tmp_path: Path) -> None:
    def interrupted(_):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        download(
            plan(),
            tmp_path,
            execute=True,
            max_credits=10,
            client_factory=factory(interrupted),
        )

    def forbidden(_):
        raise AssertionError("Uncertain request must not be retried")

    resumed = download(
        plan(),
        tmp_path,
        execute=True,
        max_credits=10,
        client_factory=factory(forbidden),
    )
    assert resumed.actual_api_credits is None and resumed.accounted_api_credits == 10
    assert resumed.missing_snapshots[0].status == "uncertain"


def test_provider_next_reference_avoids_duplicate_request(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        data = payload()
        data["next_timestamp"] = "2024-07-01T12:10:00Z"
        return httpx.Response(200, text=json.dumps(data, default=str), headers=HEADERS)

    manifest = download(
        plan(REQUESTED + timedelta(minutes=5)),
        tmp_path,
        execute=True,
        max_credits=20,
        client_factory=factory(handler),
    )
    assert len(calls) == manifest.number_of_snapshots == 1
    assert any(e.status == "duplicate" for e in manifest.entries.values())
    assert len(SavedHistoricalReader(tmp_path).read().snapshots) == 1


@pytest.mark.parametrize("conflict", [False, True])
def test_identical_actual_timestamp_deduplicates_or_rejects_conflict(
    tmp_path: Path, conflict: bool
) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        data = payload()
        data["next_timestamp"] = None
        if conflict and len(calls) > 1:
            data["data"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = (
                "2.30"
            )
        return httpx.Response(200, text=json.dumps(data, default=str), headers=HEADERS)

    manifest = download(
        plan(REQUESTED + timedelta(minutes=5)),
        tmp_path,
        execute=True,
        max_credits=20,
        client_factory=factory(handler),
    )
    assert len(calls) == 2
    assert manifest.number_of_snapshots == (0 if conflict else 1)
    if conflict:
        assert manifest.normalisation_issues


def test_empty_snapshot_records_gap_and_does_not_interpolate(tmp_path: Path) -> None:
    calls = []
    standard = timed_handler(calls)

    def handler(request):
        response = standard(request)
        if request.url.params["date"].startswith("2024-07-01T12:07"):
            data = json.loads(response.text)
            data["data"] = []
            return httpx.Response(
                200, json=data, headers={**HEADERS, "x-requests-last": "0"}
            )
        return response

    manifest = download(
        plan(REQUESTED + timedelta(minutes=10)),
        tmp_path,
        execute=True,
        max_credits=30,
        client_factory=factory(handler),
    )
    assert manifest.number_of_snapshots == 2
    assert len(manifest.gaps) == 1 and any(
        e.status == "empty" for e in manifest.missing_snapshots
    )
    assert manifest.actual_api_credits == 20
    assert len(SavedHistoricalReader(tmp_path).read().snapshots) == 2


def test_malformed_raw_response_is_cached_and_never_retried(tmp_path: Path) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="not-json", headers=HEADERS)

    manifest = download(
        plan(), tmp_path, execute=True, max_credits=10, client_factory=factory(handler)
    )
    assert (
        manifest.number_of_snapshots == 0
        and manifest.missing_snapshots[0].status == "error"
    )
    download(
        plan(), tmp_path, execute=True, max_credits=10, client_factory=factory(handler)
    )
    assert len(calls) == 1
    with pytest.raises(ValueError, match="no valid historical"):
        SavedHistoricalReader(tmp_path).read()


def test_downloaded_data_replays_offline_and_reproducibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    download(
        plan(),
        tmp_path,
        execute=True,
        max_credits=10,
        client_factory=factory(timed_handler(calls)),
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("No HTTP during offline replay")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    dataset = SavedHistoricalReader(tmp_path).read()
    assert run_replay(dataset) == run_replay(dataset)
    assert run_replay(dataset).metrics.mathematical_candidates == 1
    assert run_replay(dataset).metrics.execution_attempts == 0
    assert backtest_main([str(tmp_path), "--output-dir", str(tmp_path / "report")]) == 0


def test_separate_result_evidence_works_for_existing_synthetic_data(
    tmp_path: Path,
) -> None:
    source = Path(__file__).parent / "fixtures" / "histories" / "a_success.json"
    data = json.loads(source.read_text())
    results = data.pop("settlements")
    (tmp_path / "dataset.json").write_text(json.dumps(data))
    (tmp_path / "results.json").write_text(json.dumps(results))
    backtest_main(
        [
            str(tmp_path / "dataset.json"),
            "--results",
            str(tmp_path / "results.json"),
            "--output-dir",
            str(tmp_path / "report"),
        ]
    )
    report = BacktestReport.model_validate_json(
        (tmp_path / "report" / "report.json").read_text()
    )
    assert report.metrics.realised_pnl == Decimal("14.20")


def test_cli_estimate_and_download_dry_run_without_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "--sport-key",
        SPORT,
        "--start",
        REQUESTED.isoformat(),
        "--end",
        REQUESTED.isoformat(),
    ]
    assert history.main(["estimate", *args]) == 0
    assert (
        history.main(["download", *args, "--output-dir", str(tmp_path / "unused")]) == 0
    )
    assert "Dry run" in capsys.readouterr().out
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("execute", [False, True])
def test_cli_dynamic_sports_and_bounded_event_discovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], execute: bool
) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200,
                text=(FIXTURES / "sports.json").read_text(),
                headers={"x-requests-last": "0"},
            )
        return httpx.Response(200, text=body(), headers={"x-requests-last": "1"})

    def client(settings, ledger, options):
        return factory(handler)(ledger)

    monkeypatch.setattr(history, "HistoricalOddsClient", client)
    args = ["discover", "--date", REQUESTED.isoformat()]
    if execute:
        args.extend(["--sport-key", SPORT, "--execute"])
    assert history.main(args) == 0
    output = capsys.readouterr().out
    assert "historical=not checked" in output
    assert "tennis_wta_fictional_open" in output and "soccer_fictional" not in output
    assert len(calls) == (2 if execute else 1)
    if execute:
        assert "historical events=1" in output


@pytest.mark.parametrize(
    "update",
    [
        {"sport_key": "tennis_made_up"},
        {"end": "2024-06-01T00:00:00Z"},
        {"end": "2026-01-01T00:00:00Z"},
        {"interval": timedelta(minutes=1)},
        {"region": "uk,us"},
        {"market": "totals"},
    ],
)
def test_invalid_or_excessive_download_plan(update: dict[str, object]) -> None:
    data = plan().model_dump()
    data.update(update)
    with pytest.raises(ValidationError):
        DownloadPlan.model_validate(data)


def test_raw_cache_recovers_after_interruption_before_manifest_commit(
    tmp_path: Path,
) -> None:
    calls = []
    download(
        plan(),
        tmp_path,
        execute=True,
        max_credits=10,
        client_factory=factory(timed_handler(calls)),
    )
    path = tmp_path / "manifest.json"
    manifest = DatasetManifest.model_validate_json(path.read_text())
    entry = next(iter(manifest.entries.values())).model_copy(
        update={"status": "in_flight"}
    )
    manifest = manifest.model_copy(update={"entries": {REQUESTED.isoformat(): entry}})
    path.write_text(manifest.model_dump_json())
    resumed = download(
        plan(),
        tmp_path,
        execute=True,
        max_credits=10,
        client_factory=factory(timed_handler(calls)),
    )
    assert len(calls) == 1 and resumed.number_of_snapshots == 1
    assert resumed.actual_api_credits == 10


def test_concurrent_writer_is_rejected_before_paid_request(tmp_path: Path) -> None:
    calls = []
    with _dataset_lock(tmp_path), pytest.raises(ValueError, match="already in use"):
        download(
            plan(), tmp_path, execute=True, client_factory=factory(timed_handler(calls))
        )
    assert calls == []


def test_resuming_different_plan_never_reuses_another_competition(
    tmp_path: Path,
) -> None:
    calls = []
    download(
        plan(),
        tmp_path,
        execute=True,
        max_credits=10,
        client_factory=factory(timed_handler(calls)),
    )
    changed = plan().model_copy(update={"sport_key": "tennis_wta_fictional_open"})
    with pytest.raises(ValueError, match="different plan"):
        download(
            changed,
            tmp_path,
            execute=True,
            client_factory=factory(timed_handler(calls)),
        )
    assert len(calls) == 1


def test_no_credit_headroom_produces_no_paid_request(tmp_path: Path) -> None:
    calls = []
    manifest = download(
        plan(),
        tmp_path,
        execute=True,
        max_credits=9,
        client_factory=factory(timed_handler(calls)),
    )
    assert calls == []
    assert manifest.stop_reason == "credit_budget"
    assert manifest.actual_api_credits == 0
    assert manifest.missing_snapshots[0].status == "not_requested"


def test_unexpected_provider_charge_stops_after_one_response(tmp_path: Path) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, text=body(), headers={**HEADERS, "x-requests-last": "15"}
        )

    manifest = download(
        plan(REQUESTED + timedelta(minutes=10)),
        tmp_path,
        execute=True,
        max_credits=10,
        client_factory=factory(handler),
    )
    assert len(calls) == 1
    assert manifest.stop_reason == "unexpected_cost"
    assert manifest.actual_api_credits == 15


def test_client_server_failure_backoff_is_bounded() -> None:
    delays = []
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={**HEADERS, "retry-after": "1000"})

    ledger = CreditLedger(BudgetState(max_credits=30))
    with (
        HistoricalOddsClient(
            Settings(odds_api_key=SECRET),
            ledger,
            options=ClientOptions(max_retries=2),
            transport=httpx.MockTransport(handler),
            sleep=delays.append,
        ) as client,
        pytest.raises(OddsApiError),
    ):
        client.odds(SPORT, REQUESTED)
    assert len(calls) == 3 and delays == [30.0, 30.0]


def test_empty_discovery_does_not_confirm_coverage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request):
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200,
                text=(FIXTURES / "sports.json").read_text(),
                headers={"x-requests-last": "0"},
            )
        empty = payload()
        empty["data"] = []
        return httpx.Response(
            200, text=json.dumps(empty, default=str), headers={"x-requests-last": "0"}
        )

    monkeypatch.setattr(
        history,
        "HistoricalOddsClient",
        lambda settings, ledger, options: factory(handler)(ledger),
    )
    assert (
        history.main(
            [
                "discover",
                "--date",
                REQUESTED.isoformat(),
                "--sport-key",
                SPORT,
                "--execute",
            ]
        )
        == 0
    )
    assert "historical events=0" in capsys.readouterr().out
