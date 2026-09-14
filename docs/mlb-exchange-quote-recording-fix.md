# Preserve unpriced exchange outcomes with available sportsbook data

The natural research follow-up on September 14 recovered Dodgers–Reds inputs and
started producer nonce `fc103ae24a0e4dd3a457f24f6a4e0b18`. The producer completed
10 game reads: one supported pass, seven incomplete-input records, and two
`not_priced` records for missing current exchange quotes. It proposed zero cards.

The writer rejected the whole draft because Stage 2 classified those two games as
`ready_for_evaluation`: its price check refers to sportsbook prices, whereas the
read's missing price was the separately acquired exchange quote.

The data-completeness check now permits this narrow distinction when baseball
inputs are ready, `polymarket_ask` is null, a nonempty unavailability reason is
recorded, and `no_polymarket_market` is named. Missing baseball data remains
incomplete; missing sportsbook prices retain the original rule. No quote is
invented, no record becomes a pass or candidate, and no edge threshold changes.
The full writer still validates all read fields, arithmetic, identity, and policy.

## Exact retained-draft comparison

At the original as-of time `2026-09-14T19:51:38+00:00`, in separate temporary roots
with the same explicit test policy fixture and retained scan/coverage/run receipt:

- Merged base `81cdd17` rejects games 823575 and 823006 with
  `not_priced cannot describe ready_for_evaluation`.
- The fix lands all ten reads, preserving those two `not_priced` dispositions
  and zero candidates.
- Draft SHA256: `d23a3f732f443c778c67c618d2e421860a4844d1de7c081db4cb5c7a4fb699cc`.
- Scan SHA256: `e9ab48f82a00a3d730838d58cf3525ac6187896b229fd01443c4e66f49b75702`.

This is an isolated writer replay, not a current-price assessment or a live landing.
The original runtime draft and failed attempt remain intact. Runtime activation and
another fresh producer run follow merge; do not reset attempt counters or manufacture
an old successful receipt.

Full suite: 1,836 tests and 714 subtests passed, with one optional dependency skip.
The 63-test focused writer/data/producer suite also passes. Negative cases explicitly
exercise missing baseball data, a present quote, missing reasons, wrong rails, and
an invalid pass, both at the classification function and through the real writer.
