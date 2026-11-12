import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from betting_scanner.backtesting.models import (
    BacktestReport,
    BetDisposition,
    ExecutionStatus,
    HistoricalDataset,
    ReplayConfig,
    StakeLimits,
)
from betting_scanner.backtesting.reader import HistoricalReader, JsonHistoricalReader
from betting_scanner.backtesting.replay import ReplayClock, run_replay
from betting_scanner.backtesting.reporting import export_report, terminal_summary
from betting_scanner.interfaces.backtest import main
from betting_scanner.strategies.arbitrage.results import EngineConfig, ReasonCode

HISTORIES = Path(__file__).parent / "fixtures" / "histories"
NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)


def load(name: str = "a_success") -> HistoricalDataset:
    reader: HistoricalReader = JsonHistoricalReader(HISTORIES / f"{name}.json")
    return reader.read()


@pytest.mark.parametrize("path", sorted(HISTORIES.glob("*.json")), ids=lambda p: p.stem)
def test_all_ten_histories_validate_and_reproduce(path: Path) -> None:
    dataset = JsonHistoricalReader(path).read()
    report = run_replay(dataset)
    assert run_replay(dataset) == report
    assert BacktestReport.model_validate_json(report.model_dump_json()) == report
    assert all(point.cash >= 0 for point in report.bankroll_history)
    assert all(
        point.equity_at_cost == point.cash + point.open_stake
        for point in report.bankroll_history
    )


def test_success_bankroll_stakes_and_metrics() -> None:
    report = run_replay(load())
    metrics = report.metrics
    assert metrics.total_snapshots == metrics.unique_events == 1
    assert metrics.mathematical_candidates == metrics.eligible_opportunities == 1
    assert (
        metrics.execution_attempts
        == metrics.fully_executed
        == metrics.settled_positions
        == 1
    )
    assert metrics.total_stakes == Decimal("100")
    assert metrics.realised_pnl == Decimal("14.20")
    assert metrics.ending_cash == metrics.ending_equity_at_cost == Decimal("1014.20")
    assert metrics.unsettled_exposure == metrics.maximum_drawdown == 0
    trade = report.trades[0]
    assert [b.stake for b in trade.bets] == [Decimal("52.17"), Decimal("47.83")]
    assert [b.disposition for b in trade.bets] == [
        BetDisposition.WON,
        BetDisposition.LOST,
    ]
    assert all(b.settlement_source == "synthetic" for b in trade.bets)
    assert trade.expected_worst_case_payout == Decimal("114.20")
    execution = next(
        p for p in report.bankroll_history if p.reason.startswith("execution:")
    )
    assert execution.cash == Decimal("900") and execution.open_stake == Decimal("100")
    assert execution.equity_at_cost == Decimal("1000")


def test_either_winner_yields_approximately_equal_returns() -> None:
    data = load().model_dump(mode="json")
    for result in data["settlements"]:
        result["disposition"] = "lost" if result["disposition"] == "won" else "won"
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.realised_pnl == Decimal("14.21")


@pytest.mark.parametrize("name", ["b_no_arbitrage", "d_stale", "h_incompatible"])
def test_non_executable_histories_do_not_trade(name: str) -> None:
    report = run_replay(load(name))
    assert report.metrics.execution_attempts == 0
    assert report.metrics.ending_cash == Decimal("1000")
    assert report.metrics.realised_pnl == 0
    if name == "d_stale":
        assert ReasonCode.STALE_QUOTE in {
            r.code for r in report.observations[0].detection.reasons
        }
    if name == "h_incompatible":
        assert report.metrics.mathematical_candidates == 1


def test_repeated_quotes_are_not_new_opportunities() -> None:
    report = run_replay(load("j_repeated"))
    assert report.metrics.total_snapshots == 3
    assert (
        report.metrics.mathematical_candidates
        == report.metrics.eligible_opportunities
        == 1
    )
    assert report.metrics.execution_attempts == 1
    assert "unchanged_opportunity" in report.observations[-1].notes


