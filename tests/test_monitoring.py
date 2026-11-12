import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from betting_scanner.backtesting.replay import run_replay
from betting_scanner.config import Settings
from betting_scanner.interfaces import live
from betting_scanner.live.models import LiveManifest, LiveScanPlan
from betting_scanner.live.monitor_models import (
    MonitorEstimate,
    MonitoringReport,
    MonitorPlan,
    StopReason,
)
from betting_scanner.live.monitor_reporting import (
    completion_summary,
    estimate_summary,
    monitoring_scan_summary,
)
from betting_scanner.live.monitoring import MonitorClock, estimate_monitor, monitor
from betting_scanner.live.scanner import scan_live
from betting_scanner.providers.odds_api.client import ClientOptions, OddsApiClient
from betting_scanner.providers.odds_api.reader import SavedHistoricalReader
from betting_scanner.storage.files import directory_lock

FIXTURES = Path(__file__).parent / "fixtures" / "live"
NOW = datetime(2024, 7, 1, 12, tzinfo=UTC)
ATP = "tennis_atp_fictional_open"
WTA = "tennis_wta_fictional_open"


@dataclass
class FakeTime:
    elapsed: float = 0.0
    sleeps: list[float] = field(default_factory=list)
    interrupt_wait: bool = False

    def wall(self) -> datetime:
        return NOW + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        if self.interrupt_wait:
            raise KeyboardInterrupt()
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def clock(self) -> MonitorClock:
        return MonitorClock(wall=self.wall, monotonic=self.monotonic, sleep=self.sleep)


class FakeAPI:
    def __init__(
        self,
        time: FakeTime,
        variants: tuple[str, ...] = (),
        latency: float = 0,
        retries: int = 0,
    ) -> None:
        self.time = time
        self.variants = variants
        self.latency = latency
        self.retries = retries
        self.starts: list[float] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {
            "x-requests-last": "0",
            "x-requests-remaining": "900",
            "x-requests-used": "100",
        }
        if request.url.path.endswith("/sports"):
            return httpx.Response(
                200,
                json=json.loads((FIXTURES / "sports.json").read_text()),
                headers=headers,
            )
        self.starts.append(self.time.elapsed)
        self.time.elapsed += self.latency
        variant = (
            self.variants[min(len(self.starts) - 1, len(self.variants) - 1)]
            if self.variants
            else "arb"
        )
        headers["x-requests-last"] = "1"
        if variant == "interrupt":
            raise KeyboardInterrupt()
        if variant in {"auth", "rate", "server"}:
            status = {"auth": 401, "rate": 429, "server": 503}[variant]
            if variant == "rate":
                headers["x-requests-last"] = "0"
            return httpx.Response(
                status, json={"message": "mock failure"}, headers=headers
            )
        if variant == "backoff":
            return httpx.Response(
                429, headers={**headers, "x-requests-last": "0", "retry-after": "5"}
            )
        if variant == "empty":
            return httpx.Response(
                200, json=[], headers={**headers, "x-requests-last": "0"}
            )
        if variant == "quota":
            headers["x-requests-remaining"] = "0"
        if variant == "unexpected_cost":
            headers["x-requests-last"] = "2"
        data = json.loads((FIXTURES / "odds.json").read_text(), parse_float=Decimal)
        for event in data:
            event["sport_key"] = request.url.path.split("/")[-2]
            for book in event["bookmakers"]:
                book["last_update"] = self.time.wall().isoformat()
                if variant == "malformed":
                    book["markets"][0]["outcomes"][0]["price"] = "invalid"
                elif variant == "none":
                    for outcome in book["markets"][0]["outcomes"]:
                        outcome["price"] = "1.80"
                elif variant == "price_change" and book["key"] == "fictional_one":
                    book["markets"][0]["outcomes"][0]["price"] = "2.21"
                elif variant == "book_change" and book["key"] == "fictional_three":
                    book["markets"][0]["outcomes"][0]["price"] = "2.30"
        return httpx.Response(200, text=json.dumps(data, default=str), headers=headers)

    def client(self, ledger):
        return OddsApiClient(
            Settings(odds_api_key="dummy-monitor-key"),
            ledger,
            options=ClientOptions(max_retries=self.retries),
            transport=httpx.MockTransport(self.handler),
            clock=self.time.wall,
            sleep=self.time.sleep,
        )


