import logging

import pytest

from betting_scanner.config import ConfigurationError, Settings, load_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ODDS_API_KEY",
        "ODDS_API_BASE_URL",
        "LOG_LEVEL",
        "DEFAULT_REGION",
        "DEFAULT_ODDS_FORMAT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = load_settings()
    assert settings.odds_api_key is None
    assert settings.odds_api_base_url == "https://api.the-odds-api.com/v4"
    assert settings.log_level == "INFO"
    assert settings.default_region == "uk"
    assert settings.default_odds_format == "decimal"


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ODDS_API_KEY": "dummy-test-secret",
        "ODDS_API_BASE_URL": "https://example.org/v4",
        "LOG_LEVEL": "DEBUG",
        "DEFAULT_REGION": "eu",
        "DEFAULT_ODDS_FORMAT": "american",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = load_settings()
    assert settings.require_api_key() == values["ODDS_API_KEY"]
    assert settings.odds_api_base_url == values["ODDS_API_BASE_URL"]
    assert settings.log_level == "DEBUG"
    assert settings.default_region == "eu"
    assert settings.default_odds_format == "american"


@pytest.mark.parametrize("key", ["", "  "])
def test_missing_credentials_permitted_offline(key: str) -> None:
    settings = load_settings({"ODDS_API_KEY": key})
    assert settings.odds_api_key is None
    with pytest.raises(ConfigurationError, match="ODDS_API_KEY is required"):
        settings.require_api_key()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOG_LEVEL", "verbose"),
        ("LOG_LEVEL", ""),
        ("DEFAULT_REGION", "unknown"),
        ("DEFAULT_REGION", ""),
        ("DEFAULT_ODDS_FORMAT", "fractional"),
        ("ODDS_API_BASE_URL", "http://example.org/v4"),
        ("ODDS_API_BASE_URL", ""),
        ("ODDS_API_BASE_URL", "https://[broken"),
        ("ODDS_API_BASE_URL", "https://example.org:invalid"),
        ("ODDS_API_BASE_URL", "https://user:password@example.org/v4"),
        ("ODDS_API_BASE_URL", "https://example.org/v4?apiKey=secret"),
        ("ODDS_API_BASE_URL", "https://example.org/v4#secret"),
        ("ODDS_API_BASE_URL", "https://example.org/with space"),
    ],
)
def test_invalid_settings(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        load_settings({name: value})


def test_secret_hidden_from_representations_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "dummy-private-credential"
    settings = load_settings({"ODDS_API_KEY": secret})
    assert secret not in repr(settings)
    assert secret not in str(settings)
    with caplog.at_level(logging.INFO):
        logging.getLogger(__name__).info("Settings: %r", settings)
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "name", ["LOG_LEVEL", "DEFAULT_REGION", "DEFAULT_ODDS_FORMAT", "ODDS_API_BASE_URL"]
)
def test_errors_never_echo_input(name: str) -> None:
    secret = "dummy-private-credential"
    with pytest.raises(ConfigurationError) as error:
        load_settings({"ODDS_API_KEY": secret, name: secret})
    assert secret not in str(error.value)
    assert secret not in repr(error.value)


def test_direct_construction_validates() -> None:
    with pytest.raises(ConfigurationError, match="LOG_LEVEL"):
        Settings(log_level="invalid")


def test_explicit_empty_mapping_ignores_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "invalid")
    assert load_settings({}).log_level == "INFO"
