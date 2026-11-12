"""Command-line interface for offline historical replay."""

import argparse
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from pydantic import TypeAdapter

from betting_scanner.backtesting.models import (
    HistoricalDataset,
    ReplayConfig,
    SettlementRecord,
)
from betting_scanner.backtesting.reader import JsonHistoricalReader
from betting_scanner.backtesting.replay import run_replay
from betting_scanner.backtesting.reporting import export_report, terminal_summary
from betting_scanner.providers.odds_api.reader import SavedHistoricalReader


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay historical odds with a virtual GBP bankroll."
    )
    parser.add_argument("fixture", type=Path, help="Path to normalised historical JSON")
    parser.add_argument(
        "--results",
        type=Path,
        help="Separate JSON array of explicit settlement evidence",
    )
    parser.add_argument("--bankroll", default="1000.00")
    parser.add_argument("--stake", default="100.00")
    parser.add_argument("--min-roi", default="0")
    parser.add_argument("--min-profit", default="0")
    parser.add_argument("--max-event-exposure", default="100.00")
    parser.add_argument("--max-aggregate-exposure", default="500.00")
    parser.add_argument(
        "--execution-delay", type=int, default=0, help="Delay in whole seconds"
    )
    parser.add_argument(
        "--slippage", default="0.005", help="Proportional adverse odds haircut"
    )
    parser.add_argument(
        "--reject-last",
        action="store_true",
        help="Simulate rejection of the last outcome's bet",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("backtest-output"))
    args = parser.parse_args(argv)
    try:
        config = ReplayConfig.model_validate(
            {
                "starting_bankroll": args.bankroll,
                "stake_budget": args.stake,
                "minimum_roi": args.min_roi,
                "minimum_profit": args.min_profit,
                "max_event_exposure": args.max_event_exposure,
                "max_aggregate_exposure": args.max_aggregate_exposure,
                "execution_delay": timedelta(seconds=args.execution_delay),
                "slippage": args.slippage,
                "execution_mode": "reject_last" if args.reject_last else "accept_all",
            }
        )
        dataset = (
            SavedHistoricalReader(args.fixture).read()
            if args.fixture.is_dir()
            else JsonHistoricalReader(args.fixture).read()
        )
        if args.results is not None:
            results = TypeAdapter(tuple[SettlementRecord, ...]).validate_json(
                args.results.read_text(encoding="utf-8")
            )
            data = dataset.model_dump()
            data["settlements"] = tuple(
                r.model_dump() for r in (*dataset.settlements, *results)
            )
            dataset = HistoricalDataset.model_validate(data)
        report = run_replay(dataset, config)
        export_report(report, args.output_dir)
    except (ValueError, OSError, ArithmeticError) as error:
        parser.error(str(error))
    print(terminal_summary(report))
    print(f"Reports: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
