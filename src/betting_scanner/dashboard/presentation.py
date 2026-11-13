"""Typed transforms and production stake recalculation, independent of UI widgets."""

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from betting_scanner.backtesting.models import BacktestReport
from betting_scanner.core.models import OddsQuote, OddsSnapshot
from betting_scanner.dashboard.loading import LiveData
from betting_scanner.live.models import LiveObservation
from betting_scanner.providers.odds_api.client import BudgetState
from betting_scanner.research.analysis import ResearchReport
from betting_scanner.research.investigation import audit_candidate
from betting_scanner.research.investigation_models import Investigation
from betting_scanner.strategies.arbitrage import detect_arbitrage
from betting_scanner.strategies.arbitrage.results import ArbitrageResult

type Row = dict[str, str | int | float | bool | None]


def text(value: object) -> str:
    result = str(value)
    secret = os.environ.get("ODDS_API_KEY")
    return result.replace(secret, "[redacted]") if secret else result


def utc(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value is not None else "Unknown"


def gbp(value: Decimal | None) -> str:
    return f"£{value:,.2f}" if value is not None else "Unknown"


def pct(value: Decimal | None) -> str:
    return f"{value:.4f}%" if value is not None else "Unknown"


def credits(budget: BudgetState, updated_at: datetime) -> Row:
    last = next(
        (
            a
            for a in reversed(budget.attempts)
            if a.quota is not None and a.quota.remaining is not None
        ),
        None,
    )
    return {
        "Archive accounted credits": budget.accounted_credits,
        "Confirmed actual credits": budget.actual_credits,
        "Archive credit cap": budget.max_credits,
        "Archive headroom": max(0, budget.max_credits - budget.accounted_credits),
        "Last reported provider balance": last.quota.remaining
        if last and last.quota
        else None,
        "Balance request time": utc(last.requested_at) if last else "Unknown",
        "Journal last updated": utc(updated_at),
    }


@dataclass(frozen=True)
class MarketView:
    observation: LiveObservation
    snapshot: OddsSnapshot
    result: ArbitrageResult

    @property
    def label(self) -> str:
        return text(" vs ".join(p.name for p in self.snapshot.event.participants))


def market_views(
    observations: tuple[LiveObservation, ...], stake: Decimal
) -> tuple[MarketView, ...]:
    # Re-evaluate only saved snapshots at their receipt time. This is a historical
    # reconstruction of the checks, never a declaration of current availability.
    return tuple(
        MarketView(
            observation,
            saved.snapshot,
            detect_arbitrage(
                saved.snapshot,
                market.id,
                total_stake=stake,
                evaluated_at=observation.observed_at,
                config=observation.plan.engine,
            ),
        )
        for observation in observations
        for saved in observation.normalised.snapshots
        for market in saved.snapshot.markets
    )


def filter_markets(
    views: tuple[MarketView, ...],
    competition: str = "All",
    match: str = "All",
    bookmaker: str = "All",
    minimum_roi: Decimal = Decimal(0),
) -> tuple[MarketView, ...]:
    return tuple(
        v
        for v in views
        if (competition == "All" or v.snapshot.competition.name == competition)
        and (match == "All" or v.label == match)
        and (
            bookmaker == "All"
            or any(b.name == bookmaker for b in v.snapshot.bookmakers)
        )
        and (
            minimum_roi == 0
            or (
                v.result.candidate is not None
                and v.result.candidate.theoretical_roi >= minimum_roi
            )
        )
    )


def freshness(quote: OddsQuote, now: datetime, view: MarketView) -> str:
    updated = quote.provider_updated_at
    if updated is None:
        return "Unknown update time; recorded only"
    age = now - updated
    if (
        age.total_seconds()
        < -view.observation.plan.engine.clock_skew_tolerance.total_seconds()
    ):
        return "Future update time; unverified"
    if age > view.observation.plan.engine.max_quote_age:
        return f"Stale now ({max(0, int(age.total_seconds()))}s); recorded only"
    return "Within saved age limit now; availability unverified"


def quote_rows(
    views: tuple[MarketView, ...], now: datetime, bookmaker: str = "All"
) -> list[Row]:
    rows: list[Row] = []
    seen: set[tuple[str, str, str, str]] = set()
    for v in views:
        books = {b.id: b.name for b in v.snapshot.bookmakers}
        outcomes = {o.id: o.name for m in v.snapshot.markets for o in m.outcomes}
        for q in v.snapshot.quotes:
            if q.market_id != v.result.market_id or (
                bookmaker != "All" and books[q.bookmaker_id] != bookmaker
            ):
                continue
            key = (v.observation.id, q.event_id, q.bookmaker_id, q.outcome_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "Competition": text(v.snapshot.competition.name),
                    "Match": v.label,
                    "Event start": utc(v.snapshot.event.scheduled_start),
                    "Bookmaker": text(books[q.bookmaker_id]),
                    "Outcome": text(outcomes[q.outcome_id]),
                    "Decimal odds": str(q.decimal_odds),
                    "Bookmaker updated": utc(q.provider_updated_at),
                    "Local observation": utc(v.observation.observed_at),
                    "Freshness now": freshness(q, now, v),
                    "Mathematical candidate": bool(
                        v.result.candidate and v.result.candidate.mathematical_arbitrage
                    ),
                }
            )
    return rows