def plan(**kwargs) -> MonitorPlan:
    return MonitorPlan(
        scan=LiveScanPlan(sport_keys=(ATP,)),
        interval=10,
        max_scans=kwargs.pop("max_scans", 3),
        **kwargs,
    )


def execute(
    output: Path, config: MonitorPlan, api: FakeAPI, **kwargs
) -> MonitoringReport:
    result = monitor(
        config,
        output,
        execute=True,
        client_factory=api.client,
        options=ClientOptions(max_retries=api.retries),
        clock=api.time.clock(),
        **kwargs,
    )
    assert isinstance(result, MonitoringReport)
    return result


def test_fixed_start_intervals_and_max_scans(tmp_path: Path) -> None:
    clock = FakeTime()
    api = FakeAPI(clock, latency=2)
    report = execute(tmp_path, plan(), api)
    assert api.starts == [0, 10, 20]
    assert clock.sleeps == [8, 8]
    assert len(report.scans) == 3 and report.stop_reason == StopReason.MAX_SCANS
    assert report.event_observations == 3 and len(report.unique_events) == 1
    assert report.session_budget.actual_credits == 3


def test_slow_scans_skip_slots_without_overlap_or_catchup(tmp_path: Path) -> None:
    clock = FakeTime()
    api = FakeAPI(clock, latency=25)
    report = execute(tmp_path, plan(), api)
    assert api.starts == [0, 30, 60]
    assert all(
        later >= earlier + 25
        for earlier, later in zip(api.starts, api.starts[1:], strict=False)
    )
    assert clock.sleeps == [5, 5]
    assert report.skipped_slots == 4


def test_oversleep_skips_old_slots_instead_of_launching_a_burst(tmp_path: Path) -> None:
    clock = FakeTime()
    api = FakeAPI(clock)
    waits = []

    def oversleep(seconds: float) -> None:
        waits.append(seconds)
        clock.elapsed += seconds + (20 if len(waits) == 1 else 0)

    result = monitor(
        plan(),
        tmp_path,
        execute=True,
        client_factory=api.client,
        options=ClientOptions(max_retries=0),
        clock=MonitorClock(wall=clock.wall, monotonic=clock.monotonic, sleep=oversleep),
    )
    assert api.starts == [0, 30, 40]
    assert result.skipped_slots == 2


def test_wall_clock_adjustments_do_not_change_scheduling(tmp_path: Path) -> None:
    class AdjustedTime(FakeTime):
        def wall(self) -> datetime:
            return NOW - timedelta(seconds=self.elapsed)

    api = FakeAPI(AdjustedTime())
    report = execute(tmp_path, plan(), api)
    assert api.starts == [0, 10, 20]
    assert report.stop_reason == StopReason.MAX_SCANS


@pytest.mark.parametrize(
    "duration,expected", [(1, [0]), (10, [0]), (20, [0, 10]), (21, [0, 10, 20])]
)
def test_duration_limit_never_starts_at_deadline(
    tmp_path: Path, duration: float, expected: list[int]
) -> None:
    api = FakeAPI(FakeTime())
    report = execute(tmp_path, plan(max_scans=None, duration=duration), api)
    assert api.starts == expected
    assert report.stop_reason == StopReason.DURATION


def test_session_cap_and_archive_cap_are_separate(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime())
    scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        execute=True,
        max_credits=5,
        client_factory=api.client,
        clock=api.time.wall,
    )
    original = LiveManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text()
    )
    assert original.budget.accounted_credits == 1
    report = execute(tmp_path, plan(max_credits=1), api)
    assert report.stop_reason == StopReason.SESSION_CREDITS
    assert report.session_budget.actual_credits == 1
    assert report.archive_budget.actual_credits == 2
    assert report.archive_budget.max_credits == 5
    assert (
        report.archive_budget.attempts[: len(original.budget.attempts)]
        == original.budget.attempts
    )


def test_existing_archive_cap_is_never_automatically_increased(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime())
    scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        execute=True,
        max_credits=1,
        client_factory=api.client,
        clock=api.time.wall,
    )
    before = (tmp_path / "manifest.json").read_bytes()
    report = execute(tmp_path, plan(max_credits=10), api)
    assert report.stop_reason == StopReason.ARCHIVE_CREDITS
    assert len(api.starts) == 1 and len(report.scans) == 0
    assert (tmp_path / "manifest.json").read_bytes() == before


