# Synthetic current-odds API examples

`sports.json` contains fictional active ATP/WTA singles listings, an inactive
competition and a doubles competition. `odds.json` is the documented bare array
shape of the current v4 odds endpoint, with three fictional books and fictional
players. No file was downloaded from the real API.

Tests inject 2024-07-01 12:00 UTC as the collection clock and mock all HTTP calls.
Derived cases cover starts at/before/after collection, stale/missing timestamps,
partial markets, invalid prices, errors, rate limits and credit caps. Unknown
settlement information stays unknown. Real-world claims are not implied by the
synthetic prices or hypothetical allocations.
