# Sports Betting Scanner

A modular Python project for analysing pricing inefficiencies across bookmakers.
The first planned application is a pre-match ATP/WTA tennis match-winner
arbitrage scanner using UK bookmaker odds from The Odds API. Opportunities will
be presented for manual investigation.

## Current implementation

Project foundations and provider-independent core data models are implemented:
an installable `src/` package, typed environment configuration, validated odds
records, synthetic JSON fixtures, offline tests, linting, formatting, strict type
checking and Linux/Windows CI. The application **cannot scan live odds yet**.
There are no API requests, betting calculations, storage or dashboard.
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
dependency resolution. The build backend is pinned Hatchling. Pydantic v2 is the
only direct runtime dependency, for validation and JSON serialisation of domain
models. Development tools are pytest, Ruff and mypy.

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
  core/models.py         # Validated provider-independent domain records
  providers/             # Future retrieval and normalisation
  strategies/arbitrage/  # Future opportunity calculations
  storage/               # Future persistence
  interfaces/            # Future commands, dashboards and alerts
tests/test_config.py     # Offline configuration tests
tests/test_models.py     # Domain validation and fixture tests
tests/fixtures/          # Eight deterministic synthetic JSON scenarios
```

Providers, strategies, storage and interfaces remain placeholders. Dependency boundaries:

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

## Core data models

`betting_scanner.core.models` contains Pydantic v2 models:

- `Sport`, `Competition`, `Participant` and `Event` use stable internal identifiers
  and display names. Events support any nonempty set of unique participants,
  explicit status and optional provider references.
- `Bookmaker` retains its internal ID and optional region/provider references.
- `Market` describes its outcome space, type and expected outcome IDs; `Outcome`
  uses a stable ID independent of labels or ordering, with an optional participant
  reference. Match winner, three-way result and future market types are represented;
  market-specific calculations and parameter schemas remain future work.
- `OddsQuote` links provider/event/bookmaker/market/outcome IDs, exact decimal odds,
  timestamps, settlement evidence and optional JSON metadata.
- `OddsSnapshot` groups one event's records and checks cross-reference integrity.
  Incomplete quote coverage and differing rules are valid observations, not errors.

Models reject extra fields and invalid/blank identifiers. Internal IDs use 1-128
ASCII letters, digits, underscores, periods, colons or hyphens, starting with a
letter or digit. Provider references are opaque nonblank text, not internal IDs.
Model fields cannot be reassigned; nested metadata/reference dictionaries should
be treated as read-only. Use validated construction for changes rather than
Pydantic's validation-bypassing `model_construct` or unchecked `model_copy` updates.

Providers will translate their own JSON into these records. Strategies will consume
the same normalised records regardless of provider, so changing a provider does
not change the calculations. No provider parser or strategy is implemented yet.

### Outcome definition and quote coverage

`expected_outcome_ids` is explicit: `None` means unknown, while a nonempty unique
tuple specifies the expected outcome set. `Market.definition_status` reports
`complete`, `missing_expected_outcomes` or `unknown` by comparing the definitions
in `outcomes` with that set. Two outcome definitions do not prove a complete market
unless the expected set is known. No market type imposes a two-outcome limit.

Definition completeness is separate from quote coverage at each bookmaker. Fixture
C defines both outcomes but has only one quote at one bookmaker. Snapshot validation
allows this partial observation. Eligibility decisions belong to Prompt 3.

### Settlement evidence

Each quote has a `SettlementRule`: match completion required, minimum one set
completed, match started/first ball served, another documented rule, or unknown.
It includes a verification flag, exact descriptive text and documentation/evidence.
Missing rules default to unknown/unverified. Verification requires a known category
and documentation; other documented rules require details even when unverified.

Categories summarise only part of settlement behaviour. No rules are inferred from
bookmaker names. Matching categories alone do not prove compatibility; unknown or
unverified rules must never be classified as compatible. Rule evidence is retained
for future strategy checks; no compatibility engine is implemented here.

### Precision, timestamps and JSON

Odds use `Decimal` and must be finite and greater than 1. Supply a Decimal, numeric
string or integer. Binary floats are rejected to avoid losing precision before
validation. JSON prices are strings, preserving decimal digits and trailing zeros.
For metadata, encode precision-sensitive numbers as strings as well.

Event start, optional provider update and required collection time are distinct.
Datetime objects must be timezone-aware; ISO 8601 strings must include an offset.
Numeric epoch inputs and naive datetimes are rejected. Accepted timestamps are
normalised to UTC and serialised with `Z`. Missing provider timestamps remain
`None`; recent collection does not imply a fresh provider update. There are no
freshness thresholds yet.

```python
from pathlib import Path
from betting_scanner.core.models import OddsSnapshot

payload = Path("tests/fixtures/a_atp_candidate.json").read_text(encoding="utf-8")
snapshot = OddsSnapshot.model_validate_json(payload)
restored = OddsSnapshot.model_validate_json(snapshot.model_dump_json())
assert restored == snapshot
```

Use typed constructors or `model_validate()` for Python data, `model_dump_json()`
for JSON, and `model_validate_json()` to reconstruct validated records. These retain
stable IDs, provider references, UTC timestamps, Decimal precision and metadata.

Run the fixture/model tests with `uv run pytest tests/test_models.py`. The eight
fixtures cover ATP candidate pricing, WTA without arbitrage, missing quotes,
invalid odds, stale updates, differing rules, unknown rules and football with
three outcomes. See [fixture descriptions](tests/fixtures/README.md). All data and
rule evidence are fictional, fixed in time and loaded without network access.

## Planned scope and limitations

Subsequent tasks will add odds retrieval, cross-bookmaker comparison, arbitrage
detection, stake and theoretical profit calculations, historical observations
and a dashboard. None of these features exists today.

Longer-term possibilities include football, basketball, horse racing,
bookmaker/exchange arbitrage, statistical value betting, non-transitivity,
cross-market probability inconsistencies and other providers such as OddsRelay.
These are architectural considerations only, with no implementation in this task.