def test_conservative_retry_allowance_must_fit_before_scan(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), retries=1)
    report = execute(tmp_path, plan(max_credits=1), api)
    assert report.stop_reason == StopReason.SESSION_CREDITS
    assert api.requests == []


def test_retry_charges_remain_inside_session_limit(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("rate", "arb", "arb"), retries=1)
    report = execute(tmp_path, plan(max_scans=2, max_credits=3), api)
    assert report.stop_reason == StopReason.MAX_SCANS
    assert api.starts == [0, 1, 10]
    assert report.session_budget.actual_credits == 2


def test_provider_quota_exhaustion_stops_without_waiting(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("quota",))
    report = execute(tmp_path, plan(), api)
    assert report.stop_reason == StopReason.API_QUOTA
    assert len(api.starts) == 1 and api.time.sleeps == []


def test_unexpected_provider_cost_stops_even_if_local_cap_has_room(
    tmp_path: Path,
) -> None:
    api = FakeAPI(FakeTime(), ("unexpected_cost",))
    report = execute(tmp_path, plan(), api)
    assert report.stop_reason == StopReason.PROVIDER_ERROR
    assert report.session_budget.actual_credits == 2
    assert len(api.starts) == 1


def test_authentication_failure_never_retries_monitoring(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("auth",), retries=2)
    report = execute(tmp_path, plan(), api)
    assert report.stop_reason == StopReason.AUTHENTICATION
    assert len(api.starts) == 1 and len(report.scans) == 1


def test_duration_truncates_backoff_and_prevents_new_request(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("backoff",), retries=1)
    report = execute(tmp_path, plan(duration=2), api)
    assert report.stop_reason == StopReason.DURATION
    assert api.time.sleeps == [2]
    assert len(api.starts) == 1


def test_no_later_competition_request_after_deadline(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), latency=3)
    config = MonitorPlan(
        scan=LiveScanPlan(sport_keys=(ATP, WTA)), interval=10, duration=2
    )
    report = execute(tmp_path, config, api)
    assert report.stop_reason == StopReason.DURATION
    assert len(api.starts) == 1
    assert report.scans[0].report.events_analysed == 1


def test_ctrl_c_during_wait_stops_and_persists_report(tmp_path: Path) -> None:
    clock = FakeTime(interrupt_wait=True)
    api = FakeAPI(clock)
    report = execute(tmp_path, plan(), api)
    assert report.stop_reason == StopReason.INTERRUPTION
    assert len(api.starts) == 1
    saved = MonitoringReport.model_validate_json(
        (tmp_path / "monitoring" / f"{report.id}.json").read_text()
    )
    assert saved == report and saved.session_budget.actual_credits == 1


def test_ctrl_c_mid_scan_preserves_partial_observations_and_uncertain_charge(
    tmp_path: Path,
) -> None:
    api = FakeAPI(FakeTime(), ("arb", "interrupt"))
    points = []
    config = MonitorPlan(scan=LiveScanPlan(sport_keys=(ATP, WTA)), interval=10)
    report = execute(
        tmp_path, config, api, on_scan=lambda point, state: points.append(point)
    )
    assert report.stop_reason == StopReason.INTERRUPTION
    assert report.session_budget.actual_credits is None
    assert report.session_budget.accounted_credits == 2
    assert len(points) == 1 and points[0].report.events_analysed == 1
    assert len(SavedHistoricalReader(tmp_path).read().snapshots) == 1
    manifest = LiveManifest.model_validate_json(
        (tmp_path / "manifest.json").read_text()
    )
    assert manifest.requests[-1].status == "uncertain"


def test_new_continuing_and_disappearing_opportunities(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("arb", "price_change", "none"))
    report = execute(tmp_path, plan(), api)
    first, second, third = report.scans
    assert len(first.new_candidates) == 1
    assert second.continuing_candidates == first.new_candidates
    assert not second.new_candidates
    assert third.no_longer_observed == first.new_candidates
    assert len(report.observed_candidates) == 1
    candidate = report.candidates[0]
    assert candidate.first_observed_at == NOW
    assert candidate.most_recent_observed_at == NOW + timedelta(seconds=10)
    assert candidate.last_checked_at == NOW + timedelta(seconds=20)
    assert candidate.status == "no_longer_observed"
    assert candidate.verification_status == "unverified"


