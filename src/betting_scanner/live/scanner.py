"""One-shot, explicitly opted-in collection using the shared provider and engine."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from betting_scanner.live.archive import LiveArchive
from betting_scanner.live.models import (
    LiveDiscovery,
    LiveObservation,
    LiveScanPlan,
    LiveScanReport,
)
from betting_scanner.providers.odds_api.client import (
    BudgetState,
    CreditBudgetExceeded,
    CreditLedger,
    OddsApiClient,
    OddsApiError,
)
from betting_scanner.providers.odds_api.schema import ApiSport
from betting_scanner.storage.files import atomic_write, directory_lock
from betting_scanner.strategies.arbitrage.results import ActionabilityStatus

ClientFactory = Callable[[CreditLedger], OddsApiClient]


def _active(client: OddsApiClient, archive: LiveArchive) -> tuple[ApiSport, ...]:
    sports = tuple(
        sorted((s for s in client.sports() if s.is_active_singles), key=lambda s: s.key)
    )
    if len({s.key for s in sports}) != len(sports):
        raise ValueError("Ambiguous duplicate active sport keys")
    atomic_write(
        archive.output / "discovery.json",
        LiveDiscovery(checked_at=client.clock(), sports=sports),
    )
    return sports


def discover_live(
    output: Path,
    client_factory: ClientFactory,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[ApiSport, ...]:
    """Explicit free sport listing; never queries a competition's odds."""
    with directory_lock(output):
        archive = LiveArchive(output, None, clock)
        ledger = CreditLedger(archive.manifest.budget, archive.save_budget)
        archive.recover()
        with client_factory(ledger) as client:
            return _active(client, archive)


def _report(
    plan: LiveScanPlan,
    observations: list[LiveObservation],
    errors: list[str],
    ledger: CreditLedger,
    attempt_start: int,
    stop_code: str | None = None,
) -> LiveScanReport:
    analyses = [a for o in observations for a in o.analyses]
    prematch = [
        s.snapshot
        for o in observations
        for s in o.normalised.snapshots
        if s.snapshot.event.id not in o.excluded_started
    ]
    candidates = [
        a
        for a in analyses
        if a.candidate is not None and a.candidate.mathematical_arbitrage
    ]
    return LiveScanReport(
        plan=plan,
        observations=tuple(observations),
        errors=tuple(errors),
        run_budget=BudgetState(
            max_credits=ledger.state.max_credits,
            attempts=ledger.state.attempts[attempt_start:],
        ),
        archive_budget=ledger.state,
        events_analysed=len({s.event.id for s in prematch}),
        unique_bookmakers=len({q.bookmaker_id for s in prematch for q in s.quotes}),
        quotes_processed=sum(len(s.quotes) for s in prematch),
        mathematical_candidates=len(candidates),
        eligible_opportunities=sum(
            a.actionability_status == ActionabilityStatus.ACTIONABLE for a in analyses
        ),
        unverified_candidates=sum(
            a.actionability_status != ActionabilityStatus.ACTIONABLE for a in candidates
        ),
        rejected_markets=sum(
            a.actionability_status == ActionabilityStatus.REJECTED for a in analyses
        ),
        stop_code=stop_code,
    )


def scan_live(
    plan: LiveScanPlan,
    output: Path,
    *,
    max_credits: int | None = 5,
    execute: bool = False,
    client_factory: ClientFactory | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    reserve_retry_allowance: bool = False,
    on_report: Callable[[LiveScanReport], None] | None = None,
) -> LiveScanReport | None:
    """No-I/O dry run by default. Never reuse an old response as a current quote.

    Explicit keys are checked against active singles discovery before any odds
    request. Credit reservations span scans in this directory, including unknown
    charges after interruption. All requests are sequential and bounded by keys
    and the shared client's retry limit. There is no monitoring or background loop.
    """
    plan = LiveScanPlan.model_validate(plan.model_dump())
    if not execute:
        return None
    if client_factory is None:
        raise ValueError("Executing a live scan requires an explicit client factory")
    with directory_lock(output):
        archive = LiveArchive(output, max_credits, clock)
        ledger = CreditLedger(archive.manifest.budget, archive.save_budget)
        archive.recover()
        attempt_start = len(ledger.state.attempts)
        observations: list[LiveObservation] = []
        errors: list[str] = []
        stop_code: str | None = None
        try:
            with client_factory(ledger) as client:
                try:
                    active_keys = {s.key for s in _active(client, archive)}
                except OddsApiError as error:
                    stop_code = (
                        error.limit_reason
                        if isinstance(error, CreditBudgetExceeded)
                        else error.code
                    )
                    errors.append(
                        "Active competition discovery failed; no odds requested."
                    )
                    active_keys = set()
                except ValueError:
                    stop_code = "malformed"
                    errors.append(
                        "Active competition discovery failed; no odds requested."
                    )
                    active_keys = set()
                for key in plan.sport_keys:
                    if key not in active_keys:
                        errors.append(
                            f"{key}: not confirmed as an active singles competition"
                        )
                        continue
                    allowance = (
                        1 + client.options.max_retries if reserve_retry_allowance else 1
                    )
                    reason = ledger.stop_reason(allowance)
                    if reason is not None:
                        stop_code = reason
                        errors.append(
                            "credit_budget: insufficient local or provider credits; "
                            "scan stopped"
                        )
                        break
                    entry = archive.begin(key, plan)
                    response = None
                    try:
                        response = client.live_odds(key)
                        observation = archive.record(entry, response)
                        observations.append(observation)
                        if (
                            observation.normalised.issues
                            and not observation.normalised.snapshots
                        ):
                            errors.append(
                                f"{key}: no valid events; inspect normalisation issues"
                            )
                    except OddsApiError as error:
                        stop_code = (
                            error.limit_reason
                            if isinstance(error, CreditBudgetExceeded)
                            else error.code
                        )
                        archive.fail(entry, error.code, error.response)
                        errors.append(f"{key}: {error.code}; scan stopped")
                        break
                    except ValueError:
                        archive.fail(
                            entry, "Malformed or conflicting live response", response
                        )
                        errors.append(f"{key}: malformed or conflicting response")
        except KeyboardInterrupt:
            stop_code = "user_interruption"
            errors.append("Scan interrupted; uncertain charges remain reserved.")
            archive.recover()
            raise
        finally:
            archive.rebuild()
            report = _report(
                plan, observations, errors, ledger, attempt_start, stop_code
            )
            atomic_write(output / "scan-report.json", report)
            if on_report is not None:
                on_report(report)
        return report
