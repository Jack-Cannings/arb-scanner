"""Provider wire types and credential-free response metadata."""

import json
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from betting_scanner.core.models import DecimalOdds, DomainModel, Text, UTCTimestamp


def validate_sport_key(value: str) -> str:
    if (
        not value.startswith(("tennis_atp_", "tennis_wta_"))
        or not all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in value)
        or len(value) > 120
    ):
        raise ValueError("Select an ATP/WTA tennis sport key from discovery")
    return value


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ApiSport(WireModel):
    key: Text
    group: Text
    title: Text
    active: bool
    has_outrights: bool = False
    description: str = ""

    @property
    def is_tennis(self) -> bool:
        return (
            self.group.casefold() == "tennis"
            and self.key.startswith(("tennis_atp_", "tennis_wta_"))
            and not self.has_outrights
        )

    @property
    def is_active_singles(self) -> bool:
        wording = f"{self.key} {self.title} {self.description}".casefold()
        return (
            self.active
            and self.is_tennis
            and "singles" in wording
            and "doubles" not in wording
        )


class ApiMarket(WireModel):
    key: Text
    outcomes: tuple[dict[str, object], ...]
    last_update: UTCTimestamp | None = None


class ApiBookmaker(WireModel):
    key: Text
    title: Text
    last_update: UTCTimestamp | None = None
    markets: tuple[ApiMarket, ...]


class ApiEvent(WireModel):
    id: Text
    sport_key: Text
    sport_title: Text
    commence_time: UTCTimestamp
    home_team: Text
    away_team: Text
    bookmakers: tuple[ApiBookmaker, ...] = ()


class ApiOutcome(WireModel):
    name: Text
    price: DecimalOdds
    point: Decimal | None = None
    description: Text | None = None


class HistoricalPage(WireModel):
    timestamp: UTCTimestamp
    previous_timestamp: UTCTimestamp | None = None
    next_timestamp: UTCTimestamp | None = None
    data: tuple[dict[str, object], ...]

    @model_validator(mode="after")
    def ordered_neighbours(self) -> Self:
        if (
            self.previous_timestamp is not None
            and self.previous_timestamp >= self.timestamp
        ):
            raise ValueError("Invalid previous snapshot reference")
        if self.next_timestamp is not None and self.next_timestamp <= self.timestamp:
            raise ValueError("Invalid next snapshot reference")
        return self


class QuotaHeaders(DomainModel):
    remaining: int | None = None
    used: int | None = None
    last: int | None = None


class ResponseRecord(DomainModel):
    endpoint: str
    requested_at: UTCTimestamp | None
    collected_at: UTCTimestamp
    status_code: int
    headers: dict[str, str]
    quota: QuotaHeaders
    body: str


def parse_page(record: ResponseRecord) -> HistoricalPage:
    # JSON numbers must become Decimal before Pydantic sees them.
    value: object = json.loads(record.body, parse_float=Decimal)
    page = HistoricalPage.model_validate(value)
    if record.requested_at is None or page.timestamp > record.requested_at:
        raise ValueError("Provider snapshot must be at or before requested time")
    return page