def test_chronological_sorting_and_injected_clock() -> None:
    dataset = load("j_repeated")
    data = dataset.model_dump(mode="json")
    data["snapshots"].reverse()
    data["settlements"].reverse()
    clock = ReplayClock()
    report = run_replay(HistoricalDataset.model_validate(data), clock=clock)
    assert report == run_replay(dataset)
    timestamps = [p.timestamp for p in report.bankroll_history]
    assert timestamps == sorted(timestamps)
    assert clock.current == timestamps[-1]
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(NOW)


def test_future_snapshots_do_not_change_earlier_decisions() -> None:
    dataset = load("j_repeated")
    prefix = dataset.model_copy(
        update={"snapshots": dataset.snapshots[:1], "settlements": ()}
    )
    before = run_replay(prefix)
    after = run_replay(dataset)
    assert before.observations[0] == after.observations[0]
    assert before.trades[0].bets[0].stake == after.trades[0].bets[0].stake
    assert before.metrics.realised_pnl == 0
    assert before.metrics.ending_cash == Decimal("900")
    assert before.metrics.unsettled_exposure == Decimal("100")
    assert before.metrics.ending_equity_at_cost == Decimal("1000")


def test_future_collection_is_not_known_even_inside_engine_clock_skew() -> None:
    data = load().model_dump(mode="json")
    for quote in data["snapshots"][0]["snapshot"]["quotes"]:
        quote["collected_at"] = "2026-06-01T12:00:01Z"
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.execution_attempts == 0
    assert all(
        note.startswith("unobserved_quote:") for note in report.observations[0].notes
    )


@pytest.mark.parametrize("name", ["c_brief", "f_price_change"])
def test_delay_cannot_use_pre_change_prices(name: str) -> None:
    report = run_replay(load(name), ReplayConfig(execution_delay=timedelta(seconds=10)))
    assert report.metrics.execution_attempts == report.metrics.failed_executions == 1
    assert report.metrics.total_stakes == report.metrics.realised_pnl == 0
    assert report.trades[0].executed_at == NOW + timedelta(seconds=10)


def test_delay_requires_new_observation_at_execution_time() -> None:
    report = run_replay(load(), ReplayConfig(execution_delay=timedelta(seconds=1)))
    assert report.trades[0].reasons == ("no_execution_observation",)
    assert report.metrics.failed_executions == 1
    assert report.metrics.ending_cash == Decimal("1000")


def test_delayed_unchanged_price_can_execute_with_current_confirmation() -> None:
    report = run_replay(
        load("j_repeated"), ReplayConfig(execution_delay=timedelta(seconds=1))
    )
    assert report.metrics.fully_executed == 1
    assert report.metrics.execution_attempts == 1
    assert report.trades[0].executed_at == NOW + timedelta(seconds=1)


def test_missing_current_quote_not_carried_forward() -> None:
    data = load("j_repeated").model_dump(mode="json")
    data["snapshots"][1]["snapshot"]["quotes"] = []
    report = run_replay(
        HistoricalDataset.model_validate(data),
        ReplayConfig(execution_delay=timedelta(seconds=1)),
    )
    assert report.metrics.failed_executions == 1
    assert any(
        reason.startswith("quote_unavailable:") for reason in report.trades[0].reasons
    )
    assert report.metrics.total_stakes == 0


def test_rejection_leaves_explicit_one_sided_loss_and_drawdown() -> None:
    report = run_replay(load("e_rejection"))
    trade = report.trades[0]
    assert trade.execution_status == ExecutionStatus.PARTIAL
    assert trade.expected_worst_case_payout == 0
    assert trade.accepted_stake == Decimal("47.83")
    assert trade.bets[0].disposition == BetDisposition.NOT_PLACED
    assert trade.bets[0].rejection_reason == "simulated_bookmaker_rejection"
    assert report.metrics.realised_pnl == Decimal("-47.83")
    assert report.metrics.ending_cash == Decimal("952.17")
    assert (
        report.metrics.maximum_drawdown
        == report.metrics.largest_simulated_loss
        == Decimal("47.83")
    )
    assert report.metrics.average_realised_roi == Decimal("-100")


