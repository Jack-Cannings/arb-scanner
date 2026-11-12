"""Deterministic terminal, JSON and CSV reporting; no simulation logic."""

import csv
from decimal import Decimal
from pathlib import Path

from betting_scanner.backtesting.models import BacktestReport


def _roi(value: Decimal | None) -> str:
    return f"{value:.4f}%" if value is not None else "n/a"


def terminal_summary(report: BacktestReport) -> str:
    metrics = report.metrics
    return "\n".join(
        (
            f"Paper backtest: {report.name}",
            f"Snapshots / events: {metrics.total_snapshots} / {metrics.unique_events}",
            f"Candidates / eligible: {metrics.mathematical_candidates} / "
            f"{metrics.eligible_opportunities}",
            f"Attempts / full / partial / failed: {metrics.execution_attempts} / "
            f"{metrics.fully_executed} / {metrics.partially_executed} / "
            f"{metrics.failed_executions}",
            f"Settled / unsettled positions: {metrics.settled_positions} / "
            f"{metrics.unsettled_positions}",
            f"Accepted stakes: GBP {metrics.total_stakes:.2f}",
            f"Realised hypothetical P&L: GBP {metrics.realised_pnl:.2f}",
            f"Unsettled stake exposure: GBP {metrics.unsettled_exposure:.2f}",
            f"Ending cash / equity at cost: GBP {metrics.ending_cash:.2f} / "
            f"GBP {metrics.ending_equity_at_cost:.2f}",
            f"Maximum drawdown / largest loss: GBP {metrics.maximum_drawdown:.2f} / "
            f"GBP {metrics.largest_simulated_loss:.2f}",
            f"Average theoretical ROI: {_roi(metrics.average_theoretical_roi)}",
            f"Average realised ROI: {_roi(metrics.average_realised_roi)}",
            report.valuation_assumption,
            "Simulated fills and historical prices do not prove executability.",
        )
    )


def export_report(report: BacktestReport, output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "report.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    with (output_directory / "trades.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        fields = [
            "trade_id",
            "event_id",
            "market_id",
            "decided_at",
            "executed_at",
            "execution_status",
            "bookmaker_id",
            "outcome_id",
            "planned_odds",
            "execution_odds",
            "stake",
            "accepted",
            "rejection_reason",
            "disposition",
            "settlement_source",
            "payout",
            "settled_at",
            "position_realised_profit",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in report.trades:
            for bet in trade.bets:
                writer.writerow(
                    {
                        "trade_id": trade.id,
                        "event_id": trade.event_id,
                        "market_id": trade.market_id,
                        "decided_at": trade.decided_at.isoformat(),
                        "executed_at": trade.executed_at.isoformat(),
                        "execution_status": trade.execution_status,
                        "bookmaker_id": bet.planned_quote.bookmaker_id,
                        "outcome_id": bet.planned_quote.outcome_id,
                        "planned_odds": str(bet.planned_quote.decimal_odds),
                        "execution_odds": str(bet.execution_quote.decimal_odds)
                        if bet.execution_quote
                        else "",
                        "stake": str(bet.stake),
                        "accepted": bet.accepted,
                        "rejection_reason": bet.rejection_reason or "",
                        "disposition": bet.disposition,
                        "settlement_source": bet.settlement_source or "",
                        "payout": str(bet.payout) if bet.payout is not None else "",
                        "settled_at": trade.settled_at.isoformat()
                        if trade.settled_at
                        else "",
                        # Record position P&L only once to avoid double counting.
                        "position_realised_profit": str(trade.realised_profit)
                        if bet is trade.bets[0] and trade.realised_profit is not None
                        else "",
                    }
                )
    with (output_directory / "bankroll.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        history_writer = csv.writer(handle)
        history_writer.writerow(
            ["timestamp", "reason", "cash", "open_stake", "equity_at_cost"]
        )
        for point in report.bankroll_history:
            history_writer.writerow(
                [
                    point.timestamp.isoformat(),
                    point.reason,
                    str(point.cash),
                    str(point.open_stake),
                    str(point.equity_at_cost),
                ]
            )
