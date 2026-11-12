# Sports Betting Scanner

A modular Python project for analysing pricing inefficiencies across bookmakers.
The initial application is a pre-match ATP/WTA tennis match-winner
arbitrage scanner using UK bookmaker odds from The Odds API. Opportunities will
be presented for manual investigation.

## Current implementation

Project foundations, provider-independent core data models, an offline
cross-bookmaker arbitrage engine, historical paper replay, one-shot live
tennis collection and bounded local monitoring are implemented:
an installable `src/` package, typed environment configuration, validated odds
records, synthetic JSON fixtures, offline tests, linting, formatting, strict type
checking and Linux/Windows CI. Live UK h2h scans require explicit execution.
The engine compares normalised prices and calculates hypothetical stakes and
profits. Historical replay adds a virtual bankroll and JSON/CSV reports.
Explicit current-odds and historical API commands are available; no database
or dashboard is implemented. Backtesting cached datasets remains fully offline.
There is no bookmaker account integration, real bet execution, unlimited polling or
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
dependency resolution. The build backend is pinned Hatchling. Pydantic v2 is a
runtime dependency for validation and JSON serialisation of domain models. HTTPX
handles explicitly invoked provider requests. Development tools are pytest,
Ruff and mypy.

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
  providers/odds_api/    # Shared HTTP client and live/historical normalisation
  strategies/arbitrage/  # Selection, Decimal mathematics and typed results
  backtesting/          # Historical readers, replay, execution, settlement, reports
  live/                 # One-shot collection, archive and terminal presentation
  storage/files.py      # Atomic local files and cross-platform writer lock
  interfaces/backtest.py # Historical replay command line
  interfaces/live.py     # Discovery and explicit single live scans