def test_unsettled_partial_fill_is_exposure_not_profit() -> None:
    dataset = load("e_rejection").model_copy(update={"settlements": ()})
    report = run_replay(dataset)
    assert report.metrics.unsettled_exposure == Decimal("47.83")
    assert report.metrics.realised_pnl == 0
    assert report.trades[0].realised_profit is None
    assert report.metrics.ending_cash == Decimal("952.17")
    assert report.metrics.ending_equity_at_cost == Decimal("1000")


def test_incomplete_results_keep_position_open_without_crediting_one_known_win() -> (
    None
):
    report = run_replay(load("i_incomplete_results"))
    assert report.metrics.settled_positions == 0
    assert report.metrics.unsettled_positions == 1
    assert report.metrics.realised_pnl == 0
    assert report.metrics.average_realised_roi is None
    assert report.metrics.ending_cash == Decimal("900")
    assert report.metrics.unsettled_exposure == Decimal("100")
    assert "incomplete_settlement_evidence" in report.trades[0].reasons


def test_voided_bets_refund_only_accepted_stakes() -> None:
    data = load().model_dump(mode="json")
    for result in data["settlements"]:
        result["disposition"] = "void"
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.ending_cash == Decimal("1000")
    assert report.metrics.realised_pnl == 0
    assert all(
        b.disposition == BetDisposition.VOID and b.payout == b.stake
        for b in report.trades[0].bets
    )


@pytest.mark.parametrize("change", ["incompatible", "unverified", "before_event_start"])
def test_invalid_settlement_evidence_never_credits_profit(change: str) -> None:
    data = load().model_dump(mode="json")
    for result in data["settlements"]:
        if change == "incompatible":
            result["settlement_rule"]["description"] += " Different terms."
        elif change == "unverified":
            result["settlement_rule"]["verified"] = False
        else:
            result["observed_at"] = "2026-06-01T12:30:00Z"
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.unsettled_exposure == Decimal("100")
    assert report.metrics.realised_pnl == 0
    assert "unverified_or_incompatible_settlement_evidence" in report.trades[0].reasons


def test_settlement_payouts_are_not_credited_twice_on_later_steps() -> None:
    data = load("j_repeated").model_dump(mode="json")
    late = json.loads(json.dumps(data["snapshots"][-1]))
    late["observed_at"] = "2026-06-01T15:00:00Z"
    data["snapshots"].append(late)
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.ending_cash == Decimal("1014.20")
    assert (
        len([p for p in report.bankroll_history if p.reason.startswith("settlement:")])
        == 1
    )


def test_limited_bankroll_prevents_second_opportunity() -> None:
    report = run_replay(
        load("g_bankroll"), ReplayConfig(starting_bankroll=Decimal("100"))
    )
    assert report.metrics.unique_events == report.metrics.execution_attempts == 2
    assert report.metrics.fully_executed == report.metrics.failed_executions == 1
    assert "insufficient_funds" in report.trades[1].reasons
    assert report.metrics.total_stakes == Decimal("100")


def test_aggregate_exposure_limit() -> None:
    report = run_replay(
        load("g_bankroll"), ReplayConfig(max_aggregate_exposure=Decimal("100"))
    )
    assert "aggregate_exposure_limit" in report.trades[1].reasons
    assert report.metrics.fully_executed == 1


