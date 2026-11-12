"""Append-only raw receipts, deduplicated observations and offline replay data."""

from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from betting_scanner.backtesting.models import HistoricalDataset, HistoricalSnapshot
from betting_scanner.live.models import (
    LiveManifest,
    LiveObservation,
    LiveRequest,
    LiveScanPlan,
    ObservationIndex,
)
from betting_scanner.providers.odds_api.client import BudgetState
from betting_scanner.providers.odds_api.normalise import normalise_live
from betting_scanner.providers.odds_api.schema import ResponseRecord
from betting_scanner.storage.files import atomic_write
from betting_scanner.strategies.arbitrage import detect_arbitrage


class LiveArchive:
    """Use only while holding the shared directory lock."""

    def __init__(
        self, output: Path, max_credits: int | None, clock: Callable[[], datetime]
    ) -> None:
        self.output = output
        self.clock = clock
        path = output / "manifest.json"
        if path.exists():
            self.manifest = LiveManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if max_credits is not None:
                self.manifest = self.manifest.model_copy(
                    update={
                        "budget": BudgetState(
                            max_credits=max_credits,
                            attempts=self.manifest.budget.attempts,
                        )
                    }
                )
        else:
            now = clock()
            self.manifest = LiveManifest(
                created_at=now,
                updated_at=now,
                budget=BudgetState(
                    max_credits=max_credits if max_credits is not None else 0
                ),
            )

    def save(self) -> None:
        self.manifest = self.manifest.model_copy(update={"updated_at": self.clock()})
        atomic_write(self.output / "manifest.json", self.manifest)

    def save_budget(self, state: BudgetState) -> None:
        self.manifest = self.manifest.model_copy(update={"budget": state})
        self.save()

    def begin(self, sport_key: str, plan: LiveScanPlan) -> LiveRequest:
        entry = LiveRequest(
            id=f"request-{len(self.manifest.requests) + 1:06d}",
            sport_key=sport_key,
            plan=plan,
        )
        self.manifest = self.manifest.model_copy(
            update={"requests": (*self.manifest.requests, entry)}
        )
        self.save()
        return entry

    def update(self, entry: LiveRequest) -> None:
        entries = tuple(
            entry if e.id == entry.id else e for e in self.manifest.requests
        )
        self.manifest = self.manifest.model_copy(update={"requests": entries})
        self.save()

    def raw_path(self, entry: LiveRequest) -> Path:
        return self.output / "raw" / f"{entry.id}.json"

    def fail(
        self, entry: LiveRequest, reason: str, response: ResponseRecord | None = None
    ) -> None:
        entry = next(e for e in self.manifest.requests if e.id == entry.id)
        if response is not None:
            atomic_write(self.raw_path(entry), response)
        self.update(
            entry.model_copy(
                update={
                    "status": "error" if response else "uncertain",
                    "reason": reason,
                }
            )
        )

    def record(self, entry: LiveRequest, response: ResponseRecord) -> LiveObservation:
        atomic_write(self.raw_path(entry), response)
        if (
            response.status_code != 200
            or response.endpoint != f"sports/{entry.sport_key}/odds"
        ):
            raise ValueError("Unexpected live response endpoint/status")
        page = normalise_live(response, entry.sport_key)
        excluded = tuple(
            s.snapshot.event.id
            for s in page.snapshots
            if s.snapshot.event.scheduled_start <= response.collected_at
        )
        analyses = tuple(
            detect_arbitrage(
                s.snapshot,
                market.id,
                total_stake=entry.plan.stake,
                evaluated_at=response.collected_at,
                config=entry.plan.engine,
            )
            for s in page.snapshots
            if s.snapshot.event.id not in excluded
            for market in s.snapshot.markets
        )
        digest = sha256(response.body.encode("utf-8")).hexdigest()
        key = f"{entry.sport_key}:{response.collected_at.isoformat()}"
        observation_id = (
            "observation-" + sha256((key + digest).encode()).hexdigest()[:32]
        )
        observation = LiveObservation(
            id=observation_id,
            sport_key=entry.sport_key,
            observed_at=response.collected_at,
            normalised=page,
            analyses=analyses,
            excluded_started=excluded,
            plan=entry.plan,
        )
        path = self.output / "observations" / f"{observation_id}.json"
        if not path.exists():
            atomic_write(path, observation)
        previous = self.manifest.observations.get(key)
        if key in self.manifest.conflicted_observations or (
            previous is not None and previous.body_digest != digest
        ):
            indices = dict(self.manifest.observations)
            indices.pop(key, None)
            self.manifest = self.manifest.model_copy(
                update={
                    "observations": indices,
                    "conflicted_observations": tuple(
                        sorted(set((*self.manifest.conflicted_observations, key)))
                    ),
                }
            )
            self.update(
                entry.model_copy(
                    update={
                        "status": "error",
                        "observation_id": observation_id,
                        "reason": "Conflicting payloads at identical observation time",
                    }
                )
            )
            self.rebuild()
            raise ValueError(
                "Conflicting payloads at identical observation time; "
                "excluded from replay"
            )
        indices = dict(self.manifest.observations)
        indices[key] = ObservationIndex(
            observation_id=observation_id,
            body_digest=digest,
            observed_at=response.collected_at,
        )
        self.manifest = self.manifest.model_copy(update={"observations": indices})
        self.update(
            entry.model_copy(
                update={
                    "status": "duplicate"
                    if previous
                    else "complete"
                    if page.snapshots
                    else "error"
                    if page.issues
                    else "empty",
                    "observation_id": observation_id,
                    "reason": "All event records invalid"
                    if page.issues and not page.snapshots
                    else None,
                }
            )
        )
        return observation

    def recover(self) -> None:
        for entry in self.manifest.requests:
            if entry.status != "in_flight":
                continue
            path = self.raw_path(entry)
            if not path.exists():
                self.fail(
                    entry,
                    "Interrupted request; unknown result/charge; reservation retained",
                )
                continue
            try:
                self.record(
                    entry,
                    ResponseRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    ),
                )
            except ValueError:
                self.update(
                    entry.model_copy(
                        update={
                            "status": "error",
                            "reason": "Invalid cached live response",
                        }
                    )
                )
        self.rebuild()

    def rebuild(self) -> None:
        snapshots: dict[tuple[datetime, str], HistoricalSnapshot] = {}
        events: set[str] = set()
        quotes = 0
        for index in self.manifest.observations.values():
            observation = LiveObservation.model_validate_json(
                (
                    self.output / "observations" / f"{index.observation_id}.json"
                ).read_text(encoding="utf-8")
            )
            for snapshot in observation.normalised.snapshots:
                events.add(snapshot.snapshot.event.id)
                quotes += len(snapshot.snapshot.quotes)
                if snapshot.snapshot.event.scheduled_start > observation.observed_at:
                    snapshots[(snapshot.observed_at, snapshot.snapshot.event.id)] = (
                        snapshot
                    )
        if snapshots:
            dataset = HistoricalDataset(
                name="Recorded live tennis odds",
                description="Local receipt-time observations; settlement "
                "unverified; no invented fills or results.",
                snapshots=tuple(snapshots[key] for key in sorted(snapshots)),
            )
            atomic_write(self.output / "replay.json", dataset)
        elif (self.output / "replay.json").exists():
            (self.output / "replay.json").unlink()
        self.manifest = self.manifest.model_copy(
            update={
                "number_of_events": len(events),
                "number_of_quotes": quotes,
                "replayable_event_observations": len(snapshots),
            }
        )
        self.save()
