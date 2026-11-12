"""Read a downloaded historical dataset without creating an HTTP client."""

import json
from dataclasses import dataclass
from pathlib import Path

from betting_scanner.backtesting.models import HistoricalDataset
from betting_scanner.providers.odds_api.download import DatasetManifest


@dataclass(frozen=True)
class SavedHistoricalReader:
    directory: Path

    def read(self) -> HistoricalDataset:
        metadata: object = json.loads(
            (self.directory / "manifest.json").read_text(encoding="utf-8")
        )
        if isinstance(metadata, dict) and metadata.get("mode") == "live":
            from betting_scanner.live.models import LiveManifest

            live = LiveManifest.model_validate(metadata)
            if live.replayable_event_observations == 0:
                raise ValueError(
                    "This archive has no valid pre-match observations to replay"
                )
            return HistoricalDataset.model_validate_json(
                (self.directory / "replay.json").read_text(encoding="utf-8")
            )
        manifest = DatasetManifest.model_validate_json(
            (self.directory / "manifest.json").read_text(encoding="utf-8")
        )
        if manifest.number_of_snapshots == 0:
            raise ValueError(
                "This dataset has no valid historical event snapshots to replay"
            )
        return HistoricalDataset.model_validate_json(
            (self.directory / "replay.json").read_text(encoding="utf-8")
        )
