# Sports Betting Scanner

A modular Python project for analysing pricing inefficiencies across bookmakers.
The first planned application is a pre-match ATP/WTA tennis match-winner
arbitrage scanner using UK bookmaker odds from The Odds API. Opportunities will
be presented for manual investigation.

## Current implementation

Project foundations, provider-independent core data models, an offline
cross-bookmaker arbitrage engine and historical paper replay are implemented:
an installable `src/` package, typed environment configuration, validated odds
records, synthetic JSON fixtures, offline tests, linting, formatting, strict type
checking and Linux/Windows CI. The application **cannot scan live odds yet**.
The engine compares normalised prices and calculates hypothetical stakes and
profits. Historical replay adds a virtual bankroll and JSON/CSV reports.
There are no API requests, database or dashboard.
There is no bookmaker account integration, real bet execution, continuous polling or
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
  strategies/arbitrage/  # Selection, Decimal mathematics and typed results
  backtesting/          # Historical readers, replay, execution, settlement, reports
  storage/               # Future persistence
  interfaces/backtest.py # Historical replay command line
tests/test_config.py     # Offline configuration tests
tests/test_models.py     # Domain validation and fixture tests
tests/test_arbitrage.py  # Selection, verification, mathematics and penny allocation
tests/fixtures/          # Eight deterministic synthetic JSON scenarios
```

Providers and storage remain placeholders. Dependency boundaries:

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
not change the calculations. No provider parser is implemented yet.

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
normalised to UTC and serialised with `Z`. Missing provider timestamps remain
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
and uses an injected `ReplayClock`, never the wall clock. Quotes collected after
an observation are recorded as unavailable and excluded. At each step the
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
Only a local JSON reader is implemented. Future The Odds API, other-provider or
locally recorded-data readers must supply stable canonical event/market/outcome
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

## Planned scope and limitations

Subsequent tasks will add provider adapters, live odds retrieval, database storage
and a dashboard. None of those features exists today. The current engine uses supplied
normalised data and synthetic fixtures only. It does not place bets or run polling
or background jobs.

The models and engine already support synthetic three-way football examples.
Longer-term live-data possibilities include football, basketball, horse racing,
bookmaker/exchange arbitrage, statistical value betting, non-transitivity,
cross-market probability inconsistencies and other providers such as OddsRelay.
These are architectural considerations only, with no implementation in this task.
