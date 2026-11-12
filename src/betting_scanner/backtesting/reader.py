"""Minimal reader contract for future historical data adapters."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from betting_scanner.backtesting.models import HistoricalDataset


class HistoricalReader(Protocol):
    def read(self) -> HistoricalDataset:
        """Return validated records with observation times and explicit evidence."""
        ...


@dataclass(frozen=True)
class JsonHistoricalReader:
    path: Path

    def read(self) -> HistoricalDataset:
        return HistoricalDataset.model_validate_json(
            self.path.read_text(encoding="utf-8")
        )