tests/test_config.py     # Offline configuration tests
tests/test_models.py     # Domain validation and fixture tests
tests/test_arbitrage.py  # Selection, verification, mathematics and penny allocation
tests/fixtures/          # Eight deterministic synthetic JSON scenarios
```

Local file storage is implemented; database persistence remains future work. Boundaries:

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
not change the calculations. The Odds API historical adapter implements this
translation for historical and current odds without duplicating strategy calculations.

### Outcome definition and quote coverage

`expected_outcome_ids` is explicit: `None` means unknown, while a nonempty unique
tuple specifies the expected outcome set. `Market.definition_status` reports
`complete`, `missing_expected_outcomes` or `unknown` by comparing the definitions
in `outcomes` with that set. Two outcome definitions do not prove a complete market
unless the expected set is known. No market type imposes a two-outcome limit.

Definition completeness is separate from quote coverage at each bookmaker. Fixture
C defines both outcomes but has only one quote at one bookmaker. Snapshot validation
allows this partial observation. The engine requires one selected quote per
expected outcome across the combined books; individual books need not quote every
outcome. If any outcome has no usable quote anywhere, detection is rejected.

Markets also carry explicit `mutually_exclusive` and `collectively_exhaustive`
verification flags (both default false), a `period` (default unknown), and a
`parameters` mapping. A canonical market ID must identify the same event, type,
period, parameters and outcome definitions for every quote referencing it.
Normalisers/reviewers must establish that identity, never infer it from labels.
This engine handles unparameterised match-winner and three-way-result markets;
other types/parameters remain non-actionable. Distinct market IDs are never combined.

### Settlement evidence

Each quote has a `SettlementRule`: match completion required, minimum one set
completed, match started/first ball served, another documented rule, or unknown.
It includes a verification flag, exact descriptive text and documentation/evidence.
Missing rules default to unknown/unverified. Verification requires a known category
and documentation; other documented rules require details even when unverified.

Categories summarise only part of settlement behaviour. No rules are inferred from
bookmaker names. Matching categories alone do not prove compatibility; unknown or
unverified rules must never be classified as compatible. The engine conservatively
groups quotes only when the entire verified rule record matches: category,
description and documentation. Different contracts are evaluated separately;
similar rules expressed differently are not automatically reconciled. Producers
are responsible for truthful verification and complete exact rule descriptions.

### Precision, timestamps and JSON

Odds use `Decimal` and must be finite and greater than 1. Supply a Decimal, numeric
string or integer. Binary floats are rejected to avoid losing precision before
validation. JSON prices are strings, preserving decimal digits and trailing zeros.
For metadata, encode precision-sensitive numbers as strings as well.

Event start, optional provider update and required collection time are distinct.
Datetime objects must be timezone-aware; ISO 8601 strings must include an offset.
Numeric epoch inputs and naive datetimes are rejected. Accepted timestamps are
normalised to UTC and serialised with `Z`. Historical quotes additionally retain
`observed_at`, the archive availability time, separately from `collected_at`,
the local download time. Replay uses archive evidence when present; legacy/local
observations continue to use collection time. Missing provider timestamps remain
`None`; recent collection does not imply a fresh provider update. Engine freshness
thresholds are described below; the core model itself does not classify freshness.

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

## Offline arbitrage engine

For mutually exclusive, collectively exhaustive outcomes with decimal odds `O_i`,
the reciprocal sum is `S = sum(1 / O_i)`. A strictly positive mathematical arbitrage
requires `S < 1`. Break-even is not arbitrage. For budget `T`:

```text
ideal stake_i      = T * (1 / O_i) / S
theoretical payout = T / S
theoretical profit = T / S - T
theoretical ROI %  = (1 / S - 1) * 100
```

All monetary calculations use Decimal in a private, input-sized precision context.
Exact finite cross-products determine whether `S` is below one so recurring
decimals do not misclassify exact break-even (including 3.00 / 3.00 / 3.00).
Reported recurring values are finite Decimal approximations, with at least 60
significant digits of working precision, independent of the caller's context.

### Selection and verification

`detect_arbitrage(snapshot, market_id, total_stake=..., evaluated_at=..., config=...)`
revalidates normalised records and evaluates only the requested canonical market.
It selects the highest price per outcome; equal prices prefer the lexicographically
smallest bookmaker ID, then provider ID, then canonical quote JSON. It permits
both selected outcomes to come from the same bookmaker. Within each compatible
verified rule group it calculates a separate combination, retaining the one with
the highest rounded worst-case profit, then theoretical profit. Remaining ties
use stable rule-record ordering.

`ArbitrageResult` separates:

- `candidate`: the mathematical calculation from the best non-stale/non-future
  quotes, potentially with unverified settlement rules or missing update times.
- `eligible`: the best fully verified combination. Requires a scheduled event
  starting after evaluation, a complete explicitly verified outcome space and
  period, supported market scope, current provider timestamps and one verified
  compatible settlement contract covering all outcomes.
- `verification_status`, `market_eligible` and `actionability_status`: verification
  is separate from profitability. A verified break-even market is not arbitrage.
  Status `actionable` requires positive mathematical and rounded worst-case profit
  with nonzero stakes on every outcome. `mathematical_candidate` retains positive
  mathematics that fails verification or penny profitability.

Each calculation retains selected quotes (including provider traceability and
timestamps), ideal/rounded stakes, payouts and theoretical/rounded profit and ROI.
Structured reason codes identify rejected input, missing outcomes, stale/future
quotes, missing verification and rounding losses. Reasons may describe excluded
quotes even when a different safe selection exists. Unknown/incomplete outcome
definitions cannot support a calculation covering all possible outcomes and are
rejected. Invalid caller budgets, evaluation times and configuration raise errors;
invalid normalised records produce a structured rejection result.

`EngineConfig` defaults to a five-minute provider update age and five-second clock
skew tolerance. Both are configurable timedeltas. The supplied evaluation time
must be timezone-aware and is normalised to UTC; no calculation reads the system
clock. Age exactly at the threshold is allowed. Updates and collection times
beyond the future skew limit are excluded. Missing update times are exploratory
only: collection time is never substituted for bookmaker update time.

### Penny stakes and worked example

The budget must be a finite positive Decimal GBP amount in whole pennies.
Initial ideal stakes follow the formula above. To optimise the rounded result,
the allocator searches for the highest affordable whole-penny payout. A target
of `p` payout pennies needs `ceil(p / O_i)` stake pennies per outcome. Monotone
binary search finds the maximum feasible target; leftover pennies go to the
largest ideal-stake shortfall, with outcome-ID order breaking ties. This spends
the entire budget and maximises the worst-case payout in pennies, including for
small budgets where simply rounding ideal stakes is insufficient.

Raw payouts are retained. Payouts are also rounded **down** to pennies as a
conservative modelling convention; worst-case profit and ROI use those penny
payouts. A tiny theoretical margin can disappear, leaving a candidate rather
than a profitable rounded combination.

For 2.20 / 2.40 odds and a £100 budget:

| Quantity | Value |
| --- | --- |
| Reciprocal sum | 1/2.20 + 1/2.40 = 115/132 ≈ 0.871212 |
| Theoretical payout | £114.782608… |
| Theoretical profit / ROI | £14.782608… / 14.782608…% |
| Ideal stakes | £52.173913… / £47.826086… |
| Allocated stakes | £52.17 / £47.83 (sum £100.00) |
| Raw payouts | £114.774 / £114.792 |
| Conservative penny payouts | £114.77 / £114.79 |
| Rounded worst-case profit / ROI | £14.77 / 14.77% |

These are hypothetical returns under supplied prices and verified modelled
conditions, not actual realised profit or a guarantee of bookmaker acceptance.
Minimum/maximum stakes, available liquidity, price changes and bookmaker-specific
payout conventions are not implemented.

Run the synthetic engine tests with `uv run pytest tests/test_arbitrage.py`, or
all tests with `uv run pytest tests/`. The tests include two- and three-outcome
arbitrage, no-arbitrage/break-even, unknown and differing settlement contracts,
freshness boundaries, deterministic ties, recurring decimals, and exhaustive
checks of the optimal penny allocation for small budgets.

```python
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from betting_scanner.core.models import OddsSnapshot
from betting_scanner.strategies.arbitrage import detect_arbitrage

