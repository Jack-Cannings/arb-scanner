"""Bounded, explicitly invoked v4 HTTP requests and shared quota accounting."""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

import httpx
from pydantic import Field, TypeAdapter, ValidationError

from betting_scanner.config import Settings
from betting_scanner.core.models import DomainModel, UTCTimestamp, normalise_utc
from betting_scanner.providers.odds_api.schema import (
    ApiSport,
    QuotaHeaders,
    ResponseRecord,
    validate_sport_key,
)


class OddsApiError(RuntimeError):
    def __init__(
        self, code: str, message: str, response: ResponseRecord | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.response = response


class CreditBudgetExceeded(OddsApiError):
    def __init__(self) -> None:
        super().__init__(
            "credit_budget", "No further request fits the API credit budget."
        )


class ClientOptions(DomainModel):
    timeout: Annotated[float, Field(gt=0, le=120)] = 20.0
    max_retries: Annotated[int, Field(ge=0, le=3, strict=True)] = 1


class ApiAttempt(DomainModel):
    endpoint: str
    requested_at: UTCTimestamp | None
    reserved_credits: Annotated[int, Field(ge=0, strict=True)]
    actual_credits: Annotated[int, Field(ge=0, strict=True)] | None = None
    status: Literal["in_flight", "received", "transport_error"] = "in_flight"
    http_status: int | None = None
    quota: QuotaHeaders | None = None


class BudgetState(DomainModel):
    max_credits: Annotated[int, Field(ge=0, strict=True)]
    attempts: tuple[ApiAttempt, ...] = ()

    @property
    def accounted_credits(self) -> int:
        return sum(
            a.actual_credits if a.actual_credits is not None else a.reserved_credits
            for a in self.attempts
        )

    @property
    def actual_credits(self) -> int | None:
        if any(
            a.actual_credits is None and a.reserved_credits > 0 for a in self.attempts
        ):
            return None
        return sum(a.actual_credits or 0 for a in self.attempts)


class CreditLedger:
    def __init__(
        self, state: BudgetState, persist: Callable[[BudgetState], None] | None = None
    ) -> None:
        if state.accounted_credits > state.max_credits:
            raise ValueError("The new budget is below credits already accounted for")
        self.state = state
        self.persist = persist

    def _save(self) -> None:
        if self.persist is not None:
            self.persist(self.state)

    def reserve(self, cost: int, endpoint: str, requested_at: datetime | None) -> int:
        if cost > self.available_credits:
            raise CreditBudgetExceeded()
        index = len(self.state.attempts)
        attempt = ApiAttempt(
            endpoint=endpoint, requested_at=requested_at, reserved_credits=cost
        )
        self.state = self.state.model_copy(
            update={"attempts": (*self.state.attempts, attempt)}
        )
        self._save()  # Durably reserve BEFORE network I/O.
        return index

    @property
    def available_credits(self) -> int:
        allowance = self.state.max_credits - self.state.accounted_credits
        since_known = 0
        for attempt in reversed(self.state.attempts):
            if attempt.quota is not None and attempt.quota.remaining is not None:
                return max(0, min(allowance, attempt.quota.remaining - since_known))
            since_known += (
                attempt.actual_credits
                if attempt.actual_credits is not None
                else attempt.reserved_credits
            )
        return max(0, allowance)

    def finish(self, index: int, record: ResponseRecord | None) -> None:
        attempt = self.state.attempts[index]
        update: dict[str, object] = {
            "status": "transport_error" if record is None else "received"
        }
        if record is not None:
            update.update(
                actual_credits=record.quota.last
                if record.quota.last is not None
                else 0
                if attempt.reserved_credits == 0
                else None,
                http_status=record.status_code,
                quota=record.quota,
            )
        attempts = list(self.state.attempts)
        attempts[index] = attempt.model_copy(update=update)
        self.state = self.state.model_copy(update={"attempts": tuple(attempts)})
        self._save()


def _quota(headers: httpx.Headers) -> QuotaHeaders:
    def number(key: str) -> int | None:
        value = headers.get(key, "")
        return int(value) if value.isascii() and value.isdigit() else None

    return QuotaHeaders(
        remaining=number("x-requests-remaining"),
        used=number("x-requests-used"),
        last=number("x-requests-last"),
    )


class OddsApiClient:
    def __init__(
        self,
        settings: Settings,
        ledger: CreditLedger,
        *,
        options: ClientOptions | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self.ledger = ledger
        self.options = ClientOptions.model_validate(
            (options or ClientOptions()).model_dump()
        )
        self.clock = clock
        self.sleep = sleep
        self._http = httpx.Client(
            timeout=self.options.timeout, transport=transport, follow_redirects=False
        )

    def __enter__(self) -> Self:
        try:
            self._settings.require_api_key()
        except ValueError:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(
        self,
        endpoint: str,
        *,
        cost: int,
        requested_at: datetime | None = None,
        params: dict[str, str] | None = None,
    ) -> ResponseRecord:
        key = self._settings.require_api_key()
        # Credentials use the documented header, never URL parameters or logs.
        query = dict(params or {})
        if requested_at is not None:
            requested_at = normalise_utc(requested_at)
            query["date"] = requested_at.isoformat().replace("+00:00", "Z")
        last_record: ResponseRecord | None = None
        for retry in range(self.options.max_retries + 1):
            try:
                index = self.ledger.reserve(cost, endpoint, requested_at)
            except CreditBudgetExceeded as error:
                error.response = last_record
                raise
            try:
                response = self._http.get(
                    self._settings.odds_api_base_url.rstrip("/") + "/" + endpoint,
                    params=query,
                    headers={"x-api-key": key},
                )
            except httpx.TransportError:
                self.ledger.finish(index, None)
                # An ambiguous timeout may have consumed credits; do not repeat it.
                raise OddsApiError(
                    "transport",
                    "Historical request failed; credits may have been consumed.",
                ) from None
            safe_headers = {
                name: value.replace(key, "[REDACTED]")
                for name, value in response.headers.items()
                if name
                in {
                    "x-requests-used",
                    "x-requests-remaining",
                    "x-requests-last",
                    "retry-after",
                    "content-type",
                    "date",
                }
            }
            last_record = ResponseRecord(
                endpoint=endpoint,
                requested_at=requested_at,
                collected_at=normalise_utc(self.clock()),
                status_code=response.status_code,
                headers=safe_headers,
                quota=_quota(response.headers),
                body=response.text.replace(key, "[REDACTED]"),
            )
            self.ledger.finish(index, last_record)
            if self.ledger.state.accounted_credits > self.ledger.state.max_credits:
                raise OddsApiError(
                    "unexpected_cost",
                    "Reported API cost exceeded its documented bound; "
                    "download stopped.",
                    last_record,
                )
            if (
                response.status_code in {429, 500, 502, 503, 504}
                and retry < self.options.max_retries
            ):
                retry_after = response.headers.get("retry-after", "")
                delay = (
                    min(int(retry_after), 30)
                    if retry_after.isascii() and retry_after.isdigit()
                    else min(2**retry, 8)
                )
                self.sleep(float(delay))
                continue
            try:
                error_body: object = json.loads(last_record.body)
            except ValueError:
                error_body = None
            error_code = (
                error_body.get("error_code") if isinstance(error_body, dict) else None
            )
            if error_code == "HISTORICAL_UNAVAILABLE_ON_FREE_USAGE_PLAN":
                raise OddsApiError(
                    "historical_access",
                    "Historical odds require suitable paid-plan access.",
                    last_record,
                )
            if error_code == "OUT_OF_USAGE_CREDITS":
                raise OddsApiError(
                    "quota_exhausted",
                    "API account usage credits are exhausted.",
                    last_record,
                )
            if error_code in {
                "UNKNOWN_SPORT",
                "INVALID_SPORT",
                "HISTORICAL_MARKETS_UNAVAILABLE_AT_DATE",
            }:
                raise OddsApiError(
                    "unavailable",
                    "Competition or dated historical market is unavailable.",
                    last_record,
                )
            if response.status_code == 401:
                raise OddsApiError(
                    "authentication", "The Odds API authentication failed.", last_record
                )
            if response.status_code == 403:
                raise OddsApiError(
                    "historical_access"
                    if endpoint.startswith("historical/")
                    else "access_denied",
                    "The Odds API denied access to this endpoint.",
                    last_record,
                )
            if response.status_code == 404:
                raise OddsApiError(
                    "unavailable",
                    "Competition or historical coverage unavailable.",
                    last_record,
                )
            if response.status_code == 429:
                raise OddsApiError(
                    "rate_limited",
                    "API rate limit reached; download stopped.",
                    last_record,
                )
            if response.status_code >= 400 or response.is_redirect:
                raise OddsApiError(
                    "http_error", "Historical API request failed.", last_record
                )
            return last_record
        raise OddsApiError("http_error", "Historical API request failed.", last_record)

    def sports(self) -> tuple[ApiSport, ...]:
        record = self.get("sports", cost=0, params={"all": "true"})
        try:
            return TypeAdapter(tuple[ApiSport, ...]).validate_python(
                json.loads(record.body)
            )
        except (ValueError, ValidationError):
            raise OddsApiError(
                "malformed", "Malformed sport listing.", record
            ) from None

    def events(self, sport_key: str, requested_at: datetime) -> ResponseRecord:
        return self.get(
            f"historical/sports/{validate_sport_key(sport_key)}/events",
            cost=1,
            requested_at=requested_at,
        )

    def odds(
        self,
        sport_key: str,
        requested_at: datetime,
        region: str = "uk",
        market: str = "h2h",
    ) -> ResponseRecord:
        if region not in {"uk", "us", "us2", "eu", "au"} or market != "h2h":
            raise ValueError(
                "Historical tennis downloads support one region and h2h only"
            )
        return self.get(
            f"historical/sports/{validate_sport_key(sport_key)}/odds",
            cost=10,
            requested_at=requested_at,
            params={
                "regions": region,
                "markets": market,
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )

    def live_odds(self, sport_key: str) -> ResponseRecord:
        """One explicitly selected UK decimal h2h request; filter starts locally."""
        return self.get(
            f"sports/{validate_sport_key(sport_key)}/odds",
            cost=1,
            params={
                "regions": "uk",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
        )


# Preserve the historical public name for existing callers and saved workflows.
HistoricalOddsClient = OddsApiClient
