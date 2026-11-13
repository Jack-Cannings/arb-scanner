"""Five read-only pages. Run with betting-dashboard, not by importing clients."""

import os
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import streamlit as st

from betting_scanner.backtesting.models import BacktestReport
from betting_scanner.dashboard.loading import (
    Paths,
    backtest_directories,
    load_csv,
    load_live,
    load_model,
)
from betting_scanner.dashboard.presentation import (
    Row,
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
    utc,
)
from betting_scanner.live.models import LiveManifest
from betting_scanner.research.analysis import ResearchReport
from betting_scanner.research.investigation import percentile
from betting_scanner.research.investigation_models import Investigation
from betting_scanner.strategies.arbitrage.calculation import validate_budget

PAGES = (
    "Overview",
    "Live Odds",
    "Monitoring History",
    "Historical Research",
    "Backtesting",
)


def table(rows: list[Row], key: str) -> None:
    if not rows:
        st.info("No records match this selection.")
        return
    size = st.selectbox("Rows per page", (50, 100, 250), key=key + "size")
    page = st.selectbox(
        "Page", range(1, (len(rows) + size - 1) // size + 1), key=key + "page"
    )
    st.caption(
        f"{len(rows):,} records · click a column heading to sort the displayed page"
    )
    displayed = [
        {k: text(v) if isinstance(v, str) else v for k, v in row.items()}
        for row in rows[(page - 1) * size : page * size]
    ]
    st.dataframe(displayed, hide_index=True, width="stretch")


def metrics(values: dict[str, str | int | None]) -> None:
    columns = st.columns(min(4, max(1, len(values))))
    for i, (name, value) in enumerate(values.items()):
        columns[i % len(columns)].metric(
            name, text(value) if value is not None else "Unknown"
        )


def decimal_input(label: str, default: str, *, budget: bool = False) -> Decimal | None:
    value = st.text_input(label, default)
    try:
        result = Decimal(value)
        if budget:
            return validate_budget(result)
        if not result.is_finite() or result < 0:
            raise ValueError("Nonnegative finite value required")
        return result
    except (ValueError, InvalidOperation):
        st.warning(
            "Enter a finite nonnegative percentage."
            if not budget
            else "Enter a positive GBP stake in whole pennies, for example 100.00."
        )
        return None


def credit_panel(paths: Paths) -> None:
    manifest = load_model(paths.live, "manifest.json", LiveManifest)
    st.subheader("API credit journal")
    if manifest.value is None:
        st.info("No valid credit journal is saved. No API requests are made here.")
        return
    data = credits(manifest.value.budget, manifest.value.updated_at)
    metrics(
        {
            k: str(v) if v is not None else None
            for k, v in data.items()
            if "time" not in k.lower() and "updated" not in k.lower()
        }
    )
    st.caption(
        f"Balance request time: {data['Balance request time']} · "
        f"Journal last updated: {data['Journal last updated']}"
    )
    st.info(
        "Saved provider balance, not a guaranteed current balance. Unknown charges "
        "remain reserved in accounted usage. No credits are consumed by this dashboard."
    )


def overview(paths: Paths) -> None:
    data = load_live(paths)
    for notice in data.notices:
        st.info(notice)
    st.subheader("Latest recorded bookmaker observation")
    if data.observations:
        latest = data.observations[-1]
        snapshots = latest.normalised.snapshots
        results = latest.analyses
        metrics(
            {
                "Observed UTC": utc(latest.observed_at),
                "Unique matches": len({s.snapshot.event.id for s in snapshots}),
                "Bookmakers": len(
                    {q.bookmaker_id for s in snapshots for q in s.snapshot.quotes}
                ),
                "Quotes": sum(len(s.snapshot.quotes) for s in snapshots),
                "Mathematical candidates": sum(
                    bool(r.candidate and r.candidate.mathematical_arbitrage)
                    for r in results
                ),
            }
        )
        st.caption(
            "Competition: "
            + text(
                ", ".join(sorted({s.snapshot.competition.name for s in snapshots}))
                or latest.sport_key
            )
        )
    else:
        st.info(
            "No indexed live observations yet. Save an explicit CLI scan separately."
        )
    scans = [scan for session in data.sessions for scan in session.scans]
    if scans:
        latest_scan = max(scans, key=lambda s: s.started_at)
        metrics(
            {
                "Latest saved scan start": utc(latest_scan.started_at),
                "New candidates in that scan": len(latest_scan.new_candidates),
                "Continuing in that scan": len(latest_scan.continuing_candidates),
            }
        )
        if latest_scan.report:
            st.caption(
                "Latest scan keys: " + ", ".join(latest_scan.report.plan.sport_keys)
            )
    else:
        st.caption("No saved monitoring scan lifecycle counts are available.")
    research = load_model(paths.data, "research-output/report.json", ResearchReport)
    st.subheader("Historical CSV research")
    if research.value is not None:
        metrics(
            {
                "Unverified historical candidates": len(research.value.candidates),
                "Valid historical matches": research.value.valid_matches,
            }
        )
    else:
        st.info("No complete historical research report is available.")
    st.caption(
        "Historical CSV candidates have unknown bookmaker identities and simultaneity. "
        "Paper-trading results are shown separately on Backtesting."
    )
    credit_panel(paths)


def live_odds(paths: Paths) -> None:
    data = load_live(paths)
    for notice in data.notices:
        st.info(notice)
    st.warning(
        "Recorded observations only. Prices may be stale, markets may have started, "
        "and availability, stake acceptance and settlement rules may be unverified."
    )
    if not data.observations:
        st.info("No saved odds. Run the live CLI explicitly outside this dashboard.")
        return
    selected = st.selectbox(
        "Observation timestamp / competition key",
        tuple(reversed(data.observations)),
        format_func=lambda o: f"{utc(o.observed_at)} · {o.sport_key} · {o.id[-8:]}",
    )
    stake = decimal_input("Total hypothetical stake (GBP)", "100.00", budget=True)
    minimum = decimal_input("Minimum theoretical ROI (%)", "0")
    if stake is None or minimum is None:
        return
    views = market_views((selected,), stake)
    competition = st.selectbox(
        "Competition", ["All", *sorted({v.snapshot.competition.name for v in views})]
    )
    match = st.selectbox("Match", ["All", *sorted({v.label for v in views})])
    bookmaker = st.selectbox(
        "Bookmaker",
        ["All", *sorted({b.name for v in views for b in v.snapshot.bookmakers})],
    )
    filtered = filter_markets(views, competition, match, bookmaker, minimum)
    rows = quote_rows(filtered, datetime.now(UTC), bookmaker)
    sort = st.selectbox(
        "Sort all quotes by",
        ("Local observation", "Match", "Bookmaker", "Decimal odds"),
    )
    descending = st.checkbox("Descending order", value=True)
    rows.sort(
        key=lambda r: float(str(r[sort])) if sort == "Decimal odds" else str(r[sort]),
        reverse=descending,
    )
    table(rows, "live")
    st.subheader("Mathematical candidates at the recorded observation")
    candidates = [
        v
        for v in filtered
        if v.result.candidate and v.result.candidate.mathematical_arbitrage
    ]
    if not candidates:
        st.info(
            "No mathematical candidates in this selection. "
            "Rejected market checks remain below."
        )
    for i, view in enumerate(candidates[:50]):
        calc = view.result.candidate
        assert calc is not None
        with st.expander(
            f"{view.label} · {pct(calc.theoretical_roi)} · "
            f"{gbp(calc.worst_case_profit)} rounded hypothetical profit"
        ):
            metrics(
                {
                    "Total hypothetical stake": gbp(calc.total_stake),
                    "Theoretical ROI": pct(calc.theoretical_roi),
                    "Rounded worst-case profit": gbp(calc.worst_case_profit),
                    "Verification at receipt": view.result.verification_status.value,
                }
            )
            books = {b.id: b.name for b in view.snapshot.bookmakers}
            outcomes = {o.id: o.name for m in view.snapshot.markets for o in m.outcomes}
            table(
                [
                    {
                        "Outcome": text(outcomes[leg.quote.outcome_id]),
                        "Bookmaker": text(books[leg.quote.bookmaker_id]),
                        "Selected decimal odds": str(leg.quote.decimal_odds),
                        "Hypothetical stake": gbp(leg.stake),
                        "Penny payout": gbp(leg.payout),
                    }
                    for leg in calc.legs
                ],
                f"legs{i}",
            )
            st.caption(
                "Receipt-time verification is not current execution evidence. "
                "Bookmaker limits and price availability remain unverified."
            )
            st.write(
                "; ".join(
                    text(r.code.value + ": " + r.message) for r in view.result.reasons
                )
                or "No receipt-time detector rejection; "
                "current execution is still unverified."
            )
    if len(candidates) > 50:
        st.caption("Showing 50 candidate details; narrow the filters for more.")
    with st.expander("Market checks and rejection reasons"):
        table(
            [
                {
                    "Match": v.label,
                    "Receipt verification": v.result.verification_status.value,
                    "Reasons": "; ".join(
                        text(r.code.value + ": " + r.message) for r in v.result.reasons
                    ),
                }
                for v in filtered
            ],
            "checks",
        )


def monitoring(paths: Paths) -> None:
    data = load_live(paths)
    st.info(
        "Saved session checkpoints only. A checkpoint without a finish time is not "
        "proof of a running process. This page does not control monitoring."
    )
    for notice in data.notices:
        st.info(notice)
    table(
        [
            {
                "Session": s.id,
                "Started": utc(s.started_at),
                "Finished": utc(s.finished_at),
                "Competition keys": ", ".join(s.plan.scan.sport_keys),
                "Scans saved": len(s.scans),
                "Unique matches": len(s.unique_events),
                "Session accounted credits": s.session_budget.accounted_credits,
                "Session actual credits": s.session_budget.actual_credits,
                "Stop reason": s.stop_reason.value
                if s.stop_reason
                else "No final stop reason recorded",
            }
            for s in reversed(data.sessions)
        ],
        "sessions",
    )
    selected = st.selectbox(
        "Session", ["All", *[s.id for s in reversed(data.sessions)]]
    )
    rows = [
        r
        for r in monitoring_rows(data)
        if selected == "All" or r["Session"] == selected
    ]
    rows.sort(key=lambda r: str(r["Started UTC"]))
    table(rows, "scans")
    if rows:
        for title, column in (
            ("Quotes collected over time", "Quotes"),
            ("Mathematical candidates over time", "Candidates"),
            ("Accounted API credits per recorded scan", "Accounted credits"),
        ):
            st.subheader(title)
            st.line_chart(rows, x="Started UTC", y=column)
    credit_panel(paths)
    st.subheader("Run monitoring separately in PowerShell")
    st.code(
        "uv run betting-live monitor --sport-key <saved-tennis-key> --interval 300 "
        "--stake 100 --max-credits 5 --max-scans 3 --output-dir data/live-tennis",
        language="powershell",
    )
    st.caption(
        "This CLI command previews the plan. Add --execute only after reviewing "
        "the credit estimate in your terminal; the dashboard never runs it."
    )


def csv_preview(root: Path, filename: str, columns: tuple[str, ...], key: str) -> None:
    loaded = load_csv(root, filename)
    if loaded.value is None:
        st.info(loaded.notice or "CSV unavailable")
    else:
        table([{k: text(row.get(k, "")) for k in columns} for row in loaded.value], key)


def historical(paths: Paths) -> None:
    st.warning(
        "Unverified historical CSV research: bookmaker identities, quote observation "
        "times, simultaneity and settlement compatibility are unknown. "
        "Theoretical profits are not realised betting returns."
    )
    report = load_model(paths.data, "research-output/report.json", ResearchReport)
    audit = load_model(
        paths.data, "investigation-output/statistics.json", Investigation
    )
    if report.value is None:
        st.info(report.notice or "Generate a research report with the CLI.")
    else:
        r = report.value
        if audit.value is not None and r.imported.inputs != audit.value.inputs:
            st.warning(
                "Research and investigation use different input versions. "
                "Their summaries are separate saved runs."
            )
        metrics(
            {
                "ATP analysed": r.atp_matches,
                "WTA analysed": r.wta_matches,
                "Unverified candidates": len(r.candidates),
                "Candidate percentage": pct(r.candidate_percentage),
                "Mean theoretical ROI": pct(r.average_theoretical_roi),
                "Median theoretical ROI": pct(
                    percentile(
                        tuple(c.calculation.theoretical_roi for c in r.candidates), 50
                    )
                ),
                "Skipped rows": r.skipped_rows,
            }
        )
        tour = st.selectbox("Tour", ("All", "ATP", "WTA"))
        tournament = st.selectbox(
            "Tournament",
            [
                "All",
                *sorted(
                    {
                        c.match.tournament_name
                        for c in r.candidates
                        if tour == "All" or c.match.tour.upper() == tour
                    }
                ),
            ],
        )
        minimum = decimal_input("Minimum historical theoretical ROI (%)", "0")
        flag = st.selectbox(
            "Anomaly / review status", ("All", "Flagged", "No displayed flag")
        )
        if minimum is not None:
            table(
                research_rows(r, audit.value, tour, tournament, minimum, flag),
                "research",
            )
    with st.expander("Saved research CSVs"):
        csv_preview(
            paths.data,
            "research-output/candidates.csv",
            (
                "tour",
                "match_id",
                "tournament",
                "theoretical_roi_pct",
                "source_file",
                "line",
                "verification",
            ),
            "researchcsv",
        )
        csv_preview(
            paths.data,
            "research-output/skipped.csv",
            ("source_file", "line", "tour", "reasons_json"),
            "skippedcsv",
        )
    st.subheader("Data-quality investigation")
    if audit.value is None:
        st.info(audit.notice or "No investigation report is available.")
    else:
        a = audit.value
        scope = "all" if report.value is None or tour == "All" else tour.lower()
        d = a.distributions.get(scope)
        if d:
            metrics(
                {
                    "Investigation median ROI": pct(d.median),
                    "Investigation mean ROI": pct(d.mean),
                    "Highest ROI": pct(d.maximum),
                    "Flagged candidate cases": a.suspected_case_count,
                }
            )
        st.caption(
            "Investigation summaries describe the complete saved scope; "
            "the candidate table filters do not rewrite those summaries."
        )
        bands = [
            {"Band": b.band, "Count": b.count} for b in a.bands if b.scope == scope
        ]
        if bands:
            st.bar_chart(bands, x="Band", y="Count", sort=False)
        table(
            [
                {
                    "Tour": s.key.upper(),
                    "Valid matches": s.valid_matches,
                    "Candidates": s.candidates,
                    "Candidate rate %": float(s.candidate_percentage),
                }
                for s in a.subsets
                if s.scope == "tour"
            ],
            "rates",
        )
        metrics(
            {
                "Invalid-price records": sum(
                    x.category == "confirmed_invalid_price"
                    for x in a.confirmed_anomalies
                ),
                "Missing-price records": sum(
                    x.category == "confirmed_missing_price"
                    for x in a.confirmed_anomalies
                ),
            }
        )
    with st.expander("Investigation CSV tables"):
        for filename, columns in (
            (
                "top30.csv",
                (
                    "source_file",
                    "line",
                    "match_id",
                    "tournament",
                    "winner_name",
                    "loser_name",
                    "theoretical_roi",
                    "suspected_issues",
                ),
            ),
            (
                "roi_bands.csv",
                ("scope", "band", "count", "percentage_of_candidates"),
            ),
            (
                "confirmed_anomalies.csv",
                ("source_file", "line", "tour", "category", "details"),
            ),
        ):
            st.caption(filename)
            csv_preview(
                paths.data, "investigation-output/" + filename, columns, filename
            )
    st.caption(
        "Missing or invalid records are distinct from suspected price issues. "
        "No suspicious candidate is upgraded to a verified opportunity."
    )


def backtesting(paths: Paths) -> None:
    st.info(
        "Simulated paper trading only. Fills are not real bookmaker transactions. "
        "Open stakes are carried at cost, not counted as realised profit."
    )
    directories = backtest_directories(paths)
    if not directories:
        st.info(
            "No saved backtests found under backtest-output/ or data/backtests/. "
            "Generate one separately with betting-backtest."
        )
        return
    selected = st.selectbox(
        "Saved backtest directory",
        directories,
        format_func=lambda p: str(p.relative_to(paths.project)),
    )
    report = load_model(selected, "report.json", BacktestReport)
    if report.value is None:
        st.info(report.notice or "Backtest report unavailable")
        return
    r, m = report.value, report.value.metrics
    st.subheader(text(r.name))
    metrics(
        {
            "Starting bankroll": gbp(r.config.starting_bankroll),
            "Ending cash": gbp(m.ending_cash),
            "Ending equity at cost": gbp(m.ending_equity_at_cost),
            "Realised hypothetical P&L": gbp(m.realised_pnl),
            "Mathematical opportunities": m.mathematical_candidates,
            "Execution attempts": m.execution_attempts,
            "Fully executed positions": m.fully_executed,
            "Partial executions": m.partially_executed,
            "Failed executions": m.failed_executions,
            "Settled positions": m.settled_positions,
            "Unsettled positions": m.unsettled_positions,
            "Open stake exposure": gbp(m.unsettled_exposure),
            "Maximum drawdown": gbp(m.maximum_drawdown),
            "Largest simulated loss": gbp(m.largest_simulated_loss),
        }
    )
    st.caption(text(r.valuation_assumption))
    history = [
        {
            "Timestamp UTC": utc(p.timestamp),
            "Cash GBP": float(p.cash),
            "Equity at cost GBP": float(p.equity_at_cost),
            "Open stake GBP": float(p.open_stake),
        }
        for p in sorted(r.bankroll_history, key=lambda p: p.timestamp)
    ]
    if history:
        st.line_chart(
            history,
            x="Timestamp UTC",
            y=["Cash GBP", "Equity at cost GBP", "Open stake GBP"],
        )
    table(trade_rows(r), "trades")
    with st.expander("Simulated legs and saved CSVs"):
        table(
            [
                {
                    "Position": t.id,
                    "Bookmaker": b.planned_quote.bookmaker_id,
                    "Outcome": b.planned_quote.outcome_id,
                    "Accepted": b.accepted,
                    "Stake GBP": str(b.stake),
                    "Disposition": b.disposition.value,
                    "Rejection": text(b.rejection_reason or ""),
                    "Payout GBP": str(b.payout) if b.payout is not None else None,
                }
                for t in r.trades
                for b in t.bets
            ],
            "bets",
        )
        csv_preview(
            selected,
            "trades.csv",
            (
                "trade_id",
                "execution_status",
                "accepted",
                "disposition",
                "settled_at",
                "position_realised_profit",
            ),
            "tradescsv",
        )
        csv_preview(
            selected,
            "bankroll.csv",
            ("timestamp", "reason", "cash", "open_stake", "equity_at_cost"),
            "bankrollcsv",
        )


def render() -> None:
    st.set_page_config(
        page_title="Arbitrage Research Desk", page_icon="📊", layout="wide"
    )
    paths = Paths(Path(os.environ.get("BETTING_DASHBOARD_ROOT", ".")).resolve())
    st.sidebar.title("Arbitrage Research Desk")
    st.sidebar.caption("Local saved data · read-only")
    page = st.sidebar.radio("Navigation", PAGES)
    st.sidebar.caption(
        "Data: data/live-tennis, research-output and investigation-output"
    )
    if st.sidebar.button("Refresh saved files"):
        st.cache_data.clear()
    st.title(page)
    st.caption(
        "Recorded observations, unverified research and simulated results. "
        "No live polling, monitor control or betting."
    )
    try:
        {
            "Overview": overview,
            "Live Odds": live_odds,
            "Monitoring History": monitoring,
            "Historical Research": historical,
            "Backtesting": backtesting,
        }[page](paths)
    except (OSError, ValueError):
        st.warning(
            "A saved file or configured path is unavailable or changing. "
            "Wait for the writer to finish, then refresh saved files."
        )


if __name__ == "__main__":
    render()