def test_event_exposure_limit_on_changed_opportunity() -> None:
    data = load("j_repeated").model_dump(mode="json")
    data["snapshots"] = data["snapshots"][:2]
    for quote in data["snapshots"][1]["snapshot"]["quotes"]:
        if quote["bookmaker_id"] == "book-1" and quote["outcome_id"] == "a-win-a":
            quote["decimal_odds"] = "2.30"
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.eligible_opportunities == 2
    assert "event_exposure_limit" in report.trades[1].reasons
    assert report.metrics.total_stakes == Decimal("100")


def test_supplied_bookmaker_limits_can_cause_partial_execution() -> None:
    report = run_replay(
        load(),
        ReplayConfig(bookmaker_limits={"book-1": StakeLimits(maximum=Decimal("10"))}),
    )
    assert report.trades[0].execution_status == ExecutionStatus.PARTIAL
    assert report.trades[0].bets[0].rejection_reason == "bookmaker_stake_limit"
    assert report.metrics.realised_pnl < 0


def test_configurable_rejection_mode() -> None:
    report = run_replay(load(), ReplayConfig(execution_mode="reject_last"))
    assert report.metrics.partially_executed == 1
    assert report.trades[0].bets[-1].accepted is False


def test_price_override_and_slippage_are_actual_fill_prices() -> None:
    data = load().model_dump(mode="json")
    data["execution_instructions"] = [
        {
            "execution_at": NOW.isoformat(),
            "event_id": "event-a",
            "market_id": "market-a",
            "bookmaker_id": "book-1",
            "outcome_id": "a-win-a",
            "odds_override": "2.15",
        }
    ]
    report = run_replay(
        HistoricalDataset.model_validate(data), ReplayConfig(slippage=Decimal("0"))
    )
    assert report.trades[0].bets[0].execution_quote.decimal_odds == Decimal("2.15")
    assert report.metrics.realised_pnl == Decimal("12.16")
    worse = run_replay(
        HistoricalDataset.model_validate(data), ReplayConfig(slippage=Decimal("0.01"))
    )
    assert worse.metrics.realised_pnl < report.metrics.realised_pnl


def test_bad_price_changes_fail_preflight_without_spending_cash() -> None:
    report = run_replay(load(), ReplayConfig(slippage=Decimal("0.20")))
    assert report.metrics.failed_executions == 1
    assert report.metrics.ending_cash == Decimal("1000")
    assert "price_change_or_slippage_below_threshold" in report.trades[0].reasons


@pytest.mark.parametrize(
    "config",
    [
        ReplayConfig(minimum_roi=Decimal("20")),
        ReplayConfig(minimum_profit=Decimal("20")),
    ],
)
def test_minimum_roi_and_profit_filters(config: ReplayConfig) -> None:
    report = run_replay(load(), config)
    assert report.metrics.mathematical_candidates == 1
    assert (
        report.metrics.eligible_opportunities == report.metrics.execution_attempts == 0
    )
    assert "below_minimum_roi_or_profit" in report.observations[0].notes


def test_threshold_rechecked_after_slippage() -> None:
    report = run_replay(load(), ReplayConfig(minimum_roi=Decimal("14.5")))
    assert report.metrics.eligible_opportunities == 1
    assert report.metrics.failed_executions == 1
    assert report.metrics.total_stakes == 0


def test_configurable_freshness_threshold() -> None:
    data = load().model_dump(mode="json")
    for quote in data["snapshots"][0]["snapshot"]["quotes"]:
        quote["provider_updated_at"] = "2026-06-01T11:59:00Z"
    report = run_replay(
        HistoricalDataset.model_validate(data),
        ReplayConfig(engine=EngineConfig(max_quote_age=timedelta(seconds=30))),
    )
    assert report.metrics.execution_attempts == 0


def test_missing_update_does_not_become_verified_on_replay() -> None:
    data = load().model_dump(mode="json")
    for quote in data["snapshots"][0]["snapshot"]["quotes"]:
        quote["provider_updated_at"] = None
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.mathematical_candidates == 1
    assert report.metrics.execution_attempts == 0


