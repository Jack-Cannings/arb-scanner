"""Offline historical CSV analysis CLI; no credentials or network clients."""

import argparse
import csv
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from betting_scanner.research.analysis import analyse
from betting_scanner.research.importer import import_directory
from betting_scanner.research.investigation import investigate
from betting_scanner.research.investigation_reporting import export_investigation
from betting_scanner.research.reporting import export_report, terminal_summary
from betting_scanner.strategies.arbitrage.calculation import validate_budget


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unverified historical tennis CSV research"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    command = subcommands.add_parser("analyse")
    command.add_argument("input_directory", type=Path)
    command.add_argument("--tour", choices=("atp", "wta", "both"), default="both")
    command.add_argument("--stake", default="100")
    command.add_argument(
        "--min-roi", default="0", help="Minimum theoretical ROI percent"
    )
    command.add_argument(
        "--output-dir", type=Path, default=Path("data/research-output")
    )
    investigation = subcommands.add_parser("investigate")
    investigation.add_argument("input_directory", type=Path)
    investigation.add_argument("--baseline", type=Path)
    investigation.add_argument(
        "--output-dir", type=Path, default=Path("data/investigation-output")
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "investigate":
            audit = investigate(args.input_directory, args.baseline)
            export_investigation(audit, args.output_dir)
            print(
                f"Unverified candidates: {audit.candidate_count}; "
                f"suspicious cases: {audit.suspected_case_count}; "
                "confirmed missing/invalid records: "
                f"{len(audit.confirmed_anomalies)}"
            )
            print(
                f"{len(audit.arithmetic_checks)} independent sample checks; "
                f"{audit.sign_or_decimal_discrepancies} full-data "
                "sign/conversion discrepancies"
            )
            print(f"Reports: {args.output_dir}")
            return 0
        # Validate parameters before reading potentially large inputs.
        stake = validate_budget(Decimal(args.stake))
        minimum = Decimal(args.min_roi)
        if not minimum.is_finite() or minimum < 0:
            raise ValueError("Minimum ROI must be finite and nonnegative")
        imported = import_directory(args.input_directory, args.tour)
        report = analyse(imported, stake, minimum)
        export_report(report, args.output_dir)
    except (OSError, ValueError, InvalidOperation, csv.Error) as exc:
        parser.error(str(exc))
    print(terminal_summary(report))
    print(f"Reports: {args.output_dir}")
    return 0
