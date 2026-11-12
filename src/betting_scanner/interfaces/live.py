"""Cost-estimated, opt-in single scans and bounded local monitoring."""

import argparse
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from betting_scanner.config import load_settings
from betting_scanner.live.models import LiveScanPlan
from betting_scanner.live.monitor_models import (
    MonitoringReport,
    MonitorPlan,
    StopReason,
)
from betting_scanner.live.monitor_reporting import (
    completion_summary,
    estimate_summary,
    monitoring_scan_summary,
)
from betting_scanner.live.monitoring import estimate_monitor, monitor
from betting_scanner.live.reporting import scan_summary
from betting_scanner.live.scanner import discover_live, scan_live
from betting_scanner.providers.odds_api.client import (
    ClientOptions,
    OddsApiClient,
    OddsApiError,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot current ATP/WTA singles odds research; never places bets"
    )
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--retries", type=int, default=1)
    parser.set_defaults(command="discover", output_dir=Path("data/live-tennis"))
    commands = parser.add_subparsers(dest="command")
    discover = commands.add_parser("discover")
    discover.add_argument("--output-dir", type=Path, default=Path("data/live-tennis"))
    scan = commands.add_parser("scan")
    repeated = commands.add_parser("monitor")
    for command in (scan, repeated):
        command.add_argument(
            "--sport-key",
            action="append",
            required=True,
            help="Repeat for each explicitly selected active singles key",
        )
        command.add_argument("--stake", default="100.00")
        command.add_argument(
            "--max-credits", type=int, default=5 if command is scan else 10
        )
        command.add_argument(
            "--max-age",
            type=int,
            default=300,
            help="Maximum provider quote age in seconds",
        )
        command.add_argument(
            "--output-dir", type=Path, default=Path("data/live-tennis")
        )
        command.add_argument("--execute", action="store_true")
    repeated.add_argument(
        "--interval", type=float, default=300, help="Seconds between scan starts"
    )
    repeated.add_argument("--max-scans", type=int, default=10)
    repeated.add_argument(
        "--duration",
        type=float,
        help="Maximum duration in seconds; earliest limit wins",
    )
    args = parser.parse_args(argv)
    try:
        options = ClientOptions(timeout=args.timeout, max_retries=args.retries)
        if args.command in {None, "discover"}:
            print(
                "Active singles discovery costs 0 credits; "
                "no competition odds are requested."
            )
            sports = discover_live(
                args.output_dir,
                lambda ledger: OddsApiClient(load_settings(), ledger, options=options),
            )
            for sport in sports:
                print(
                    f"{sport.title} | {sport.key} | {sport.description} | "
                    "match odds availability not checked"
                )
            if not sports:
                print(
                    "No active ATP/WTA competitions with explicit singles "
                    "metadata were returned."
                )
            return 0
        plan = LiveScanPlan.model_validate(
            {
                "sport_keys": tuple(args.sport_key),
                "stake": args.stake,
                "engine": {"max_quote_age": timedelta(seconds=args.max_age)},
            }
        )
        if args.max_credits < 0:
            raise ValueError("Credit cap must be nonnegative")
        if args.command == "monitor":
            monitoring_plan = MonitorPlan(
                scan=plan,
                interval=args.interval,
                max_credits=args.max_credits,
                max_scans=args.max_scans,
                duration=args.duration,
            )
            print(
                estimate_summary(
                    estimate_monitor(monitoring_plan, args.output_dir, options)
                )
            )
            if not args.execute:
                print(
                    "Dry run only. Add --execute to start finite monitoring; "
                    "Ctrl+C stops it."
                )
                return 0
            session = monitor(
                monitoring_plan,
                args.output_dir,
                execute=True,
                options=options,
                client_factory=lambda ledger: OddsApiClient(
                    load_settings(), ledger, options=options
                ),
                on_scan=lambda point, state: print(
                    monitoring_scan_summary(point, state)
                ),
            )
            assert isinstance(session, MonitoringReport)
            print(completion_summary(session, args.output_dir))
            return (
                1
                if session.stop_reason
                in {StopReason.AUTHENTICATION, StopReason.PROVIDER_ERROR}
                else 0
            )
        print(f"Selected competitions: {', '.join(plan.sport_keys)}")
        print(
            f"Base cost: up to {len(plan.sport_keys)} credits; "
            f"retry-inclusive bound: {plan.estimate(options.max_retries)} credits."
        )
        print(
            f"Archive-wide credit cap: {args.max_credits}. "
            f"Hypothetical stake: GBP {plan.stake:.2f}."
        )
        if not args.execute:
            print(
                "Dry run: no API requests or files written. Add --execute for one scan."
            )
            return 0
        report = scan_live(
            plan,
            args.output_dir,
            max_credits=args.max_credits,
            execute=True,
            client_factory=lambda ledger: OddsApiClient(
                load_settings(), ledger, options=options
            ),
        )
        assert report is not None
        print(scan_summary(report))
        print(f"Archive: {args.output_dir.resolve()}")
        return 1 if report.errors else 0
    except ValidationError:
        parser.error(
            "Invalid live arguments or dataset; "
            "check keys, stake, thresholds and archive format."
        )
    except (ValueError, OSError, OddsApiError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
