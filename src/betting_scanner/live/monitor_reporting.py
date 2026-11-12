"""Monitoring schedule, per-scan lifecycle counts and final summaries."""

from pathlib import Path

from betting_scanner.live.monitor_models import (
    MonitorEstimate,
    MonitoringReport,
    MonitoringScan,
)
from betting_scanner.live.reporting import scan_summary


def estimate_summary(estimate: MonitorEstimate) -> str:
    offsets = ", ".join(f"+{offset:g}s" for offset in estimate.schedule_offsets)
    return "\n".join(
        (
            f"Scheduled scans: {estimate.scheduled_scans}; start offsets: {offsets}",
            f"Retry-inclusive bound: {estimate.credits_per_scan_bound} credits/scan; "
            f"requested schedule: {estimate.maximum_credits} credits.",
            f"Archive: {estimate.archive_accounted} already accounted; "
            f"cap={estimate.archive_cap}; "
            f"headroom={estimate.archive_headroom}.",
            f"Session cap={estimate.session_cap}; "
            f"provider reported remaining={estimate.provider_remaining}.",
            f"Worst-case full scans affordable: "
            f"{estimate.affordable_scans}/{estimate.scheduled_scans}. "
            "Unused retry allowances may permit more successful scans.",
            "Existing archive cap and usage are preserved. "
            "No network or files in dry run.",
        )
    )


def monitoring_scan_summary(point: MonitoringScan, session: MonitoringReport) -> str:
    report = point.report
    observed = (
        ", ".join(o.observed_at.isoformat() for o in report.observations)
        if report
        else "unavailable"
    )
    lines = [f"Scan {point.number} | observation time: {observed or 'unavailable'}"]
    if report is not None:
        lines.append(
            "Observed competitions: "
            + (", ".join(o.sport_key for o in report.observations) or "none")
        )
        lines.append(
            f"Events={report.events_analysed}; bookmakers={report.unique_bookmakers}; "
            f"quotes={report.quotes_processed}; "
            f"mathematical candidates={report.mathematical_candidates}; "
            f"unverified={report.unverified_candidates}; "
            f"rejected markets={report.rejected_markets}"
        )
        lines.append(
            f"Run credits: actual={report.run_budget.actual_credits}; "
            f"accounted={report.run_budget.accounted_credits}"
        )
    lines.append(
        f"New={len(point.new_candidates)}; "
        f"continuing={len(point.continuing_candidates)}; "
        f"no longer observed={len(point.no_longer_observed)}; "
        f"not checked={len(point.not_checked)}"
    )
    remaining = max(
        0, session.plan.max_credits - session.session_budget.accounted_credits
    )
    lines.append(
        f"Session credits: actual={session.session_budget.actual_credits}; "
        f"accounted={session.session_budget.accounted_credits}; remaining={remaining}"
    )
    if report is not None and report.mathematical_candidates:
        lines.append(scan_summary(report))
    return "\n".join(lines)


def completion_summary(session: MonitoringReport, output: Path) -> str:
    return "\n".join(
        (
            f"Monitoring complete: {session.id}",
            f"Scans={len(session.scans)}; "
            f"event observations={session.event_observations}; "
            f"unique events={len(session.unique_events)}; "
            f"unique candidates={len(session.observed_candidates)}",
            f"Session credits: actual={session.session_budget.actual_credits}; "
            f"accounted={session.session_budget.accounted_credits}",
            f"Stop reason: {session.stop_reason}; "
            f"skipped slots={session.skipped_slots}",
            f"Archive: {output.resolve()}",
            "Mathematical candidates are not guaranteed executable profit. "
            "No bets placed.",
        )
        + ((f"Error: {session.error}",) if session.error else ())
    )
