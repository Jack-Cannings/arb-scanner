# Synthetic provider responses

These wire-format examples are fictional, not downloaded bookmaker observations.
`historical.json` uses the documented v4 historical envelope, three fictional
books and a fictional ATP event. Its prices are numeric JSON literals to exercise
Decimal parsing without an intermediate binary float. `sports.json` contains
fictional ATP/WTA listings and a non-tennis entry for discovery filtering.

Tests use HTTPX MockTransport. No real credentials, paid requests or real match
results are needed. Modified envelopes exercise duplicates, invalid odds,
missing updates, gaps, errors and resumable credit accounting.
