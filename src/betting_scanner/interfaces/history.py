"""Explicit cost estimates, bounded discovery and historical-only downloading."""

import argparse
import re
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from pydantic import TypeAdapter

from betting_scanner.config import load_settings
from betting_scanner.core.models import UTCTimestamp
from betting_scanner.providers.odds_api.client import (
    BudgetState,
    ClientOptions,
    CreditLedger,
    HistoricalOddsClient,
    OddsApiError,
)
from betting_scanner.providers.odds_api.download import DownloadPlan, download
from betting_scanner.providers.odds_api.schema import ApiEvent, parse_page


def _interval(value: str) -> timedelta:
    match = re.fullmatch(r"([1-9][0-9]*)(m|h)", value)
    if match is None:
        raise ValueError("Interval must use integer minutes/hours, e.g. 5m or 1h")
    return timedelta(minutes=int(match[1]) * (60 if match[2] == "h" else 1))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cost-bounded The Odds API historical tennis data"
    )
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--retries", type=int, default=1)
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--sport", choices=["tennis"], default="tennis")
    discover.add_argument("--sport-key")
    discover.add_argument("--date")
    discover.add_argument("--max-credits", type=int, default=10)
    discover.add_argument("--execute", action="store_true")
    for command in ("estimate", "download"):
        sub = commands.add_parser(command)
        sub.add_argument("--sport-key", required=True)
        sub.add_argument("--start", required=True)
        sub.add_argument("--end", required=True)
        sub.add_argument("--interval", default="5m")
        sub.add_argument(
            "--region", choices=["uk", "us", "us2", "eu", "au"], default="uk"
        )
        sub.add_argument("--market", choices=["h2h"], default="h2h")
        if command == "download":
            sub.add_argument("--output-dir", type=Path, required=True)
            sub.add_argument("--max-credits", type=int, default=100)
            sub.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        options = ClientOptions(timeout=args.timeout, max_retries=args.retries)
        if args.command == "discover":
            if args.execute and (not args.date or not args.sport_key):
                raise ValueError(
                    "Dated discovery requires --date and one explicit --sport-key"
                )
            date = (
                TypeAdapter(UTCTimestamp).validate_python(args.date)
                if args.date
                else None
            )
            print(
                "Sport listing cost: 0 credits; current listings do not "
                "confirm historical coverage."
            )
            if date is not None:
                print(
                    "Dated event discovery: up to 1 credit per attempt; "
                    f"upper bound {options.max_retries + 1} including retries."
                )
            ledger = CreditLedger(BudgetState(max_credits=args.max_credits))
            with HistoricalOddsClient(
                load_settings(), ledger, options=options
            ) as client:
                for sport in client.sports():
                    if sport.is_tennis:
                        print(
                            f"{sport.title} | {sport.key} | active={sport.active} | "
                            "historical=not checked"
                        )
                if args.execute and date is not None:
                    try:
                        record = client.events(args.sport_key, date)
                        page = parse_page(record)
                        events = tuple(
                            ApiEvent.model_validate(event) for event in page.data
                        )
                        if any(event.sport_key != args.sport_key for event in events):
                            raise ValueError("Discovery returned a different sport key")
                        title = events[0].sport_title if events else args.sport_key
                        print(
                            f"{title} | {args.sport_key} | "
                            f"actual={page.timestamp.isoformat()} | "
                            f"historical events={len(events)}"
                        )
                        print(
                            "Event coverage does not prove UK bookmaker/h2h "
                            "availability."
                        )
                    except OddsApiError as error:
                        print(f"Historical coverage not confirmed: {error}")
                        print(
                            f"Credits: actual={ledger.state.actual_credits}; "
                            f"accounted={ledger.state.accounted_credits}"
                        )
                        return 1
                elif date is not None:
                    print(
                        "No dated calls made. Add --sport-key KEY --execute "
                        "to spend discovery credits."
                    )
            print(
                f"Credits: actual={ledger.state.actual_credits}; "
                f"accounted={ledger.state.accounted_credits}"
            )
            return 0
        plan = DownloadPlan.model_validate(
            {
                "sport_key": args.sport_key,
                "start": args.start,
                "end": args.end,
                "interval": _interval(args.interval),
                "region": args.region,
                "market": args.market,
            }
        )
        print(
            f"Requested snapshots: {len(plan.dates())}; "
            f"region={plan.region}; market=h2h"
        )
        print(
            "Cost: up to 10 credits/request; upper bound including retries: "
            f"{plan.estimate(options.max_retries)} credits."
        )
        if args.command == "estimate" or not args.execute:
            print(
                "Dry run: no API requests or files written. "
                "Paid historical download requires --execute."
            )
            return 0
        print(
            "Total dataset credit cap (including prior attempts on resume): "
            f"{args.max_credits}"
        )
        manifest = download(
            plan,
            args.output_dir,
            max_credits=args.max_credits,
            execute=True,
            client_factory=lambda ledger: HistoricalOddsClient(
                load_settings(), ledger, options=options
            ),
        )
        assert manifest is not None
        print(
            f"Saved {manifest.number_of_snapshots} snapshots, "
            f"{manifest.number_of_events} events, {manifest.number_of_quotes} quotes."
        )
        print(
            f"Credits: actual={manifest.actual_api_credits}; "
            f"accounted={manifest.accounted_api_credits}; "
            f"stop={manifest.stop_reason or 'complete'}"
        )
        print(
            f"Missing/unrequested slots: {len(manifest.missing_snapshots)}; "
            f"normalisation issues: {len(manifest.normalisation_issues)}"
        )
        return 0 if manifest.stop_reason in {None, "credit_budget"} else 1
    except (ValueError, OSError, OddsApiError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
