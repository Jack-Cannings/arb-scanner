# Sports Betting Scanner

A modular Python project for analysing pricing inefficiencies across bookmakers.
The first planned application is a pre-match ATP/WTA tennis match-winner
arbitrage scanner using UK bookmaker odds from The Odds API. Opportunities will
be presented for manual investigation.

## Current implementation

Only project foundations are implemented: an installable `src/` package, typed
environment configuration, offline tests, linting, formatting, strict type
checking and Linux/Windows CI. The application **cannot scan live odds yet**.
There are no API requests, odds models, betting calculations, storage or dashboard.
There is no bookmaker account integration, bet execution, continuous polling or
real-money transaction support.

## Setup

Use Python 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
CI pins uv 0.11.7; using that version locally keeps tooling consistent.

```sh
uv python install 3.12
uv sync --locked
uv run python -c "import betting_scanner"
```

uv creates `.venv` and installs the package in editable mode. `uv.lock` pins the
development dependency resolution. The build backend is pinned Hatchling;
there are no runtime dependencies. Development tools are pytest, Ruff and mypy.

## Configuration

Settings read process environment variables when `load_settings()` is called.
Importing the package does not load configuration or perform network I/O.
`.env.example` is a reference containing no credentials; `.env` files are ignored
by Git and are **not loaded automatically**. Export variables in your shell.
For example, in PowerShell: `$env:LOG_LEVEL = "DEBUG"`; in POSIX shells:
`export LOG_LEVEL=DEBUG`.

| Variable | Default | Accepted values |
| --- | --- | --- |
| `ODDS_API_KEY` | absent | Optional credential; blank means absent |
| `ODDS_API_BASE_URL` | `https://api.the-odds-api.com/v4` | HTTPS URL without embedded credentials, query or fragment |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, CRITICAL, NOTSET |
| `DEFAULT_REGION` | `uk` | uk, us, us2, eu, au (one region) |
| `DEFAULT_ODDS_FORMAT` | `decimal` | decimal, american |

The default endpoint follows the [official v4 documentation](https://the-odds-api.com/liveapi/guides/v4/).
Values are case-sensitive. Invalid settings raise `ConfigurationError` naming
the setting without echoing its value.

```python
from betting_scanner.config import load_settings

settings = load_settings()  # Works offline without a key.
# Future live provider entry points must call settings.require_api_key().
```

Settings are immutable and exclude all field values from string/repr output.
The configuration code emits no logs. Never log raw environment variables,
serialize settings, or log the result of `require_api_key()`; it intentionally
returns the credential for future authenticated requests. Future providers must
also redact authentication headers and request URLs from logs and exceptions.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Use `uv run ruff format .` to format edits. Tests isolate environment changes
and require neither network access nor credentials. CI runs the same checks on
Python 3.12 on Linux and Windows, with SHA-pinned actions and the committed lockfile.

## Package architecture

```text
src/betting_scanner/
  config.py              # Implemented typed settings
  core/                  # Future reusable domain concepts
  providers/             # Future retrieval and normalisation
  strategies/arbitrage/  # Future opportunity calculations
  storage/               # Future persistence
  interfaces/            # Future commands, dashboards and alerts
tests/test_config.py     # Offline configuration tests
```

All domain subpackages are placeholders. Dependency boundaries for later work:

- **Core** is independent of providers and contains no tennis-specific assumptions.
- **Providers** adapt external data into shared domain concepts. Strategies must
  remain unchanged when an odds provider is replaced.
- **Strategies** consume normalised domain data, never provider-specific JSON.
  They must not depend on storage or interfaces.
- **Storage** persists snapshots and opportunities (SQLite is planned first).
  Persistence must not determine betting calculations.
- **Interfaces** orchestrate components and present results without duplicating
  calculation logic. Core must not import these outer layers.

No abstract base classes, plugin registries or service containers are needed yet.

## Planned scope and limitations

Subsequent tasks will add odds retrieval, cross-bookmaker comparison, arbitrage
detection, stake and theoretical profit calculations, historical observations
and a dashboard. None of these features exists today.

Longer-term possibilities include football, basketball, horse racing,
bookmaker/exchange arbitrage, statistical value betting, non-transitivity,
cross-market probability inconsistencies and other providers such as OddsRelay.
These are architectural considerations only, with no implementation in this task.