snapshot = OddsSnapshot.model_validate_json(
    Path("tests/fixtures/a_atp_candidate.json").read_text(encoding="utf-8")
)
result = detect_arbitrage(
    snapshot,
    snapshot.markets[0].id,
    total_stake=Decimal("100.00"),
    evaluated_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
)
print(result.model_dump_json(indent=2))
```

`calculate_arbitrage(selected_quotes, total_stake)` is the pure low-level
calculator. It validates unique outcome IDs and a common event/market but makes
no completeness, settlement or freshness claims; use the detection pipeline for
eligibility classification. Results round-trip with `model_dump_json()` and
`ArbitrageResult.model_validate_json()`.

## Historical replay and paper trading

Run a fictional history from the repository root:

```sh
uv sync --locked
uv run betting-backtest tests/fixtures/histories/a_success.json --bankroll 1000 --stake 100 --min-roi 0 --output-dir backtest-output/success
```

Equivalent module command: `uv run python -m betting_scanner.interfaces.backtest
tests/fixtures/histories/a_success.json` (enter on one line). No credentials or
network access are needed. The terminal summary and generated files distinguish
detected candidates, threshold-qualified opportunities, attempted/full/partial/
failed executions, settled positions and unsettled exposure.

Defaults are £1,000 starting cash, £100 per opportunity, £100 maximum exposure per
event, £500 aggregate exposure, zero execution delay, zero minimum ROI/profit,
and a 0.5% adverse haircut to decimal prices. ROI thresholds use **rounded
worst-case ROI**, and a strictly positive rounded profit is always required.
The baseline assumes all submitted legs are accepted; this is a simulation
assumption, not evidence of acceptance. Unknown data and observation gaps fail
closed, and prices/payouts are conservatively adjusted. Stress the baseline with:

```sh
uv run betting-backtest tests/fixtures/histories/f_price_change.json --execution-delay 10 --output-dir backtest-output/delayed
uv run betting-backtest tests/fixtures/histories/g_bankroll.json --bankroll 100 --output-dir backtest-output/limited
uv run betting-backtest tests/fixtures/histories/a_success.json --reject-last --slippage 0.01 --min-profit 1 --max-event-exposure 100 --max-aggregate-exposure 200 --output-dir backtest-output/stress
```

All flags accept GBP amounts or percentages as decimal strings; `--slippage` is a
fraction (0.005 = 0.5%), and `--execution-delay` is integer seconds. Available
per-bet bookmaker minimum/maximum stakes can be supplied through the Python API:

```python
from decimal import Decimal
from pathlib import Path
from betting_scanner.backtesting.models import ReplayConfig, StakeLimits
from betting_scanner.backtesting.reader import JsonHistoricalReader
from betting_scanner.backtesting.replay import run_replay

dataset = JsonHistoricalReader(Path("tests/fixtures/histories/a_success.json")).read()
report = run_replay(
    dataset,
    ReplayConfig(bookmaker_limits={"book-1": StakeLimits(maximum=Decimal("10.00"))}),
)
```

### Chronology and execution assumptions

The reader returns `HistoricalDataset`: full event snapshots with explicit
`observed_at` timestamps, separately observed per-bet settlement evidence, and
optional synthetic execution instructions. Replay sorts records chronologically
and uses an injected `ReplayClock`, never the wall clock. Quotes whose archive
observation (or collection time for local recordings) is after a replay step are
recorded as unavailable and excluded. At each step the
existing arbitrage engine evaluates the known snapshot with the configured
provider-update freshness limit. Missing update times remain unverified.

Each snapshot **replaces** the event's state; absent quotes are never carried
forward. Delayed orders require a new full observation at the exact due time;
otherwise they fail with `no_execution_observation`. A later observation is not
used retroactively. For example, the brief fixture loses its opportunity after
one second; with a ten-second delay there is no execution confirmation. The
changing-price fixture has an observation at ten seconds, so the revised prices
are checked then and the attempt fails preflight.

Execution confirms the originally selected bookmakers/outcomes and settlement
contracts, reapplies the existing engine to those prices after the haircut, and
checks the fixed decision-time stakes at the new prices. It does not silently
replace submitted stakes with a more favourable allocation. Cash, event and total
exposure are checked before any fill. Successful confirmations may still have
individual rejections through instructions, `reject_last`, or supplied stake
limits. Partial fills deduct only accepted stakes and retain explicit one-sided
exposure; they have no guaranteed arbitrage payout. Failed attempts deduct nothing.
Pending orders reserve no cash, so simultaneous orders compete deterministically
for available funds at execution.

Unchanged combinations of canonical market definition, selected bookmaker/provider,
outcome, price and rule records are counted/entered once per replay, regardless of
refreshed quote timestamps. This deliberately suppresses retries of the same
combination after failed execution or later reappearance. Price/rule changes may
create a new opportunity, still subject to exposure limits.

### Settlement, accounting and metrics

Historical results must explicitly state won/lost/void, observation time, source
(`synthetic` or `historical`) and the applicable verified settlement rule for each
bookmaker/outcome. No outcomes or retirement adjudications are invented. Rules
must match the accepted quote's full verified contract, and evidence must be
observed after the event start known at execution. Missing or incompatible
evidence leaves positions unsettled.

Conservatively, all accepted legs must have valid observed results before any
position payout or realised P&L is credited. Wins pay accepted price × stake,
rounded down to pennies; losses pay zero; voids refund the accepted stake. Rejected
legs are `not_placed`. Incomplete positions stay at cost, with zero unrealised P&L:
`equity_at_cost = cash + open accepted stakes`. This is an accounting convention,
not a liquidation value or guarantee of recovering one-sided stakes.

Reports include snapshot/event counts, unique mathematical candidates and eligible
opportunities, execution outcomes, settled/unsettled counts, theoretical and
realised ROI, accepted stakes, realised P&L, exposure, ending cash/equity, drawdown
and largest settled loss. Average theoretical ROI is the arithmetic mean over
unique threshold-qualified opportunities; average realised ROI is the arithmetic
mean of settled-position P&L / accepted stake (including partial fills). Empty
averages are `null`/`n/a`. Drawdown is the largest peak-to-trough decrease in
chronological equity at cost; it does not mark open bets to market. Returns are
not annualised and no Sharpe ratio is reported.

The success example above produces:

```text
Snapshots / events: 1 / 1
Candidates / eligible: 1 / 1
Attempts / full / partial / failed: 1 / 1 / 0 / 0
Accepted stakes: GBP 100.00
Realised hypothetical P&L: GBP 14.20
Ending cash / equity at cost: GBP 1014.20 / GBP 1014.20
```

The 0.5% price haircut explains the difference from the £14.77 no-slippage
arbitrage example. The rejection fixture loses £47.83 on its accepted losing leg;
the incomplete-result fixture has £900 cash, £100 unsettled stakes and zero
realised profit. These are fictional results, not backtested real bookmaker returns.

### Reports, tests and future readers

Each run writes `report.json` (full typed report), `trades.csv` (one row per planned
leg, including rejections, actual fill prices and settlement sources), and
`bankroll.csv` (chronological cash, open stake and equity). Position P&L appears
only once per trade in the CSV. Reusing an output directory replaces these three
files. Default output under `backtest-output/` is ignored by Git; use an ignored
or external directory for custom output.

Ten synthetic histories cover success, no arbitrage, brief availability, stale
updates, rejection, price changes, limited funds, incompatible rules, incomplete
results and repeated observations. See [history scenarios](tests/fixtures/histories/README.md).
Run focused checks:

```sh
uv run pytest tests/test_backtesting.py tests/test_arbitrage.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

