"""Read the inspected ATP/WTA CSV schema without doing arbitrage maths."""

import csv
import hashlib
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from betting_scanner.research.models import (
    ImportResult,
    InputEvidence,
    ResearchMatch,
    SkippedRow,
    SourceRow,
    Tour,
)

REQUIRED = {
    "match_id",
    "tourney_id",
    "tourney_name",
    "tourney_date",
    "winner_id",
    "winner_name",
    "loser_id",
    "loser_name",
    "winner_best_close_odds",
    "loser_best_close_odds",
}


def _parse(row: SourceRow) -> ResearchMatch:
    raw = row.raw
    missing = sorted(key for key in REQUIRED if not raw.get(key, "").strip())
    if missing:
        raise ValueError("missing_fields: " + ", ".join(missing))
    if raw["winner_id"].strip() == raw["loser_id"].strip():
        raise ValueError("identical_participant_ids")
    date_text = raw["tourney_date"].strip()
    if len(date_text) != 8 or not date_text.isdecimal():
        raise ValueError("invalid_tournament_date: expected YYYYMMDD")
    tournament_date = datetime.strptime(date_text, "%Y%m%d").date()
    warnings: list[str] = []
    count_text = raw.get("bookmakers", "").strip()
    count = None
    if count_text:
        if count_text.isdecimal():
            count = int(count_text)
        else:
            warnings.append("invalid_bookmaker_count")
    match = ResearchMatch.model_validate(
        {
            **row.model_dump(),
            "match_id": raw["match_id"].strip(),
            "tournament_id": raw["tourney_id"].strip(),
            "tournament_name": raw["tourney_name"],
            "tournament_date": tournament_date,
            "match_start": raw.get("start_time_utc", "").strip() or None,
            "winner_id": raw["winner_id"].strip(),
            "winner_name": raw["winner_name"],
            "loser_id": raw["loser_id"].strip(),
            "loser_name": raw["loser_name"],
            "winner_odds": raw["winner_best_close_odds"].strip(),
            "loser_odds": raw["loser_best_close_odds"].strip(),
            "score": raw.get("score", ""),
            "bookmaker_count": count,
        }
    )
    if max(match.winner_odds, match.loser_odds) >= 100:
        warnings.append("unusual_price_at_least_100")
    if not match.score.strip():
        warnings.append("missing_result")
    if match.match_start is None:
        warnings.append("missing_match_start")
    elif match.match_start.year != 2025:
        warnings.append("match_start_outside_filename_year_2025")
    return match.model_copy(update={"warnings": tuple(warnings)})


