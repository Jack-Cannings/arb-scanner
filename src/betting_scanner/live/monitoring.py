"""Bounded sequential scheduling around the existing one-shot live scanner."""

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from betting_scanner.config import ConfigurationError
from betting_scanner.live.models import LiveManifest, LiveScanReport
from betting_scanner.live.monitor_models import (
    CandidateArchive,
    CandidateTrack,
    MonitorEstimate,
    MonitoringReport,
    MonitoringScan,
    MonitorPlan,
    StopReason,
)
from betting_scanner.live.scanner import ClientFactory, scan_live
from betting_scanner.live.tracking import track_candidates
from betting_scanner.providers.odds_api.client import (
    BudgetState,
    ClientOptions,
    CreditLedger,
    OddsApiClient,
    OddsApiError,
)
from betting_scanner.storage.files import atomic_write, directory_lock


@dataclass(frozen=True)
class MonitorClock:
    wall: Callable[[], datetime] = lambda: datetime.now(UTC)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _budget(output: Path, initial_cap: int) -> BudgetState:
    path = output / "manifest.json"
    if not path.exists():
        return BudgetState(max_credits=initial_cap)
    return LiveManifest.model_validate_json(path.read_text(encoding="utf-8")).budget


def _scoped(state: BudgetState, scope: str, cap: int) -> BudgetState:
    return BudgetState(
        max_credits=cap, attempts=tuple(a for a in state.attempts if a.scope == scope)
    )


def estimate_monitor(
    plan: MonitorPlan, output: Path, options: ClientOptions | None = None
) -> MonitorEstimate:
    """Read-only estimate: do not create a lock, directory, client or journal."""
    plan = MonitorPlan.model_validate(plan.model_dump())
    options = options or ClientOptions()
    state = _budget(output, plan.max_credits)
    ledger = CreditLedger(state)
    bound = plan.scan.estimate(options.max_retries)
    provider_remaining = next(
        (
            a.quota.remaining
            for a in reversed(state.attempts)
            if a.quota is not None and a.quota.remaining is not None
        ),
        None,
    )
    return MonitorEstimate(
        scheduled_scans=plan.scheduled_scans,
        credits_per_scan_bound=bound,
        maximum_credits=plan.scheduled_scans * bound,
        archive_cap=state.max_credits,
        archive_accounted=state.accounted_credits,
        archive_headroom=max(0, state.max_credits - state.accounted_credits),
        provider_remaining=provider_remaining,
        session_cap=plan.max_credits,
        affordable_scans=min(
            plan.scheduled_scans,
            min(ledger.available_credits, plan.max_credits) // bound,
        ),
        schedule_offsets=tuple(
            i * plan.interval for i in range(min(plan.scheduled_scans, 20))
        ),
    )


def _stop(code: str | None, errors: tuple[str, ...]) -> StopReason | None:
    known = {
        "user_interruption": StopReason.INTERRUPTION,
        "duration_reached": StopReason.DURATION,
        "session_credit_budget": StopReason.SESSION_CREDITS,
        "archive_credit_budget": StopReason.ARCHIVE_CREDITS,
        "api_quota_exhausted": StopReason.API_QUOTA,
        "quota_exhausted": StopReason.API_QUOTA,
        "authentication": StopReason.AUTHENTICATION,
        "access_denied": StopReason.AUTHENTICATION,
    }
    if code in known:
        return known[code]
    return StopReason.PROVIDER_ERROR if code is not None or errors else None


def _next_id(output: Path, budget: BudgetState) -> str:
    numbers = []
    for path in (output / "monitoring").glob("monitor-*.json"):
        match = re.fullmatch(r"monitor-([0-9]+)", path.stem)
        if match:
            numbers.append(int(match[1]))
    for attempt in budget.attempts:
        match = re.fullmatch(r"monitor-([0-9]+)", attempt.scope or "")
        if match:
            numbers.append(int(match[1]))
    return f"monitor-{max(numbers, default=0) + 1:06d}"