`HistoricalReader.read() -> HistoricalDataset` is the minimal adapter contract.
Local JSON and downloaded The Odds API directory readers are implemented.
Future other-provider or locally recorded-data readers must supply stable event/market/outcome
IDs, competition/participants, exact Decimal prices, original provider update
times (or explicit missing values), local observation/collection times, complete
outcome definitions and settlement verification. Meaningful performance claims
also need reliable result/adjudication evidence and realistic price availability,
bookmaker stake limits, latency and acceptance data. Historical snapshots alone
cannot establish that quoted prices were executable.

The in-memory implementation is intended for small normalised histories. It does
not consume provider-specific JSON, merge delta streams, reconcile settlement
corrections, infer rules, apply fees/taxes, or model real order books. Duplicate
event observations at a timestamp, conflicting current offers and duplicate or
corrected settlement records are rejected. No live adapters are implemented.

## The Odds API historical tennis integration

Obtain a key through [The Odds API](https://the-odds-api.com/) and set
`ODDS_API_KEY` in your shell or secret manager. In PowerShell, enter it without
putting the credential in command history:

```powershell
$env:ODDS_API_KEY = Read-Host "The Odds API key" -MaskInput
```

For older PowerShell versions, set the environment variable using your preferred
secret manager. `.env` is not automatically loaded. Never place credentials in
CLI arguments, data files or Git. The client authenticates using the documented
`x-api-key` header, suppresses underlying HTTP error details, redacts credentials
from stored response bodies and only retains selected response headers. It does
not follow redirects. Timeout defaults to 20 seconds; `--timeout` and `--retries`
are global options placed **before** the subcommand.

### Coverage and costs

The [official historical guide](https://the-odds-api.com/historical-odds-data/)
describes five-minute featured-market snapshots from September 2022 (earlier
coverage starts June 2020 at ten-minute intervals). A response resolves to the
closest snapshot at or before the requested date and includes neighbouring times.
Historical odds require a paid plan. A free-plan key can query historical events,
but that is not a zero-credit operation.

Per the [v4 endpoint documentation](https://the-odds-api.com/liveapi/guides/v4/),
the sports listing costs zero credits; dated event discovery costs up to one;
historical odds cost up to ten per region/market, with empty responses uncharged.
This implementation requests exactly one region and `h2h` market. It estimates
retry-inclusive upper bounds and records the returned quota headers. Access and
coverage are separate: current listings and dated event counts do not establish
UK bookmaker/h2h prices, and availability varies by competition/date.

Discovery gets ATP/WTA keys dynamically from `/sports?all=true`:

```powershell
uv run betting-history discover --sport tennis --date 2024-07-01T12:00:00Z
```

This only fetches the free current listing and prints the discovery cost. Copy a
key from the listing; no permanent competition list is hardcoded. Explicitly
investigate **one** competition at the selected date:

```powershell
$sportKey = Read-Host "ATP/WTA sport key from discovery"
uv run betting-history discover --sport-key $sportKey --date 2024-07-01T12:00:00Z --max-credits 2 --execute
```

`historical=not checked` in the listing is distinct from a dated event count. A
zero count or an unavailable competition is reported without inventing coverage.
Keys absent from today's listing may be supplied explicitly for investigation;
the API, not the key's spelling, determines whether history exists. Discovery
never loops over all competitions making chargeable calls.

### Worked small download

After checking a key/date, estimate a three-point, ten-minute range:

```powershell
uv run betting-history estimate --sport-key $sportKey --start 2024-07-01T12:00:00Z --end 2024-07-01T12:10:00Z --interval 5m --region uk
uv run betting-history download --sport-key $sportKey --start 2024-07-01T12:00:00Z --end 2024-07-01T12:10:00Z --interval 5m --region uk --output-dir data/tennis-history --max-credits 60
```

Both commands perform **no HTTP requests and write no files**. They need no key.
The range is inclusive, so there are three requested dates. The base bound is
30 credits; the default one retry per request gives a 60-credit upper bound.
Cache hits and empty responses may reduce actual cost. Disable retries with
`uv run betting-history --retries 0 estimate ...` to estimate the base bound.

Only this explicit opt-in performs paid historical requests:

```powershell
uv run betting-history download --sport-key $sportKey --start 2024-07-01T12:00:00Z --end 2024-07-01T12:10:00Z --interval 5m --region uk --output-dir data/tennis-history --max-credits 60 --execute
```

These commands demonstrate syntax, not a claim that the selected competition has
coverage on that date. Change the date after investigating actual coverage. Paid
access has been tested with mocked responses, not a real subscription in this task.

Downloads are capped at 100 requested dates per dataset, use intervals of at least
five minutes, and accept only single-region decimal `h2h` odds. No interpolation,
pagination loop or automatic bulk discovery is performed. Transient 429/500/502/
503/504 responses have bounded retries/backoff. Ambiguous transport timeouts,
authentication and access errors are not retried.

Every HTTP attempt reserves its documented maximum cost in an atomic journal
**before** sending. Reported `x-requests-last` replaces that reservation; absent
headers leave a conservative accounted charge and `actual_api_credits=null`,
not a fabricated zero. The cap includes retries and all earlier attempts in this
dataset. If the provider reports an unexpectedly larger charge, the downloader
records it and stops immediately; it cannot undo a charge the API already made.

Rerun the identical command/directory to resume; the cap may be increased. Cached
requests are not repeated. Provider `next_timestamp` references can prove that
another requested date maps to the same cached observation, avoiding a request.
An OS file lock prevents concurrent writers. Interrupted in-flight requests with
no saved response stay `uncertain` and are not automatically resubmitted, because
they may already have consumed credits. Review those cases against your API
usage before deliberately starting another dataset. A different range, region,
market or competition requires a different output directory.

### Cache, identity and offline replay

The ignored `data/` directory contains:

```text
data/tennis-history/
  manifest.json  # Version, plan, collection times, counts, gaps, missing slots, quota journal
  raw/           # Sanitised original response text plus status, quota and request metadata
  replay.json    # Unique normalised event snapshots, if any valid events exist
```

One raw receipt is kept per performed request for auditing; repeated actual
snapshot/event pairs appear once in replay data. Conflicting payloads for the
same actual timestamp are rejected. Missing prices and empty/invalid responses
are recorded, never filled in. Manifest gaps refer to returned observation times;
`missing_snapshots` separately identifies empty, failed, uncertain and unrequested
slots. Snapshot counts are unique replayable provider times, event counts are
unique internal events, and quote counts include each event observation.

Provider event references and competition keys determine stable internal event
IDs. Player IDs are explicitly **event-scoped**, derived from the event and
normalised source label because this response does not provide global player IDs.
Outcome IDs are anchored to those participant and market IDs, not array order.
Names alone never merge separate events. Only unambiguous `h2h` participant
selections are normalised; lay, handicap and period markets are not relabelled.
Malformed/ambiguous events are rejected with structured normalisation issues.

Actual snapshot time becomes `HistoricalSnapshot.observed_at` and quote
`observed_at`. Requested time and local download time remain separate metadata;
quote `collected_at` is the genuine local download time. A market-specific update
is used when supplied; otherwise the bookmaker update is used. Both original
update fields are preserved, and missing updates stay missing. Downloading in
2026 does not move a 2024 observation into 2026, nor does requesting 12:02 invent
an observation if the API returned 12:00.

Run the saved data with no credentials or network calls:

```powershell
uv run betting-backtest data/tennis-history --bankroll 1000 --stake 100 --output-dir backtest-output/real-history
uv run betting-backtest data/tennis-history --bankroll 500 --stake 10 --min-roi 1 --execution-delay 30 --output-dir backtest-output/risk-comparison
```

The existing arbitrage and replay algorithms are reused. Historical availability
time is checked instead of download time; legacy fixtures retain their original
collection-time behaviour. Five-minute observations cannot verify a fill thirty
seconds later. Strict exact-time execution confirmation remains unchanged; such
attempts fail with `no_execution_observation` rather than carrying prices forward.

Crucially, the odds endpoint does not verify event status, exclusivity/
exhaustiveness, market period or bookmaker settlement policies. These fields
remain unknown/unverified. Valid complete prices can produce **mathematical
candidates**, but the downloaded data alone produces no eligible paper fills or
realised profit. The mocked three-book example produces one candidate, zero
execution attempts and an unchanged £1,000 cash balance. This is not a fill study.

No final results are inferred from price movements. Optional separate adjudicated
result evidence uses the existing `SettlementRecord` schema:

```powershell
uv run betting-backtest data/tennis-history --results data/results.json --output-dir backtest-output/with-results
```

The file is a JSON array with canonical event/market/bookmaker/outcome IDs,
observation time, won/lost/void disposition, explicit historical/synthetic source,
and verified compatible rule evidence. Adding results does not manufacture the
missing execution/settlement verification of an odds-only dataset. Existing
synthetic settlement fixtures remain supported. Missing or mismatched evidence
leaves accepted positions unsettled, with exposure separate from realised P&L.

Run provider and integration tests entirely offline:

```sh
uv run pytest tests/test_odds_api.py tests/test_backtesting.py tests/test_arbitrage.py
```

They use [fictional provider responses](tests/fixtures/odds_api/README.md) and
HTTPX MockTransport; there are no automatically run paid/live tests.

## Current tennis odds: one-shot scanner

`betting-live` discovers active ATP/WTA singles competitions, fetches explicitly
selected UK `h2h` decimal prices, runs the existing arbitrage engine and archives
the entire response for offline research. It places no bets. Bounded monitoring
is a separate opt-in command below; there is no background service or dashboard.

Configure `ODDS_API_KEY` using the environment setup above. Discovery requires a
key but makes only the zero-credit sport-listing request. The same shared HTTPX
client, bounded retries, credential redaction and credit journal serve historical
and current requests. No new dependencies were added for live scanning.

Discover active competitions in PowerShell:

```powershell
uv sync --locked
uv run betting-live discover
$sportKey = Read-Host "Copy one active singles sport key from discovery"
```

`uv run betting-live` without a subcommand also defaults to discovery. Keys are
obtained dynamically; inactive competitions, doubles, outrights and listings
without explicit singles wording are excluded conservatively. An active listing
does not promise any UK match prices. Empty discovery is reported clearly and
never triggers queries against every tournament.

Estimate one scan without credentials, network calls or new files:

```powershell
uv run betting-live scan --sport-key $sportKey --stake 100 --max-credits 5 --output-dir data/live-tennis
```

Only the following explicitly opted-in command fetches current odds:

```powershell
uv run betting-live scan --sport-key $sportKey --stake 100 --max-credits 5 --output-dir data/live-tennis --execute
```

Every selected key is checked against a fresh free active-singles listing before
its odds are requested. To select multiple competitions, repeat the option:

```powershell
$secondKey = Read-Host "Another active singles key"
uv run betting-live scan --sport-key $sportKey --sport-key $secondKey --stake 50 --max-credits 5 --output-dir data/live-tennis --execute
```

Duplicate key selections are collapsed; at most ten explicit selections are
allowed. Calls are sequential. `--max-age 300` controls the provider-update
freshness threshold in seconds. `--stake` must be a finite positive GBP amount
in whole pennies. Global `--timeout` and `--retries` precede the subcommand, e.g.
`uv run betting-live --retries 0 scan ...`.

### Live costs and safeguards

The [official current-odds documentation](https://the-odds-api.com/liveapi/guides/v4/)
documents a one-credit base cost for one region/market and no charge for empty
responses. UK `h2h` therefore has a one-credit per-competition estimate. With
the default one retry, the conservative bound is two credits per selected key;
actual charges are tracked separately from the retry allowance.

The default five-credit cap is **cumulative for the archive directory**, not a
fresh allowance on each invocation. Requests reserve credits before network I/O
and persist all three quota headers. Missing quota headers leave actual usage
unknown and retain the conservative reservation. Interrupted uncertain attempts
also retain their reservation. A scan stops when either local headroom or the
provider's most recently reported remaining credits cannot cover the next call.
Retries share that cap. Increase `--max-credits` deliberately for subsequent
scans; changing directories creates a separate local budget, not extra provider
allowance. No code increases your API allowance or bypasses provider access rules.

All live verification in this task used mocked HTTP, not real API credentials.
Real scans must be run explicitly by the user with a valid key and remaining quota.

### Reading candidates

Filtering uses the actual local receipt timestamp: events whose `commence_time`
is at or before receipt are excluded from analysis, including events that start
while a request is in flight. Events starting shortly afterward are retained.
The operational scheduled/live status follows that timestamp comparison; it is
not an independent confirmation of the real match clock.

For each mathematical candidate, the terminal prints competition, participants,
start time, selected bookmaker names, odds, hypothetical penny stakes, theoretical
ROI, rounded worst-case profit, provider update timestamps/ages, verification
status and reasons for failed verification. The zero-opportunity summary still
reports events, bookmakers, quote counts, market checks and credits consumed.
Stale/future updates are excluded by the existing engine. Missing updates remain
unverified; receipt time is never substituted for a missing bookmaker update.

The API does not independently verify settlement contracts or complete market
metadata. Those fields remain unknown rather than being inferred from bookmaker
names. For example, the fictional 2.20/2.40 test case reports £52.17/£47.83 stakes
and £14.77 rounded worst-case profit on £100, but **zero eligible opportunities**
because verification is missing. A positive mathematical margin is a research
candidate, not a claim of an executable or guaranteed profitable bet. Prices can
change before manual acceptance; retirement rules, bookmaker limits and exchange
commissions can also change the outcome of an apparent opportunity.

### Saved observations and replay

Default output is under ignored `data/live-tennis/`:

```text
manifest.json      # mode=live, format version, cumulative quota journal, requests and indices
discovery.json     # Latest confirmed active singles listing
raw/               # Sanitised received response text, timestamps and quota headers
observations/      # All valid normalised quotes, issues, excluded starts and scan results
scan-report.json   # Latest scan summary and detailed mathematical results
replay.json        # Chronological unique pre-match observations, when available
```

Raw and normalised started-event data are retained for auditing, while only
pre-match observations enter replay. All prices are stored, not only selected
candidate legs. Invalid data remains visible in raw receipts and validation
issues. Empty observations are recorded and do not fabricate a replayable market.
The observation clock is local receipt time; original event/bookmaker IDs and
bookmaker update times are preserved. No historical snapshot timestamp is invented.

Identical responses at exactly the same competition/receipt time are indexed
once. Conflicting responses at the same instant are excluded from replay. New
receipt times are separate observations even when prices are unchanged; the
scanner never relabels a cached response as freshly received. The archive's OS
lock prevents overlapping writers. Interrupted raw receipts can be recovered at
their original timestamps; requests with no receipt remain uncertain, not retried
automatically for the old time.

Replay without a key or API calls, choosing a new hypothetical budget:

```powershell
uv run betting-backtest data/live-tennis --bankroll 1000 --stake 100 --output-dir backtest-output/live-replay
uv run betting-backtest data/live-tennis --bankroll 500 --stake 10 --execution-delay 30 --output-dir backtest-output/live-delayed
```

Unverified observations support mathematical analysis but not fabricated fills.
If later verified data supports execution, the existing exact execution-time
observation requirement still applies; a thirty-second gap cannot be filled with
an old quote. No final tennis results are inferred, and missing settlement
evidence leaves any accepted paper exposure unsettled. Archives containing only
empty, invalid or started-event observations give a clear no-replayable-data error.

Run the live and integration tests offline:

```sh
uv run pytest tests/test_live.py tests/test_odds_api.py tests/test_backtesting.py tests/test_arbitrage.py
```

The [live fixtures](tests/fixtures/live/README.md) are fictional, all HTTP uses
MockTransport, and no test performs a real scan. Repeated scans require the
explicit bounded monitoring command below; nothing starts automatically.

## Controlled repeated scanning

`betting-live monitor` calls the existing single-scan implementation sequentially.
Defaults are a 300-second interval between **scan starts**, £100 hypothetical
stake, a ten-credit **session** cap and at most ten scans. The first scan starts
immediately; without delays ten starts would be at +0, +300, …, +2700 seconds.
`--duration` optionally adds a limit in seconds; the earliest count, time or
credit limit wins. The Python API also supports duration-only limits. Unbounded
configuration, nonpositive intervals/durations and invalid stakes are rejected.

Use an active singles key already returned by discovery:

```powershell
$sportKey = Read-Host "Active ATP/WTA singles sport key"
uv run betting-live monitor --sport-key $sportKey --interval 300 --stake 100 --max-credits 10 --max-scans 10 --output-dir data/live-tennis
```

This is a **dry run**. It validates configuration, reads existing credit usage,
shows the scheduled start offsets and retry-inclusive cost, and estimates how
many complete scans the budgets can fund. It makes no HTTP requests, needs no
API key, and does not create directories, locks or report files. For schedules
longer than twenty starts, only the first twenty offsets are displayed.

Explicitly authorise a foreground session:

```powershell
uv run betting-live monitor --sport-key $sportKey --interval 300 --stake 100 --max-credits 10 --max-scans 10 --output-dir data/live-tennis --execute
```

Or add a thirty-minute duration limit:

```powershell
uv run betting-live monitor --sport-key $sportKey --interval 300 --stake 100 --max-credits 10 --max-scans 10 --duration 1800 --output-dir data/live-tennis --execute
```

Repeat `--sport-key` for multiple explicitly selected competitions. Monitoring
does not discover other competitions and start querying their odds automatically.
Each iteration rechecks active singles coverage through the free listing. A
competition becoming unavailable stops the session conservatively.

### Two independent credit limits

The monitoring `--max-credits` limit applies only to **this session**. It neither
resets past usage nor increases the persistent archive cap. Each API attempt is
tagged with its session ID in the existing quota journal, including retries and
unknown charges. Other sessions and manual single scans still count against the
archive cap, but are not charged to this session's total.

For example, an existing archive with one credit used and a five-credit cap
retains both values when you request a ten-credit session. Only four archive
credits remain. One competition with the default one retry has a two-credit
worst-case bound per scan, so the dry run can guarantee budget headroom for two
complete scans, not ten. Unused retry allowances are not spent, so more successful
scans may fit before the next full retry allowance no longer fits. Use global
`--retries 0` before `monitor` if you explicitly prefer no retries.

Before each scan, the entire competition set's conservative retry bound must fit
the session limit, archive headroom and API-reported remaining credits. The
single scanner also checks headroom before each competition, and the shared
ledger checks every attempt/retry. Monitoring stops rather than silently raising
the cap. To change an existing archive's cap deliberately, use the established
single-scan `--max-credits` workflow; monitoring itself preserves it. A new
directory starts with a persistent cap equal to the requested session cap.
A discovery-only directory with an existing zero cap remains zero.

Actual charged credits and conservatively accounted credits are reported
separately. Missing quota headers and interrupted requests retain their reserved
cost. Unexpected provider charges are recorded and stop the session; the program
cannot reverse a charge already made by the API. Authentication failures are
not retried by the monitor. Rate-limit/server retries stay bounded, share both
budgets and use the existing client/backoff policy.

### Scheduling and stopping

Scheduling uses monotonic time and a fixed session anchor, so request duration
does not accumulate into the polling interval. Slow scans, delayed wake-ups or
computer suspension skip missed slots instead of launching overlapping requests
or a burst of catch-up scans. Wall-clock changes do not shift the interval.

Choose an interval that fits your quota and the usefulness of the observations;
the five-minute default is a cautious starting point for local research, not an
assurance that quotes remain available between polls. Faster scans consume
allowance faster and still cannot establish bookmaker acceptance.

Press **Ctrl+C** in the terminal to stop. The command saves partial results,
records `user_interruption`, retains uncertain credit reservations, releases its
locks and displays the final summary. Limits also stop the session automatically
for scan count, duration, session/archive credit headroom or API quota. Provider,
authentication and unrecoverable local-data errors are recorded as distinct
stops. No new request starts at or after the duration deadline; a request already
in flight can finish afterward, bounded by the configured HTTP timeout. Retry
backoff waits are truncated at the duration deadline.

The computer must remain running and the foreground command/terminal must stay
open. There is no Windows service, scheduled startup task, cloud host, push
notification or auto-resume process. Starting another session is an explicit
command. A separate monitor lock prevents two monitors using the same archive;
existing per-scan locks still prevent overlapping archive writers.

### Candidate lifecycle and saved reports

A candidate's deterministic identity uses its event, market and sorted
outcome/bookmaker/provider combination. Odds, hypothetical stakes and ROI are
values on that record, not its identity. Small odds changes update the same
candidate; a different selected bookmaker/outcome combination creates a new one.
First and most recent observed times, latest theoretical ROI, rounded worst-case
profit, verification status and the full detector result are retained.

Per-scan output distinguishes **new**, **continuing**, **no longer observed**
and **not checked** candidates. A successful response with no matching candidate
can establish “no longer observed”; failed or malformed observations cannot.
Reappearing combinations continue their existing record rather than becoming
another new discovery. The candidate index persists across sessions in this
archive. “No longer observed” describes the selected feed result, not proof that
every alternative price combination ceased to exist.

Each scan prints actual observation timestamps, observed competitions, events,
bookmakers, quotes, mathematical candidates, lifecycle counts, unverified/rejected
counts and actual/accounted credits with remaining session allowance. Candidate
details reuse the single-scan presentation: match, bookmakers, odds, hypothetical
stakes, ROI, penny profit, quote freshness and verification. Unknown settlement
rules stay unknown; monitoring never upgrades a candidate to verified executable
profit. The final summary includes scan count, event observations versus unique
events, unique candidates seen this session, credits, skipped slots and stop reason.

All raw receipts and normalised observations use the existing archive. Additional
files under the same ignored directory are:

```text
monitoring/monitor-000001.json  # Atomic running/final session checkpoints and per-scan results
candidate-tracking.json        # Cross-session identities and last confirmed lifecycle state
```

Interrupted scans preserve valid partial observations at their actual timestamps.
Missing receipts stay uncertain; no old quote is relabelled as newly received.
Offline replay continues to use `replay.json` and its existing strict freshness,
execution-time and settlement safeguards:

```powershell
uv run betting-backtest data/live-tennis --bankroll 1000 --stake 100 --output-dir backtest-output/monitor-replay
```

Run focused monitoring and integration tests without real waits or credits:

```sh
uv run pytest tests/test_monitoring.py tests/test_live.py tests/test_odds_api.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The tests inject wall and monotonic clocks, mock HTTP and advance simulated time
instead of sleeping. No dependencies or betting strategies were added.

## Planned scope and limitations

Subsequent tasks may add other provider adapters, database storage
and a dashboard. None of those features exists today. The current engine uses supplied
normalised data, explicit live observations, cached historical datasets and synthetic fixtures.
It does not place bets or run unattended background services.

The models and engine already support synthetic three-way football examples.
Longer-term live-data possibilities include football, basketball, horse racing,
bookmaker/exchange arbitrage, statistical value betting, non-transitivity,
cross-market probability inconsistencies and other providers such as OddsRelay.
These are architectural considerations only, with no implementation in this task.