def test_changed_bookmaker_combination_creates_new_identity(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("arb", "book_change"))
    report = execute(tmp_path, plan(max_scans=2), api)
    assert len(report.observed_candidates) == 2
    assert report.scans[0].new_candidates != report.scans[1].new_candidates
    assert report.scans[1].no_longer_observed == report.scans[0].new_candidates


def test_reappearing_combination_is_not_a_second_discovery(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("arb", "none", "arb"))
    report = execute(tmp_path, plan(), api)
    assert report.scans[-1].continuing_candidates == report.scans[0].new_candidates
    assert not report.scans[-1].new_candidates
    assert len(report.observed_candidates) == 1


def test_malformed_observation_does_not_prove_disappearance(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime(), ("arb", "malformed"))
    report = execute(tmp_path, plan(), api)
    assert report.stop_reason == StopReason.PROVIDER_ERROR
    assert not report.scans[-1].no_longer_observed
    assert report.scans[-1].not_checked == report.scans[0].new_candidates
    assert report.candidates[0].status == "not_checked"


def test_empty_observation_proves_no_candidate_observed_and_costs_zero(
    tmp_path: Path,
) -> None:
    api = FakeAPI(FakeTime(), ("arb", "empty"))
    report = execute(tmp_path, plan(max_scans=2), api)
    assert report.session_budget.actual_credits == 1
    assert report.scans[-1].no_longer_observed == report.scans[0].new_candidates


def test_tracking_continues_across_sessions_without_recounting_new(
    tmp_path: Path,
) -> None:
    api = FakeAPI(FakeTime())
    first = execute(tmp_path, plan(max_scans=1, max_credits=5), api)
    api.time.elapsed = 10
    second = execute(tmp_path, plan(max_scans=1, max_credits=5), api)
    assert first.id != second.id
    assert not second.scans[0].new_candidates
    assert second.scans[0].continuing_candidates == first.scans[0].new_candidates
    assert second.session_budget.actual_credits == 1
    assert second.archive_budget.actual_credits == 2


def test_session_ids_are_not_reused_when_old_reports_are_removed(
    tmp_path: Path,
) -> None:
    api = FakeAPI(FakeTime())
    first = execute(tmp_path, plan(max_scans=1, max_credits=5), api)
    (tmp_path / "monitoring" / f"{first.id}.json").unlink()
    api.time.elapsed = 10
    second = execute(tmp_path, plan(max_scans=1, max_credits=5), api)
    assert first.id != second.id
    assert second.session_budget.actual_credits == 1
    assert second.archive_budget.actual_credits == 2


def test_manual_scan_between_iterations_does_not_count_as_session_spend(
    tmp_path: Path,
) -> None:
    api = FakeAPI(FakeTime())
    # Establish an archive cap independently, without making a request.
    from betting_scanner.live.archive import LiveArchive

    LiveArchive(tmp_path, 3, api.time.wall).save()
    interleaved = False

    def wait(seconds: float) -> None:
        nonlocal interleaved
        api.time.sleep(seconds)
        if not interleaved:
            interleaved = True
            scan_live(
                LiveScanPlan(sport_keys=(ATP,)),
                tmp_path,
                execute=True,
                max_credits=None,
                client_factory=api.client,
                clock=api.time.wall,
            )

    report = monitor(
        plan(max_credits=2),
        tmp_path,
        execute=True,
        client_factory=api.client,
        options=ClientOptions(max_retries=0),
        clock=MonitorClock(
            wall=api.time.wall, monotonic=api.time.monotonic, sleep=wait
        ),
    )
    assert report.session_budget.actual_credits == 2
    assert report.archive_budget.actual_credits == 3
    assert len(report.scans) == 2


