# Fictional historical replay scenarios

All events, prices, fill instructions and result adjudications are synthetic.
No file represents actual bookmaker prices, acceptance or settlement policies.
Observations begin at 2026-06-01 12:00 UTC, event start is 13:00 UTC and result
evidence is observed at 14:00 UTC. Every snapshot is a full event replacement.
The JSON files can be loaded with `JsonHistoricalReader`; outcome results are
per-bookmaker/selection records with explicit rule evidence and source labels.

| History | Scenario and baseline expectation |
| --- | --- |
| `a_success.json` | Both selected bets accepted. With default 0.5% slippage, £100 stakes produce £14.20 realised hypothetical profit. |
| `b_no_arbitrage.json` | Two observations of non-profitable prices; no paper orders. |
| `c_brief.json` | Prices cease to offer arbitrage after one second. With `--execution-delay 10`, no due-time quote confirmation exists. |
| `d_stale.json` | Newly collected but old provider prices; no executable opportunity. |
| `e_rejection.json` | The winning side is rejected; only the losing £47.83 bet is accepted and settled. |
| `f_price_change.json` | Due-time observation at ten seconds shows reduced prices. Use `--execution-delay 10` to test revalidation failure. |
| `g_bankroll.json` | Two distinct events before settlement. Use `--bankroll 100`; the second £100 attempt cannot be funded. |
| `h_incompatible.json` | Mixed best prices offer mathematical arbitrage, but no compatible verified subset does. |
| `i_incomplete_results.json` | Only one accepted leg has result evidence; £100 stays unsettled and realised P&L stays zero. |
| `j_repeated.json` | Three unchanged price observations with refreshed timestamps; one unique opportunity and one attempt. |

Baseline uses zero execution delay and assumes acceptance unless explicitly
overridden. C and F can therefore fill immediately if no delay is supplied; G can
fund both positions under the default £1,000 bankroll. These are configurable
stress scenarios, not fixed conclusions independent of simulation assumptions.
