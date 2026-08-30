# Historical pick replay & attribution — report contract

`scripts/vig_pick_replay.py` answers the two questions the audit deliberately
did not: **why did executed picks miss**, and **why did we pass on winners**.

```bash
# Offline, against whatever the audit's results cache already holds.
python scripts/vig_pick_replay.py --picks-dir ~/projects/sports-picks-runtime/.picks

# Opt-in cache population (the ONLY side effect, delegated to the audit's
# fetch helper; writes the explicit results cache and nothing else).
python scripts/vig_pick_replay.py --results-dir /tmp/mlb-results --fetch

python scripts/vig_pick_replay.py --json
```

## Foundation: the audit, not a copy of it

Reconciliation, official-result provenance, normalization, and every
per-candidate classification come from `vig_historical_audit.build_report` —
the same code path the merged audit runs. This module never re-derives an
outcome, re-parses a schedule shape, or fetches a score itself. Its sibling
imports are pinned to `vig_historical_audit` and `vig_calibration_report`,
its transitive closure is pinned off the execution path, and it contains no
write calls at all (the guard requires the `--fetch` delegation to be present
so the no-writes assertion cannot pass vacuously).

## What the report contains

| Section | Population | What it separates |
|---|---|---|
| Attribution matrix | every candidate, exactly once | disposition × official outcome |
| Cohorts | executed vs passed | side quality (win rate + Wilson CI) from economic quality (synthetic units) |
| Missed winners | passed candidates whose side won | each with its recorded `skip_reason`, price, band |
| Executed losses | executed candidates whose side lost | price bands + field presence |
| Profiles | per cohort | process/data completeness, confidence values, skip reasons |
| Rule candidates | replay-eligible records | bounded rules graded leave-one-month-out |

**Controls are never bets.** A no-pick control day carries no candidate, so it
contributes nothing to the passed cohort. And the passed cohort covers only
candidates the slate PROPOSED and declined — games never proposed at all are
out of scope, so the missed-winner count is a floor on missed opportunity,
not a measure of it. The report states both.

## Synthetic economics

Only 8 corpus cards carry a real P&L, so cohorts are graded with a flat
one-unit stake at the record's effective price (paid price when recorded,
quoted ask otherwise): a win at price *p* returns *(1−p)/p* units, a loss
forfeits the unit, a push returns it. A record with no decided outcome or no
usable price contributes `None`, never zero — "no evidence" and "broke even"
are different facts. Every derived number is labelled synthetic and travels
with the caveats (quoted asks are not fills; gross of fees).

## Rule-change candidates: bounded, and never tuned where they are graded

The rule sets are fixed enumerated dictionaries — price-band filters plus a
mandatory no-change rule per cohort (`keep_all` for executed exclusions,
`add_none` for passed inclusions), so "change nothing" can win honestly.

Grading is **leave-one-calendar-month-out**: the rule applied to a held-out
month is the one with the best synthetic units on all *other* months
(deterministic tiebreak by rule name). Only held-out results aggregate. A
fold whose selection months hold fewer than `--min-selection` eligible
records is reported `insufficient_selection` and grades nothing. The
in-sample table is printed as reference only and is never a verdict.

`tests/test_vig_pick_replay.py` proves the no-leak property with a fixture
where in-sample selection, held-out-slice selection, and honest
complement-selection all disagree — a grader that leaks in either direction
changes the answer and reds the test.

## What this report refuses to do

- Execute, size, or route anything; import anything on the execution path.
- Mutate a historical schedule or any file outside the opt-in results cache
  (which the audit's helper owns, not this module).
- Promote a synthetic or insufficient-sample number into a recommendation:
  every rate carries its CI, every cohort its sample flag, and the held-out
  aggregate is the only number offered for decision-making.