def import_directory(directory: Path, tour: str = "both") -> ImportResult:
    """Fail on invalid file schemas; reject individual bad rows with evidence.

    Conflicting identities are removed in their entirety, independent of row order.
    Exact duplicates retain one row. Tour namespaces keep ATP/WTA IDs distinct.
    """
    if tour not in {"atp", "wta", "both"}:
        raise ValueError("tour must be atp, wta or both")
    tours: tuple[Tour, ...] = (
        ("atp", "wta") if tour == "both" else (("atp",) if tour == "atp" else ("wta",))
    )
    inputs: list[InputEvidence] = []
    skipped: list[SkippedRow] = []
    rows: list[SourceRow] = []
    quality: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)

    def flag(code: str, reference: str) -> None:
        quality[code] += 1
        if len(examples[code]) < 5:
            examples[code].append(reference)

    for selected in tours:
        path = directory / f"{selected}_matches_2025.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            columns = reader.fieldnames or []
            if len(set(columns)) != len(columns) or not REQUIRED.issubset(columns):
                raise ValueError(
                    f"{path.name}: missing required or duplicate CSV columns"
                )
            count = 0
            for raw in reader:
                count += 1
                # csv.DictReader signals surplus cells with a None key; preserve them.
                malformed = None in raw or any(value is None for value in raw.values())
                preserved = {
                    str(key) if key is not None else "__extra_cells__": value
                    if isinstance(value, str)
                    else repr(value)
                    for key, value in raw.items()
                }
                row = SourceRow(
                    source_file=path.name,
                    line=reader.line_num,
                    tour=selected,
                    raw=preserved,
                )
                if malformed:
                    skipped.append(
                        SkippedRow(**row.model_dump(), reasons=("malformed_csv_width",))
                    )
                    flag("malformed_csv_width", f"{path.name}:{reader.line_num}")
                else:
                    rows.append(row)
        inputs.append(
            InputEvidence(
                source_file=path.name,
                tour=selected,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                columns=tuple(columns),
                rows=count,
            )
        )

    # Group BEFORE price validation: an invalid conflicting copy cannot let a
    # convenient valid price version slip through. Secondary source identity too.
    identities: dict[tuple[str, ...], list[SourceRow]] = defaultdict(list)
    for row in rows:
        raw = row.raw
        if raw.get("match_id", "").strip():
            identities[(row.tour, "match", raw["match_id"].strip())].append(row)
        if raw.get("tourney_id", "").strip() and raw.get("match_num", "").strip():
            identities[
                (
                    row.tour,
                    "tournament_match",
                    raw["tourney_id"].strip(),
                    raw["match_num"].strip(),
                )
            ].append(row)
    conflicts: set[tuple[str, int]] = set()
    for group in identities.values():
        if len({tuple(sorted(row.raw.items())) for row in group}) > 1:
            conflicts.update((row.source_file, row.line) for row in group)
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    matches: list[ResearchMatch] = []
    for row in rows:
        reference = f"{row.source_file}:{row.line}"
        signature = (row.tour, tuple(sorted(row.raw.items())))
        try:
            if (row.source_file, row.line) in conflicts:
                raise ValueError("conflicting_duplicate_identity")
            if signature in seen:
                raise ValueError("exact_duplicate_row")
            seen.add(signature)
            match = _parse(row)
        except (ValueError, ValidationError) as exc:
            skipped.append(SkippedRow(**row.model_dump(), reasons=(str(exc),)))
            flag("skipped_row", reference)
            if str(exc) in {"exact_duplicate_row", "conflicting_duplicate_identity"}:
                flag(str(exc), reference)
            if any(
                not row.raw.get(f"{side}_best_close_odds", "").strip()
                for side in ("winner", "loser")
            ):
                flag("missing_odds", reference)
            if isinstance(exc, ValidationError):
                fields = {error["loc"][0] for error in exc.errors()}
                if fields.intersection({"winner_odds", "loser_odds"}):
                    flag("invalid_odds", reference)
            if any(
                not row.raw.get(key, "").strip()
                for key in ("match_id", "winner_id", "loser_id", "tourney_id")
            ):
                flag("missing_identifier", reference)
            continue
        matches.append(match)
        for warning in match.warnings:
            flag(warning, reference)

    # Check identifiers and names across all source records, including rejected prices.
    id_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    name_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    tournament_names: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        for side in ("winner", "loser"):
            identifier = row.raw.get(f"{side}_id", "").strip()
            name = row.raw.get(f"{side}_name", "").strip()
            if identifier and name:
                id_names[(row.tour, identifier)].add(name)
                name_ids[(row.tour, name.casefold())].add(identifier)
        tournament_names[(row.tour, row.raw.get("tourney_id", ""))].add(
            row.raw.get("tourney_name", "")
        )
    for code, groups in (
        ("player_id_multiple_names", id_names),
        ("player_name_multiple_ids", name_ids),
        ("tournament_id_multiple_names", tournament_names),
    ):
        for key, values in groups.items():
            if len(values) > 1:
                flag(code, f"{key}: {sorted(values)}")
    for code in (
        "missing_odds",
        "invalid_odds",
        "missing_identifier",
        "exact_duplicate_row",
        "conflicting_duplicate_identity",
        "malformed_csv_width",
        "missing_result",
        "missing_match_start",
        "unusual_price_at_least_100",
        "player_id_multiple_names",
        "player_name_multiple_ids",
        "tournament_id_multiple_names",
    ):
        quality.setdefault(code, 0)
    quality["skipped_row"] = len(skipped)
    # These are absent from the inspected schema, not merely absent in valid rows.
    total_rows = sum(source.rows for source in inputs)
    quality["unknown_bookmaker_identity"] = total_rows
    quality["unknown_odds_observation_time"] = total_rows
    quality["unproven_price_simultaneity"] = total_rows
    quality["result_labelled_rows_potential_leakage"] = total_rows
    return ImportResult(
        inputs=tuple(inputs),
        matches=tuple(matches),
        skipped=tuple(sorted(skipped, key=lambda r: (r.source_file, r.line))),
        quality=dict(quality),
        quality_examples={key: tuple(value) for key, value in examples.items()},
    )
