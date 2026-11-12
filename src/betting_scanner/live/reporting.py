"""Terminal presentation of mathematical candidates; no calculation logic."""

from betting_scanner.live.models import LiveScanReport


def scan_summary(report: LiveScanReport) -> str:
    lines = [
        f"Pre-match events analysed: {report.events_analysed}",
        f"Bookmakers / quotes: {report.unique_bookmakers} / {report.quotes_processed}",
        f"Mathematical candidates: {report.mathematical_candidates}",
        f"Eligible opportunities: {report.eligible_opportunities}",
        f"Unverified candidates / rejected markets: {report.unverified_candidates} / "
        f"{report.rejected_markets}",
        f"Run credits: actual={report.run_budget.actual_credits}; "
        f"accounted={report.run_budget.accounted_credits}",
        f"Archive credits: actual={report.archive_budget.actual_credits}; "
        f"accounted={report.archive_budget.accounted_credits}; "
        f"cap={report.archive_budget.max_credits}",
    ]
    reasons: dict[str, int] = {}
    for observation in report.observations:
        for analysis in observation.analyses:
            for code in {reason.code.value for reason in analysis.reasons}:
                reasons[code] = reasons.get(code, 0) + 1
    if reasons:
        lines.append(
            "Market check results: "
            + "; ".join(
                f"{code}={count} market(s)" for code, count in sorted(reasons.items())
            )
        )
    for observation in report.observations:
        lines.append(
            f"{observation.sport_key}: observed {observation.observed_at.isoformat()}; "
            f"started excluded={len(observation.excluded_started)}; "
            f"validation issues={len(observation.normalised.issues)}"
        )
        for analysis in observation.analyses:
            calculation = analysis.candidate
            if calculation is None or not calculation.mathematical_arbitrage:
                continue
            snapshot = next(
                s.snapshot
                for s in observation.normalised.snapshots
                if s.snapshot.event.id == analysis.event_id
            )
            names = " vs ".join(p.name for p in snapshot.event.participants)
            lines.append(f"Candidate: {snapshot.competition.name} | {names}")
            lines.append(
                f"Start: {snapshot.event.scheduled_start.isoformat()} | "
                f"verification={analysis.verification_status}; "
                f"status={analysis.actionability_status}"
            )
            books = {b.id: b for b in snapshot.bookmakers}
            outcomes = {o.id: o for m in snapshot.markets for o in m.outcomes}
            for leg in calculation.legs:
                quote = leg.quote
                update = quote.provider_updated_at
                freshness = (
                    f"age={(observation.observed_at - update).total_seconds():.0f}s"
                    if update is not None
                    else "freshness unverified: provider update missing"
                )
                lines.append(
                    f"  {outcomes[quote.outcome_id].name}: "
                    f"{books[quote.bookmaker_id].name} odds={quote.decimal_odds}; "
                    f"hypothetical stake=GBP {leg.stake:.2f}; "
                    f"updated={update.isoformat() if update else 'missing'}; "
                    f"{freshness}"
                )
            lines.append(
                f"Theoretical ROI={calculation.theoretical_roi:.4f}%; "
                f"rounded worst-case profit=GBP {calculation.worst_case_profit:.2f}"
            )
            lines.append(
                "Unverified/rejection reasons: "
                + "; ".join(sorted({r.code.value for r in analysis.reasons}))
            )
    lines.extend(f"Notice: {error}" for error in report.errors)
    lines.append(
        "Hypothetical prices/stakes only. Unknown settlement rules are not "
        "executable verification; no bets are placed."
    )
    return "\n".join(lines)
