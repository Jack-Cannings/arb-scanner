"""Validated settings with no network access or implicit file loading."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """A setting is invalid or a required live credential is absent."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Application settings. Never serialize settings or log raw credentials.

    All values are excluded from repr to prevent accidental credential exposure,
    even when a credential is mistakenly supplied in another environment field.
    """

    odds_api_key: str | None = field(default=None, repr=False)
    odds_api_base_url: str = field(
        default="https://api.the-odds-api.com/v4", repr=False
    )
    log_level: str = field(default="INFO", repr=False)
    default_region: str = field(default="uk", repr=False)
    default_odds_format: str = field(default="decimal", repr=False)

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.odds_api_base_url)
            valid_url = (
                url.scheme == "https"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and url.port != 0
                and not any(c.isspace() for c in self.odds_api_base_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigurationError(
                "ODDS_API_BASE_URL must be an HTTPS URL without credentials, "
                "query parameters or fragments."
            ) from None
        if self.log_level not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
            "NOTSET",
        }:
            raise ConfigurationError(
                "LOG_LEVEL must be a standard uppercase log level."
            )
        if self.default_region not in {"uk", "us", "us2", "eu", "au"}:
            raise ConfigurationError("DEFAULT_REGION must be uk, us, us2, eu or au.")
        if self.default_odds_format not in {"decimal", "american"}:
            raise ConfigurationError("DEFAULT_ODDS_FORMAT must be decimal or american.")

    def require_api_key(self) -> str:
        """Guard future live integration; offline loading never requires a key."""
        if self.odds_api_key is None or not self.odds_api_key.strip():
            raise ConfigurationError("ODDS_API_KEY is required for live integration.")
        return self.odds_api_key


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read an explicit mapping or the current process environment, without I/O."""
    env = os.environ if environ is None else environ
    key = env.get("ODDS_API_KEY", "").strip() or None
    return Settings(
        odds_api_key=key,
        odds_api_base_url=env.get(
            "ODDS_API_BASE_URL", "https://api.the-odds-api.com/v4"
        ),
        log_level=env.get("LOG_LEVEL", "INFO"),
        default_region=env.get("DEFAULT_REGION", "uk"),
        default_odds_format=env.get("DEFAULT_ODDS_FORMAT", "decimal"),
    )