def test_json_csv_and_bankroll_exports(tmp_path: Path) -> None:
    report = run_replay(load())
    export_report(report, tmp_path)
    assert (
        BacktestReport.model_validate_json((tmp_path / "report.json").read_text())
        == report
    )
    with (tmp_path / "trades.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert (
        sum(
            (
                Decimal(row["position_realised_profit"])
                for row in rows
                if row["position_realised_profit"]
            ),
            Decimal(0),
        )
        == report.metrics.realised_pnl
    )
    with (tmp_path / "bankroll.csv").open(newline="", encoding="utf-8") as handle:
        history = list(csv.DictReader(handle))
    assert len(history) == len(report.bankroll_history)
    assert Decimal(history[-1]["cash"]) == report.metrics.ending_cash
    assert "Realised hypothetical P&L: GBP 14.20" in terminal_summary(report)


def test_cli_exports_from_user_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                str(HISTORIES / "a_success.json"),
                "--bankroll",
                "500",
                "--stake",
                "10",
                "--min-roi",
                "1",
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert "Paper backtest:" in capsys.readouterr().out
    report = BacktestReport.model_validate_json((tmp_path / "report.json").read_text())
    assert report.config.starting_bankroll == Decimal("500")
    assert report.metrics.total_stakes == Decimal("10")


@pytest.mark.parametrize(
    "args", [["missing.json"], [str(HISTORIES / "a_success.json"), "--bankroll", "-1"]]
)
def test_cli_errors_are_clear(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def test_replay_independent_of_external_decimal_context() -> None:
    expected = run_replay(load())
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        actual = run_replay(load())
    assert actual == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("starting_bankroll", "0"),
        ("stake_budget", "1.001"),
        ("minimum_roi", "-1"),
        ("slippage", "1"),
        ("slippage", "NaN"),
        ("slippage", 0.1),
        ("execution_delay", timedelta(seconds=-1)),
    ],
)
def test_invalid_simulation_configuration(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ReplayConfig.model_validate({field: value})


@pytest.mark.parametrize(
    "field", ["snapshots", "settlements", "execution_instructions"]
)
def test_duplicate_historical_records_rejected(field: str) -> None:
    data = load("e_rejection").model_dump(mode="json")
    data[field].append(data[field][0])
    with pytest.raises(ValidationError):
        HistoricalDataset.model_validate(data)


def test_duplicate_current_quote_is_not_a_second_executable_offer() -> None:
    data = load().model_dump(mode="json")
    quotes = data["snapshots"][0]["snapshot"]["quotes"]
    quotes.append(quotes[0])
    with pytest.raises(ValidationError, match="one current offer"):
        HistoricalDataset.model_validate(data)


def test_history_inventory() -> None:
    assert len(list(HISTORIES.glob("*.json"))) == 10


def test_delayed_execution_uses_current_event_start_for_settlement() -> None:
    data = load("j_repeated").model_dump(mode="json")
    for record in data["snapshots"][1:]:
        record["snapshot"]["event"]["scheduled_start"] = "2026-06-01T15:00:00Z"
    report = run_replay(
        HistoricalDataset.model_validate(data),
        ReplayConfig(execution_delay=timedelta(seconds=1)),
    )
    assert report.trades[0].event_start.hour == 15
    assert report.metrics.realised_pnl == 0
    assert report.metrics.unsettled_exposure == Decimal("100")


def test_opportunity_identity_preserves_long_decimal_price_changes() -> None:
    data = load("j_repeated").model_dump(mode="json")
    data["snapshots"] = data["snapshots"][:2]
    for quote in data["snapshots"][1]["snapshot"]["quotes"]:
        if quote["bookmaker_id"] == "book-1" and quote["outcome_id"] == "a-win-a":
            quote["decimal_odds"] = "2.2" + "0" * 90 + "1"
    report = run_replay(HistoricalDataset.model_validate(data))
    assert report.metrics.mathematical_candidates == 2
    assert report.metrics.eligible_opportunities == 2
