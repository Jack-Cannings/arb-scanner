# Synthetic fixtures

All participants, events, bookmaker names, prices and rule documentation are
fictional. No fixture represents a real bookmaker's settlement policy. Collection
time is fixed at 2026-06-01 12:00 UTC; event start is the next day. These dates are
test data, never a reference to the current clock.

Files A-C and E-H are normalised `OddsSnapshot` JSON objects. File D is an array
of deliberately invalid `OddsQuote` objects; each must fail odds validation.
Decimal prices are JSON strings to preserve exact values.

| File | Scenario |
| --- | --- |
| `a_atp_candidate.json` | ATP, three books, best prices 2.10 for opposite players at books 1 and 2. Implied total is 20/21, below 1; synthetic rules are verified and identical. |
| `b_wta_no_arbitrage.json` | WTA, three books, best prices 1.90 and 1.95. Implied total is 770/741, above 1. |
| `c_missing_outcome.json` | Complete outcome definition, but book 3 has only the first outcome quoted. |
| `d_invalid_odds.json` | Zero, one, negative, NaN, both infinities and malformed text. |
| `e_stale_quote.json` | Old provider update alongside recent collection; another quote has a missing update. |
| `f_incompatible_settlement.json` | Match completion and one completed set are distinct documented rules. |
| `g_unknown_settlement.json` | Explicit and omitted rule information both remain unknown and unverified. |
| `h_three_outcomes.json` | Football home/draw/away with three quotes per bookmaker. |

The margins above describe the authored fixtures only. There is no arbitrage
calculation code. A candidate fixture is not a finding of real betting eligibility.
