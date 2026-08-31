# Historical pick replay & attribution — report contract

`scripts/vig_pick_replay.py` answers the two questions the audit deliberately
did not: **why did executed picks miss**, and **why did we pass on winners**.

```bash
# Offline, against whatever the audit's results cache already holds.
python scripts/vig_pick_replay.py --picks-dir ~/projects/sports-picks-runtime/.picks

# Opt-in cache population (the ONLY side effect, delegated to the audit's
# fetch helper; writes the explicit results cache and nothing else).
# --picks-dir (or SPORTS_PICKS_ROOT) is always required — it names the
# schedules whose dates get fetched.
python scripts/vig_pick_replay.py --picks-dir ~/projects/sports-picks-runtime/.picks \
    --results-dir /tmp/mlb-results --fetch

python scripts/vig_pick_replay.py --picks-dir ~/projects/sports-picks-runtime/.picks --json
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
| Executed losses | executed candidates whose side lost | price bands + field presence, with the executed WINS bands printed alongside as the denominator |
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

**Push policy.** A priced push is replay-eligible: the stake came back, which
is economic evidence worth zero units, not absent evidence. Pushes therefore
count in `replayable_with_price`, `synthetic_units`, and the leave-one-month-
out selection and held-out samples. The **win rate, its Wilson interval, and
the `--min-sample` sufficiency gate stay strictly wins/(wins+losses)** — a
push says nothing about side-picking skill, and counting it toward
sufficiency would let push-heavy cohorts make rate claims on fewer decided
records. Cohort summaries report `pushes` and `resolved` alongside `decided`
so both populations are named.

One knob, two uses, disclosed: `--min-sample` is both this report's cohort
claim threshold and the value passed through to the audit as its calibration
bucket threshold. Both gates answer "how few records may back a rate", and a
single floor keeps the two reports from disagreeing about sufficiency.

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

When the best selection score is shared, the graded fold reports the tied
rules in `selection_ties` and the render says the winner came from name
order — a rule that won on the tiebreak is a different fact from one that
won on the economics.

`tests/test_vig_pick_replay.py` proves the no-leak property with a fixture
on which the three possible selection sets disagree with strictly distinct
scores — the honest complement chooses `keep_under_0.50`, tuning on all data
chooses `keep_all`, and tuning on the held-out month chooses
`keep_0.40_to_0.55`. The test asserts those three argmaxes directly (refusing
any tie) and then asserts both the fold's chosen rule and its selection-set
units, so a leak in either direction changes the answer and reds the test.

## Side-selection attribution

Every candidate gets a structured `side_selection_attribution` record stating
what the card RECORDED about the side choice — never a reconstructed reason:

- **Selected side** (canonical from the official row when reconciled, the
  card's own value otherwise) and its recorded evidence: `thesis`,
  `vig_notes`, model probabilities, prices, gate reasons, and on Phase 2
  cards the structured `candidate_failure_path` and `named_risks`. A card
  with none of these is labelled `not_recorded` explicitly.
- **Opponent side**, named from the official row (identity only — which team,
  never how the game ended), falling back to the card's own matchup when
  unreconciled; unresolvable stays `None`, labelled.
- **Why the opponent was not selected**, a closed category set:
  `opponent_case_recorded` (the card carries an explicit
  `opponent_shutdown_path`), `recorded_case_backed_selected_side` (a recorded
  pregame case backs the chosen side and no separate opponent case exists),
  or `not_recorded`.
- **Opposing winners** — reconciled candidates whose selected side lost — are
  enumerated separately from the per-candidate records and classified from
  recorded STATE (disposition and `vig_approved`), never from what a
  reason's prose says: `evidence_process_miss` (executed on recorded
  evidence that pointed the wrong way), `executed_without_recorded_evidence`,
  `approved_not_executed` (the review approved the side and no order was
  ever placed — manual routing, pipeline failure, or a post-approval
  execution-time stop; these are neither gate saves nor evidence misses,
  and the recorded reason travels verbatim because post-approval reasons
  are too heterogeneous to grade mechanically), `risk_gate_declined`
  (a recorded gate declined the losing side before approval, so the gate
  was not the miss; the winning opponent was never itself proposed), or
  `no_recorded_reason`. `vig_notes` is not a gate signal: it is present on
  approved-and-executed cards too, so its existence carries no polarity.
- **Category counts are zero-filled** over each closed set, so a category
  that has never occurred prints as 0 instead of vanishing. On the corpus
  to date every card records a thesis and none records an opponent case, so
  several categories (`opponent_case_recorded`, `not_recorded`,
  `card_matchup`/`card_only` resolutions, and some miss classes) have only
  ever varied in synthetic tests — the zeros in the report are how a reader
  can tell.

**No hindsight leakage.** The rationale inputs are the audit's
`recorded_rationale`, which carries only pregame fields (`thesis`,
`vig_notes`, `execution_note`, and the `baseball_evidence` subset) verbatim;
the postgame vocabulary (`postgame_reflection`, `scoring_summary`,
settlement fields) is excluded at the carrier, and a test proves the
rationale half of every record is byte-identical when the official winner is
flipped. Outcome fields appear only as labels and to select the
opposing-winner cases. Headline replay totals are unchanged: a test strips
every rationale input and asserts every pre-existing section is identical.

## What this report refuses to do

- Execute, size, or route anything; import anything on the execution path.
- Mutate a historical schedule or any file outside the opt-in results cache
  (which the audit's helper owns, not this module).
- Promote a synthetic or insufficient-sample number into a recommendation:
  every rate carries its CI, every cohort its sample flag, and the held-out
  aggregate is the only number offered for decision-making.
