"""Small, journalled, credit-bounded historical downloads with offline caching."""

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from betting_scanner.backtesting.models import HistoricalDataset, HistoricalSnapshot
from betting_scanner.core.models import DomainModel, UTCTimestamp
from betting_scanner.providers.odds_api.client import (
    BudgetState,
    CreditLedger,
    HistoricalOddsClient,
    OddsApiError,
)
from betting_scanner.providers.odds_api.normalise import NormalisationIssue, normalise
from betting_scanner.providers.odds_api.schema import (
    ResponseRecord,
    parse_page,
    validate_sport_key,
)


class DownloadPlan(DomainModel):
    sport_key: Annotated[str, AfterValidator(validate_sport_key)]
    start: UTCTimestamp
    end: UTCTimestamp
    interval: Annotated[timedelta, Field(ge=timedelta(minutes=5))] = timedelta(
        minutes=5
    )
    region: Literal["uk", "us", "us2", "eu", "au"] = "uk"
    market: Literal["h2h"] = "h2h"

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.end < self.start:
            raise ValueError("End must be at or after start")
        if (self.end - self.start) // self.interval + 1 > 100:
            raise ValueError(
                "Use a small range: at most 100 requested snapshots per dataset"
            )
        return self

    def dates(self) -> tuple[datetime, ...]:
        return tuple(
            self.start + self.interval * i
            for i in range((self.end - self.start) // self.interval + 1)
        )

    def estimate(self, max_retries: int = 1) -> int:
        return len(self.dates()) * 10 * (max_retries + 1)


class DownloadEntry(DomainModel):
    requested_at: UTCTimestamp
    status: Literal[
        "not_requested",
        "in_flight",
        "complete",
        "empty",
        "duplicate",
        "error",
        "uncertain",
    ]
    raw_file: str | None = None
    actual_timestamp: UTCTimestamp | None = None
    reason: str | None = None


class DatasetManifest(DomainModel):
    format_version: Literal[1] = 1
    provider: Literal["the-odds-api"] = "the-odds-api"
    competition: str
    plan: DownloadPlan
    collected_at: UTCTimestamp
    updated_at: UTCTimestamp
    budget: BudgetState
    entries: dict[str, DownloadEntry] = Field(default_factory=dict)
    number_of_snapshots: int = 0
    number_of_events: int = 0
    number_of_quotes: int = 0
    actual_api_credits: int | None = 0
    accounted_api_credits: int = 0
    missing_snapshots: tuple[DownloadEntry, ...] = ()
    gaps: tuple[tuple[UTCTimestamp, UTCTimestamp], ...] = ()
    normalisation_issues: tuple[NormalisationIssue, ...] = ()
    stop_reason: str | None = None


def _write(path: Path, model: DomainModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _stamp(timestamp: datetime) -> str:
    return timestamp.strftime("%Y%m%dT%H%M%S%fZ")


def _download_unlocked(
    plan: DownloadPlan,
    output: Path,
    *,
    max_credits: int = 100,
    execute: bool = False,
    client_factory: Callable[[CreditLedger], HistoricalOddsClient] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DatasetManifest | None:
    """Dry run has zero I/O. Execute uses a durable reservation for every attempt.

    A request journalled in-flight without a cached response is never retried
    automatically after interruption: its charge and result are uncertain.
    """
    if not execute:
        return None
    if client_factory is None:
        raise ValueError(
            "Explicit client factory required to execute a historical download"
        )
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        manifest = DatasetManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        if manifest.plan != plan:
            raise ValueError(
                "Output directory belongs to a different plan; choose a new directory"
            )
        budget = BudgetState(max_credits=max_credits, attempts=manifest.budget.attempts)
        manifest = manifest.model_copy(update={"budget": budget, "stop_reason": None})
    else:
        now = clock()
        budget = BudgetState(max_credits=max_credits)
        manifest = DatasetManifest(
            competition=plan.sport_key,
            plan=plan,
            collected_at=now,
            updated_at=now,
            budget=budget,
        )

    def save_budget(state: BudgetState) -> None:
        nonlocal manifest
        manifest = manifest.model_copy(
            update={
                "budget": state,
                "actual_api_credits": state.actual_credits,
                "accounted_api_credits": state.accounted_credits,
                "updated_at": clock(),
            }
        )
        _write(manifest_path, manifest)

    ledger = CreditLedger(budget, save_budget)
    cached: dict[datetime, ResponseRecord] = {}
    # Recover raw response files written just before a manifest update/interruption.
    for date in plan.dates():
        path = output / "raw" / f"{_stamp(date)}.json"
        if path.exists():
            cached_record = ResponseRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if cached_record.requested_at != date:
                raise ValueError("Cached request timestamp disagrees with its filename")
            cached[date] = cached_record
    with client_factory(ledger) as client:
        for date in plan.dates():
            key = date.isoformat()
            previous_entry = manifest.entries.get(key)
            response = cached.get(date)
            if (
                response is None
                and previous_entry is not None
                and previous_entry.status != "not_requested"
            ):
                # No resubmission of failed, interrupted or unavailable requests.
                if previous_entry.status == "in_flight":
                    entries = dict(manifest.entries)
                    entries[key] = previous_entry.model_copy(
                        update={
                            "status": "uncertain",
                            "reason": "Interrupted request; charge/result unknown; "
                            "not retried.",
                        }
                    )
                    manifest = manifest.model_copy(update={"entries": entries})
                continue
            if response is None:
                # The provider's neighbouring reference can prove a later query
                # would return a cached snapshot. Do not emit a new replay time.
                for earlier in sorted(cached, reverse=True):
                    old = cached[earlier]
                    if old.status_code != 200:
                        continue
                    try:
                        page = parse_page(old)
                    except ValueError:
                        continue
                    if (
                        page.timestamp <= date
                        and page.next_timestamp is not None
                        and date < page.next_timestamp
                    ):
                        response = old
                        break
            if response is None:
                if ledger.state.accounted_credits + 10 > max_credits:
                    manifest = manifest.model_copy(
                        update={"stop_reason": "credit_budget"}
                    )
                    break
                entries = dict(manifest.entries)
                entries[key] = DownloadEntry(requested_at=date, status="in_flight")
                manifest = manifest.model_copy(update={"entries": entries})
                _write(manifest_path, manifest)
                try:
                    response = client.odds(
                        plan.sport_key, date, plan.region, plan.market
                    )
                except OddsApiError as error:
                    if error.response is not None:
                        response = error.response
                        _write(output / "raw" / f"{_stamp(date)}.json", response)
                        cached[date] = response
                    entries = dict(manifest.entries)
                    entries[key] = DownloadEntry(
                        requested_at=date,
                        status="error" if response else "uncertain",
                        reason=error.code,
                        raw_file=f"raw/{_stamp(date)}.json" if response else None,
                    )
                    manifest = manifest.model_copy(
                        update={"entries": entries, "stop_reason": error.code}
                    )
                    break
                _write(output / "raw" / f"{_stamp(date)}.json", response)
                cached[date] = response
            if response is not None:
                source_date = response.requested_at
                assert source_date is not None
                raw_file = f"raw/{_stamp(source_date)}.json"
                entries = dict(manifest.entries)
                try:
                    if response.status_code != 200:
                        raise ValueError("Cached unsuccessful request")
                    page = parse_page(response)
                    entries[key] = DownloadEntry(
                        requested_at=date,
                        status="empty" if not page.data else "complete",
                        actual_timestamp=page.timestamp,
                        raw_file=raw_file,
                        reason="No events returned" if not page.data else None,
                    )
                except ValueError:
                    entries[key] = DownloadEntry(
                        requested_at=date,
                        status="error",
                        raw_file=raw_file,
                        reason="Malformed historical response",
                    )
                manifest = manifest.model_copy(update={"entries": entries})
                _write(manifest_path, manifest)
    # Rebuild the derived dataset only from cached successful actual timestamps.
    snapshots: dict[tuple[datetime, str], HistoricalSnapshot] = {}
    actuals: set[datetime] = set()
    issues: list[NormalisationIssue] = []
    first_request_for_actual: dict[datetime, datetime] = {}
    payloads: dict[datetime, tuple[dict[str, object], ...]] = {}
    conflicts: set[datetime] = set()
    entries = dict(manifest.entries)
    for key, entry in sorted(entries.items()):
        if entry.raw_file is None or entry.status not in {
            "complete",
            "empty",
            "duplicate",
        }:
            continue
        record = ResponseRecord.model_validate_json(
            _cache_path(output, entry.raw_file).read_text(encoding="utf-8")
        )
        try:
            page = parse_page(record)
            if page.timestamp in first_request_for_actual:
                if page.data != payloads[page.timestamp]:
                    conflicts.add(page.timestamp)
                entries[key] = entry.model_copy(
                    update={"status": "duplicate" if page.data else "empty"}
                )
                continue
            first_request_for_actual[page.timestamp] = entry.requested_at
            payloads[page.timestamp] = page.data
            normalised = normalise(record, plan.sport_key, plan.region)
        except ValueError:
            entries[key] = entry.model_copy(
                update={"status": "error", "reason": "Malformed historical response"}
            )
            continue
        issues.extend(normalised.issues)
        if normalised.snapshots:
            actuals.add(page.timestamp)
        elif page.data:
            entries[key] = entry.model_copy(
                update={"status": "error", "reason": "No valid normalised events"}
            )
        for snapshot in normalised.snapshots:
            snapshots[(snapshot.observed_at, snapshot.snapshot.event.id)] = snapshot
    for date in plan.dates():
        if date.isoformat() not in entries:
            entries[date.isoformat()] = DownloadEntry(
                requested_at=date,
                status="not_requested",
                reason=manifest.stop_reason or "Not requested",
            )
    if conflicts:
        issues.append(
            NormalisationIssue(
                provider_event_id=None,
                reason="Conflicting payloads for identical actual snapshot "
                "timestamps were rejected.",
            )
        )
        for key, entry in entries.items():
            if entry.actual_timestamp in conflicts:
                entries[key] = entry.model_copy(
                    update={"status": "error", "reason": "Conflicting actual snapshot"}
                )
    actuals.difference_update(conflicts)
    ordered = tuple(
        snapshots[key] for key in sorted(snapshots) if key[0] not in conflicts
    )
    if ordered:
        dataset = HistoricalDataset(
            name=f"The Odds API historical {plan.sport_key}",
            description="Historical prices only; event status, outcome verification "
            "and settlement remain unverified. No fill or result evidence.",
            snapshots=ordered,
        )
        _write(output / "replay.json", dataset)
    elif (output / "replay.json").exists():
        (output / "replay.json").unlink()
    times = sorted(actuals)
    manifest = manifest.model_copy(
        update={
            "entries": entries,
            "competition": ordered[0].snapshot.competition.name
            if ordered
            else plan.sport_key,
            "number_of_snapshots": len(actuals),
            "number_of_events": len({s.snapshot.event.id for s in ordered}),
            "number_of_quotes": sum(len(s.snapshot.quotes) for s in ordered),
            "missing_snapshots": tuple(
                e
                for e in entries.values()
                if e.status
                in {"not_requested", "empty", "error", "uncertain", "in_flight"}
            ),
            "gaps": tuple(
                (a, b)
                for a, b in zip(times, times[1:], strict=False)
                if b - a > plan.interval
            ),
            "normalisation_issues": tuple(issues),
        }
    )
    save_budget(ledger.state)
    return manifest


def _cache_path(output: Path, relative: str) -> Path:
    path = (output / relative).resolve()
    if not path.is_relative_to(output.resolve()):
        raise ValueError("Cached file path must stay inside the dataset directory")
    return path


@contextmanager
def _dataset_lock(output: Path) -> Iterator[None]:
    """OS lock releases even on process death; do not race a second downloader."""
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".download.lock").open("a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Dataset is already in use by another download") from None
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def download(
    plan: DownloadPlan,
    output: Path,
    *,
    max_credits: int = 100,
    execute: bool = False,
    client_factory: Callable[[CreditLedger], HistoricalOddsClient] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DatasetManifest | None:
    """Default to a no-I/O dry run; execute one single-writer bounded download."""
    plan = DownloadPlan.model_validate(plan.model_dump())
    if not execute:
        return None
    with _dataset_lock(output):
        return _download_unlocked(
            plan,
            output,
            max_credits=max_credits,
            execute=True,
            client_factory=client_factory,
            clock=clock,
        )