def monitor(
    plan: MonitorPlan,
    output: Path,
    *,
    execute: bool = False,
    client_factory: ClientFactory | None = None,
    options: ClientOptions | None = None,
    clock: MonitorClock | None = None,
    on_scan: Callable[[MonitoringScan, MonitoringReport], None] | None = None,
) -> MonitorEstimate | MonitoringReport:
    """Monitor finite limits, never changing an existing archive cap or balance.

    A new directory gets an archive cap equal to the session cap. Discovery-only
    directories with a zero cap remain zero: increase that cap explicitly with
    the existing single-scan workflow before monitoring there.
    """
    plan = MonitorPlan.model_validate(plan.model_dump())
    options = ClientOptions.model_validate((options or ClientOptions()).model_dump())
    if not execute:
        return estimate_monitor(plan, output, options)
    if client_factory is None:
        raise ValueError("Executing monitoring requires an explicit client factory")
    clock = clock or MonitorClock()
    with directory_lock(output, ".monitor.lock"):
        return _run(plan, output, client_factory, options, clock, on_scan)


def _run(
    plan: MonitorPlan,
    output: Path,
    client_factory: ClientFactory,
    options: ClientOptions,
    clock: MonitorClock,
    on_scan: Callable[[MonitoringScan, MonitoringReport], None] | None,
) -> MonitoringReport:
    archive_budget = _budget(output, plan.max_credits)
    scope = _next_id(output, archive_budget)
    tracker_path = output / "candidate-tracking.json"
    tracked = (
        CandidateArchive.model_validate_json(tracker_path.read_text(encoding="utf-8"))
        if tracker_path.exists()
        else CandidateArchive()
    )
    records: dict[str, CandidateTrack] = {
        r.id: r.model_copy(update={"status": "not_checked"}) for r in tracked.candidates
    }
    state = MonitoringReport(
        id=scope,
        plan=plan,
        started_at=clock.wall(),
        session_budget=_scoped(archive_budget, scope, plan.max_credits),
        archive_budget=archive_budget,
    )
    session_path = output / "monitoring" / f"{scope}.json"
    origin = clock.monotonic()
    deadline = origin + plan.duration if plan.duration is not None else None
    slot = 0
    bound = plan.scan.estimate(options.max_retries)

    def checkpoint() -> None:
        nonlocal state
        current = _budget(output, plan.max_credits)
        state = state.model_copy(
            update={
                "session_budget": _scoped(current, scope, plan.max_credits),
                "archive_budget": current,
                "candidates": tuple(records[key] for key in sorted(records)),
            }
        )
        atomic_write(tracker_path, CandidateArchive(candidates=state.candidates))
        atomic_write(session_path, state)

    def guard() -> None:
        if deadline is not None and clock.monotonic() >= deadline:
            raise OddsApiError(
                "duration_reached",
                "Monitoring duration reached before another request.",
            )

    def factory(ledger: CreditLedger) -> OddsApiClient:
        ledger.restrict_session(scope, plan.max_credits)
        ledger.request_guard = guard
        client = client_factory(ledger)
        if client.options.max_retries > options.max_retries:
            client.close()
            raise ValueError("Client retries exceed the monitoring cost estimate")

        def pause(delay: float) -> None:
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - clock.monotonic()))
            if delay > 0:
                clock.sleep(delay)

        client.sleep = pause
        return client

    checkpoint()
    try:
        while True:
            if plan.max_scans is not None and len(state.scans) >= plan.max_scans:
                state = state.model_copy(update={"stop_reason": StopReason.MAX_SCANS})
                break
            now = clock.monotonic()
            if deadline is not None and now >= deadline:
                state = state.model_copy(update={"stop_reason": StopReason.DURATION})
                break
            ledger = CreditLedger(_budget(output, plan.max_credits))
            ledger.restrict_session(scope, plan.max_credits)
            reason = ledger.stop_reason(bound)
            if reason is not None:
                state = state.model_copy(update={"stop_reason": _stop(reason, ())})
                break
            scheduled = origin + slot * plan.interval
            # A suspended computer or delayed sleep can miss slots as well.
            if now >= scheduled + plan.interval:
                next_slot = math.ceil((now - origin) / plan.interval)
                state = state.model_copy(
                    update={"skipped_slots": state.skipped_slots + next_slot - slot}
                )
                slot = next_slot
                scheduled = origin + slot * plan.interval
            if now < scheduled:
                clock.sleep(
                    min(scheduled - now, deadline - now)
                    if deadline is not None
                    else scheduled - now
                )
                continue
            point = MonitoringScan(number=len(state.scans) + 1, started_at=clock.wall())
            state = state.model_copy(update={"scans": (*state.scans, point)})
            checkpoint()  # An interrupted attempt is visible even without a response.
            partial: LiveScanReport | None = None

            def capture(report: LiveScanReport) -> None:
                nonlocal partial
                partial = report

            interrupted = False
            try:
                scan_live(
                    plan.scan,
                    output,
                    max_credits=None
                    if (output / "manifest.json").exists()
                    else plan.max_credits,
                    execute=True,
                    client_factory=factory,
                    clock=clock.wall,
                    reserve_retry_allowance=True,
                    on_report=capture,
                )
            except KeyboardInterrupt:
                interrupted = True
            finally:
                point = point.model_copy(update={"finished_at": clock.wall()})
                if partial is not None:
                    point = track_candidates(records, partial, point)
                    events = {
                        a.event_id
                        for o in partial.observations
                        for a in o.analyses
                        if a.event_id is not None
                    }
                    state = state.model_copy(
                        update={
                            "event_observations": state.event_observations
                            + partial.events_analysed,
                            "unique_events": tuple(
                                sorted(set(state.unique_events) | events)
                            ),
                            "observed_candidates": tuple(
                                sorted(
                                    set(state.observed_candidates)
                                    | set(point.new_candidates)
                                    | set(point.continuing_candidates)
                                )
                            ),
                        }
                    )
                state = state.model_copy(update={"scans": (*state.scans[:-1], point)})
                checkpoint()
            if on_scan is not None:
                on_scan(point, state)
            if interrupted:
                state = state.model_copy(
                    update={"stop_reason": StopReason.INTERRUPTION}
                )
                break
            assert partial is not None
            reason_enum = _stop(partial.stop_code, partial.errors)
            if reason_enum is not None:
                state = state.model_copy(update={"stop_reason": reason_enum})
                break
            if plan.max_scans is not None and len(state.scans) >= plan.max_scans:
                state = state.model_copy(update={"stop_reason": StopReason.MAX_SCANS})
                break
            if deadline is not None and clock.monotonic() >= deadline:
                state = state.model_copy(update={"stop_reason": StopReason.DURATION})
                break
            # Fixed anchor, never previous completion + interval or catch-up bursts.
            next_slot = max(
                slot + 1, math.ceil((clock.monotonic() - origin) / plan.interval)
            )
            state = state.model_copy(
                update={"skipped_slots": state.skipped_slots + next_slot - slot - 1}
            )
            slot = next_slot
    except KeyboardInterrupt:
        state = state.model_copy(update={"stop_reason": StopReason.INTERRUPTION})
    except ConfigurationError as error:
        state = state.model_copy(
            update={"stop_reason": StopReason.AUTHENTICATION, "error": str(error)}
        )
    except (ValueError, OSError, OddsApiError):
        state = state.model_copy(
            update={
                "stop_reason": StopReason.PROVIDER_ERROR,
                "error": "Monitoring stopped on a configuration, archive "
                "or provider error.",
            }
        )
    finally:
        state = state.model_copy(update={"finished_at": clock.wall()})
        checkpoint()
    return state
