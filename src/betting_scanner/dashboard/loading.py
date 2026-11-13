"""Confined, validated file reads. Never instantiate archive writers or clients."""

import csv
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import streamlit as st
from pydantic import BaseModel

from betting_scanner.live.models import LiveManifest, LiveObservation
from betting_scanner.live.monitor_models import MonitoringReport


def confined(root: Path, relative: str | Path) -> Path:
    candidate = Path(relative)
    if (
        candidate.is_absolute()
        or PurePosixPath(str(relative)).is_absolute()
        or PureWindowsPath(str(relative)).drive
        or ".." in candidate.parts
        or ".." in PureWindowsPath(str(relative)).parts
    ):
        raise ValueError("Select a saved output inside the configured roots")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Output path escapes its configured root")
    return resolved


@dataclass(frozen=True)
class Paths:
    project: Path

    @property
    def data(self) -> Path:
        return confined(self.project, "data")

    @property
    def live(self) -> Path:
        return confined(self.project, "data/live-tennis")

    @property
    def output_roots(self) -> tuple[Path, ...]:
        return (
            confined(self.project, "backtest-output"),
            confined(self.project, "data/backtests"),
        )


@dataclass(frozen=True)
class Loaded[T]:
    value: T | None
    notice: str | None = None


def signature(path: Path) -> tuple[int, int, int]:
    stat = path.stat()
    if stat.st_size > 100_000_000:
        raise ValueError("Report exceeds the local dashboard size limit")
    return stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns


@st.cache_data(show_spinner=False, max_entries=48)
def _cached_model[T: BaseModel](
    path: str, version: tuple[int, int, int], kind: str, _model: type[T]
) -> T:
    result = _model.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
    if signature(Path(path)) != version:
        raise ValueError("Report changed while reading")
    return result


def load_model[T: BaseModel](
    root: Path, relative: str | Path, model: type[T]
) -> Loaded[T]:
    try:
        path = confined(root, relative)
        result = _cached_model(
            str(path), signature(path), model.__module__ + "." + model.__name__, model
        )
        return Loaded(result)
    except (OSError, ValueError):
        # Never render validation inputs, provider bodies or arbitrary exception text.
        return Loaded(
            None,
            f"{Path(relative).name}: missing, incomplete or invalid saved data. "
            "Save a complete report and refresh.",
        )


@st.cache_data(show_spinner=False, max_entries=32)
def _cached_csv(path: str, version: tuple[int, int, int]) -> tuple[dict[str, str], ...]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        columns = reader.fieldnames or []
        if not columns or len(columns) != len(set(columns)):
            raise ValueError("Invalid CSV header")
        rows: list[dict[str, str]] = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Incomplete CSV row")
            if len(rows) >= 50000:
                raise ValueError("CSV display limit exceeded")
            rows.append(row)
    if signature(Path(path)) != version:
        raise ValueError("CSV changed while reading")
    return tuple(rows)


def load_csv(root: Path, relative: str | Path) -> Loaded[tuple[dict[str, str], ...]]:
    try:
        path = confined(root, relative)
        return Loaded(_cached_csv(str(path), signature(path)))
    except (OSError, ValueError, csv.Error):
        return Loaded(None, f"{Path(relative).name}: no complete valid CSV available.")


@dataclass(frozen=True)
class LiveData:
    manifest: LiveManifest | None
    observations: tuple[LiveObservation, ...]
    sessions: tuple[MonitoringReport, ...]
    notices: tuple[str, ...]


def load_live(paths: Paths) -> LiveData:
    manifest = load_model(paths.live, "manifest.json", LiveManifest)
    observations: dict[str, LiveObservation] = {}
    sessions: list[MonitoringReport] = []
    notices = [manifest.notice] if manifest.notice else []
    if manifest.value is not None:
        for index in manifest.value.observations.values():
            item = load_model(
                paths.live, f"observations/{index.observation_id}.json", LiveObservation
            )
            if (
                item.value is not None
                and item.value.id == index.observation_id
                and item.value.observed_at == index.observed_at
            ):
                observations[item.value.id] = item.value
            else:
                notices.append(
                    "An indexed observation is unavailable or inconsistent; "
                    "it was omitted."
                )
        if manifest.value.conflicted_observations:
            notices.append(
                "Conflicting observations excluded by the archive remain excluded."
            )
    try:
        for path in confined(paths.live, "monitoring").glob("monitor-*.json"):
            session_item = load_model(
                paths.live, path.relative_to(paths.live), MonitoringReport
            )
            if session_item.value is not None:
                sessions.append(session_item.value)
            elif session_item.notice:
                notices.append(session_item.notice)
    except (OSError, ValueError):
        notices.append("Monitoring history is unavailable.")
    return LiveData(
        manifest.value,
        tuple(sorted(observations.values(), key=lambda o: (o.observed_at, o.id))),
        tuple(sorted(sessions, key=lambda s: (s.started_at, s.id))),
        tuple(notices),
    )


def backtest_directories(paths: Paths) -> tuple[Path, ...]:
    found: list[Path] = []
    for root in paths.output_roots:
        try:
            for path in root.rglob("report.json"):
                safe = confined(root, path.relative_to(root))
                found.append(safe.parent)
        except (OSError, ValueError):
            continue
    return tuple(sorted(set(found)))