def monitoring_rows(data: LiveData) -> list[Row]:
    return [
        {
            "Session": session.id,
            "Scan": scan.number,
            "Started UTC": utc(scan.started_at),
            "Competition keys": ", ".join(scan.report.plan.sport_keys)
            if scan.report
            else "Unknown",
            "Event observations": scan.report.events_analysed if scan.report else None,
            "Quotes": scan.report.quotes_processed if scan.report else None,
            "Candidates": scan.report.mathematical_candidates if scan.report else None,
            "New": len(scan.new_candidates),
            "Continuing": len(scan.continuing_candidates),
            "Accounted credits": scan.report.run_budget.accounted_credits
            if scan.report
            else None,
            "Actual credits": scan.report.run_budget.actual_credits
            if scan.report
            else None,
        }
        for session in data.sessions
        for scan in session.scans
    ]


def research_rows(
    report: ResearchReport,
    investigation: Investigation | None,
    tour: str = "All",
    tournament: str = "All",
    minimum_roi: Decimal = Decimal(0),
    anomaly: str = "All",
) -> list[Row]:

    rows: list[Row] = []
    for c in report.candidates:
        m, calc = c.match, c.calculation
        flags = (
            audit_candidate(c).suspected_issues
            if investigation is not None
            else m.warnings + c.warnings
        )
        if (
            (tour != "All" and m.tour.upper() != tour)
            or (tournament != "All" and m.tournament_name != tournament)
            or calc.theoretical_roi < minimum_roi
        ):
            continue
        if (anomaly == "Flagged" and not flags) or (
            anomaly == "No displayed flag" and flags
        ):
            continue
        rows.append(
            {
                "Tour": m.tour.upper(),
                "Tournament": text(m.tournament_name),
                "Match": text(f"{m.winner_name} vs {m.loser_name}"),
                "Match start": utc(m.match_start),
                "Source": f"{m.source_file}:{m.line}",
                "Winner odds": str(m.winner_odds),
                "Loser odds": str(m.loser_odds),
                "Theoretical ROI %": float(calc.theoretical_roi),
                "Rounded hypothetical profit GBP": str(calc.worst_case_profit),
                "Review flags": "; ".join(flags),
                "Verification": "Unverified historical CSV",
            }
        )
    return sorted(rows, key=lambda r: float(str(r["Theoretical ROI %"])), reverse=True)


def trade_rows(report: BacktestReport) -> list[Row]:
    return [
        {
            "Position": trade.id,
            "Executed UTC": utc(trade.executed_at),
            "Execution": trade.execution_status.value,
            "Accepted stake GBP": str(trade.accepted_stake),
            "Settlement": "Settled" if trade.settled_at is not None else "Unsettled",
            "Settled UTC": utc(trade.settled_at),
            "Realised hypothetical P&L GBP": str(trade.realised_profit)
            if trade.settled_at is not None and trade.realised_profit is not None
            else None,
            "Accepted legs still open": sum(
                b.accepted and b.disposition.value == "open" for b in trade.bets
            ),
            "Reasons": text("; ".join(trade.reasons)),
        }
        for trade in report.trades
    ]