def test_observations_archive_and_replay_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeAPI(FakeTime())
    report = execute(tmp_path, plan(), api)

    def forbidden(*args, **kwargs):
        raise AssertionError("No network in replay")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    dataset = SavedHistoricalReader(tmp_path).read()
    assert len(dataset.snapshots) == 3
    assert [s.observed_at for s in dataset.snapshots] == [
        NOW,
        NOW + timedelta(seconds=10),
        NOW + timedelta(seconds=20),
    ]
    replay = run_replay(dataset)
    assert (
        replay.metrics.mathematical_candidates == 1
    )  # Existing replay dedup is retained.
    assert replay.metrics.execution_attempts == replay.metrics.realised_pnl == 0
    assert MonitoringReport.model_validate_json(report.model_dump_json()) == report


def test_deterministic_results_with_injected_time(tmp_path: Path) -> None:
    first = execute(tmp_path / "a", plan(), FakeAPI(FakeTime()))
    second = execute(tmp_path / "b", plan(), FakeAPI(FakeTime()))
    assert first == second


def test_dry_run_reads_old_usage_but_writes_nothing(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime())
    scan_live(
        LiveScanPlan(sport_keys=(ATP,)),
        tmp_path,
        execute=True,
        max_credits=5,
        client_factory=api.client,
        clock=api.time.wall,
    )
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    estimate = monitor(plan(max_scans=10), tmp_path)
    assert isinstance(estimate, MonitorEstimate)
    assert estimate.archive_accounted == 1 and estimate.archive_cap == 5
    assert estimate.affordable_scans == 2
    assert len(api.starts) == 1
    after = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert before == after


def test_dry_run_on_missing_directory_creates_no_files(tmp_path: Path) -> None:
    target = tmp_path / "unused"
    estimate = monitor(plan(), target)
    assert estimate.scheduled_scans == 3 and estimate.maximum_credits == 6
    assert not target.exists()
    assert "+10s" in estimate_summary(estimate)


def test_duration_and_max_scans_estimate_earliest_limit(tmp_path: Path) -> None:
    config = plan(duration=21, max_scans=20)
    assert estimate_monitor(config, tmp_path).scheduled_scans == 3
    assert (
        estimate_monitor(plan(duration=100, max_scans=2), tmp_path).scheduled_scans == 2
    )


def test_monitor_lock_prevents_second_monitor(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime())
    with (
        directory_lock(tmp_path, ".monitor.lock"),
        pytest.raises(ValueError, match="already in use"),
    ):
        execute(tmp_path, plan(), api)
    assert api.requests == []


def test_output_contains_scan_and_session_metrics(tmp_path: Path) -> None:
    api = FakeAPI(FakeTime())
    report = execute(tmp_path, plan(max_scans=1), api)
    output = monitoring_scan_summary(report.scans[0], report)
    for text in (
        "Scan 1",
        "New=1",
        "remaining=9",
        "Fictional Ada Ace",
        "hypothetical stake",
        "verification=unverified",
    ):
        assert text in output
    assert "maximum_scans_reached" in completion_summary(report, tmp_path)


def test_cli_monitor_dry_run_has_no_http_or_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("No HTTP client in dry run")

    monkeypatch.setattr(live, "OddsApiClient", forbidden)
    output = tmp_path / "dry"
    assert live.main(["monitor", "--sport-key", ATP, "--output-dir", str(output)]) == 0
    text = capsys.readouterr().out
    assert (
        "Scheduled scans: 10" in text
        and "20 credits" in text
        and "Dry run only" in text
    )
    assert not output.exists()


def test_cli_monitor_single_mock_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeAPI(FakeTime())
    monkeypatch.setattr(
        live, "OddsApiClient", lambda settings, ledger, options: api.client(ledger)
    )
    assert (
        live.main(
            [
                "monitor",
                "--sport-key",
                ATP,
                "--max-scans",
                "1",
                "--output-dir",
                str(tmp_path),
                "--execute",
            ]
        )
        == 0
    )
    assert "Monitoring complete" in capsys.readouterr().out
    assert len(api.starts) == 1


@pytest.mark.parametrize(
    "update",
    [
        {"interval": 0},
        {"interval": -1},
        {"interval": float("nan")},
        {"duration": 0},
        {"duration": float("inf")},
        {"max_scans": 0},
        {"max_scans": None, "duration": None},
        {"max_credits": -1},
    ],
)
def test_invalid_monitoring_config(update) -> None:
    data = {"scan": LiveScanPlan(sport_keys=(ATP,))}
    data.update(update)
    with pytest.raises(ValidationError):
        MonitorPlan.model_validate(data)
