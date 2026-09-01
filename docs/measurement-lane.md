# MLB measurement lane

`scripts/mlb_measurement_lane.py` measures what the card did with **every
scheduled game**, from recorded state only. It is read-only: no network, no
live state, no gate or policy change, and no execution path anywhere in its
import closure's behaviour.

It exists because a skip list is not a measurement. `mlb_model_eval_dataset`
builds the rows the deployment gate consumes, and it is right to skip a read it
cannot score — an evaluation set with an imputed probability in it is worse than
a smaller one. But the question this lane answers, *why are we not getting
picks, and are we right when we do*, is answered by the games that produced no
row at all just as much as by the ones that did. So every scheduled game gets a
row, and a game whose read carries no usable number gets a row saying which
field is missing and what reason the run gave for it.

## Running it

```
python3 scripts/mlb_measurement_lane.py \
    --schedules .picks/execute --finals .picks/audit-results \
    --start 2026-09-01 --until 2026-09-21 \
    --out-json measurement.json --out-markdown measurement.md \
    --out-rows measurement.jsonl
```

`--schedules` is repeatable for multiple roots. `--finals` is a directory of
cached StatsAPI schedule payloads named `<date>.json`; there is no fetch flag,
because a lane that can reach the network is a lane whose read-only claim rests
on a flag nobody passed.

## The row

One row per scheduled game, **always the away side**. Both sides would double n
with perfectly anti-correlated rows and break the independence every metric
here assumes; "the side the model favoured" would re-introduce exactly the
selection bias this population exists to remove. That rule is inherited from
the dataset builder rather than re-decided, and a test pins that it is the same
constant and that the row actually reads it.

Every field carries its own provenance, in one of four states:

| provenance | means |
| --- | --- |
| `recorded` | the run wrote the value |
| `unavailable` | the run said the value was absent, and the row carries its words |
| `unexplained_absence` | absent with no reason — which the recorder's own validator refuses at slate time, so seeing one here means a read reached this report by a path that skipped that gate |
| `never_captured` | the pipeline has never had this field at all (see below) |

`captured_at_utc` is the roster's fetch time from `slate_denominator`, and says
so: no read carries its own capture time, so it is not a per-game observation
and is not reported as one.

`starter_availability` and `lineup_availability` are `pending` or `not_stated`,
**never `confirmed`**. Nothing in a read states that a starter was announced or
a lineup posted; what exists is the rail the run named when one was not. The
absence of a complaint is not a confirmation, and reporting one as the other
would manufacture exactly the input provenance this lane exists to stop
inventing.

## Fidelity and source quality

Two independent closed axes, both zero-filled — a bucket that never occurred
prints `0`, because a reader cannot otherwise tell a constant axis from an
impossible one.

- **fidelity**: `recorded_handicap`, `no_handicap`, `unusable_read`, `no_read`.
- **source quality**: `market_only_fallback`, `non_market_model`,
  `not_applicable`. Under the market-only fallback our raw probability *is*
  DK's own de-vigged fair, so a model-versus-DK number computed across both
  would be partly DK measured against itself. The split survives into every
  aggregate.

## No blended headline

There is no combined Brier, log-loss, record, or calibration key anywhere in
the output. Every metric block names its fidelity, its source quality, its
model version, and its own n; the market comparison reports its own n
separately, because the two populations differ whenever a read carries a
handicap and no DK line.

That is blocker 1 on PR #75 turned into a structural property. There, half the
outcome record was drawn from a different fidelity of input and the headline
did not say so. The fix was to make the combined key impossible to write rather
than to remember not to write it.

**The property is pinned where a headline would actually land.** The first
version of this lane asserted it on `aggregate()` alone, one level below the
two things a reader sees, and a blended Brier added to `report()` left the
whole suite green (blocker 2, review round 1). The guard now walks the JSON
payload recursively and requires every metric key to sit inside a bucket that
names its own fidelity, source quality and n, walks the rendered Markdown for
the same words outside a bucket heading, and carries a positive control that
proves both guards fail on the headline they exist to catch.

## Schedules opened

Beside the per-read counters, the report carries a **schedule-level** audit,
because two whole-date failures are invisible to the row counts.

A date whose file was read but whose `slate_denominator` is missing or
malformed contributes no rows — `denominator_games` returns `[]` for that with
no raise and no log. Counted as a used date with zero rows, the page reads *we
measured these slates and found nothing in them* when the truth is *we never
opened them*. This is not a corner case: across 617 schedule files on this
fleet, 613 carry no usable denominator, so every historical date takes that
path. Each one is now named, with which of the three ways it failed — no
denominator object, no games list, or a denominator listing zero games.

A `game_reads` entry whose `game_pk` is not in the denominator is dropped, and
that is correct: the denominator is the roster the recorder cross-checks
against a fresh scan, and a run must not be able to add rows to its own
population. But the recorder's own validator calls exactly that an error, so an
orphaned read reaching this lane is evidence the slate was written
unvalidated. It is dropped **and counted**, with its date and `game_pk`.

Both are the same rule as the `unusable_read` bucket, one level up: a record
that reached this report without passing the recorder's gate is itself the
finding.

## Attribution

**Why we did not bet** — from recorded state, closed and zero-filled:
`not_refused`, `process_missing_input`, `gate_handicapping_rail`,
`gate_volume_cap`, `gate_candidate_from_inferred_input`, `unclassified_rail`.

A missing input **outranks** a handicapping rail named beside it. A gate cannot
be said to have refused a game it was never able to price, so a read naming
both `no_dk_price` and `price_discipline` is a process failure that happens to
have written down a gate as well. The precedence is stated rather than left to
the order the run happened to write the rails in.

`gate_candidate_from_inferred_input` is structurally unreachable in this
population: recorder rows have no inferred inputs, only recorded ones and
explained absences. It stays in the set and prints zero, because a category
that is impossible here and one that merely did not occur are different facts.

**What the result says about the read** — `unattributed_no_game_script`,
`pending_no_final`, `no_probability_recorded`, `refused_transposed_read`.

Rebecca's classes 1 and 2 — the read was wrong on the merits, versus the case
held and the game did not — are **not separable from a scoreline**. Separating
them needs game script and decisive scoring events, and nothing in this
pipeline records either. So a row with both a probability and a final is
labelled undecided and carries which way the read leaned and what happened as
evidence beside the label, never as a classification. A short table that means
something beats a complete one that does not.

`refused_transposed_read` is the side join made explicitly. Probabilities
descend from the ESPN scoreboard and outcomes from StatsAPI; a transposed read
otherwise produces a perfectly clean row scoring one club's handicap against
the other club's result, with no trace. The detection is the dataset builder's
own, consulted here rather than re-derived.

## Dedup

**One policy, and it is printed in the report.** Byte-identical copies of a
date's schedule across roots collapse to one. A date whose roots hold
*different* schedules is **excluded and named**, never merged or preferred.

Refusal, not repair. Choosing between two disagreeing captures of the same
slate is exactly the decision the replay could not make on 2026-08-22, where
the two cards' Polymarket asks differed by up to nine and a half points and
preferring either root produced a different answer with equal confidence.

Open finding #4 on the replay was two dedup policies in one report, neither of
them stated. This lane has one and states it.

## What this lane does not have

`polymarket_bbo`, `polymarket_mid`, and `polymarket_traded` are
`never_captured` on every row. Every price in the deployed pipeline is an ask
(`current_ask`, `slate_ask`, `ask_at_recheck`) or an executed fill
(`entry_price`, `settlement_price`); there is no bid, no midpoint, and no
last-trade field in any script. The fills *are* traded prices, but they exist
only for picks we actually made — the selection-biased population this whole
effort exists to escape.

Capturing them means a new order-book fetch inside the slate run, which is a
runtime behaviour change and outside a read-only lane. Rebecca ruled it out of
this slice on 2026-09-01 (D1); it wants its own scoped slice with its own
review. It is not a small item — the Polymarket two-sided ask sum measured 1.005
over the 2026-08 window, and half that spread is the same order of magnitude as
the entire measured edge distribution.

The fields stay on the row rather than being omitted, so the gap is visible
instead of absent, and they are never approximated from the ask.

## Population

**Recorder rows only** (Rebecca, D2, 2026-09-01). The 2026-08-11..08-31 replay
reconstructed its inputs from slate prose and its entire graded set is
`faithful_inputs: false`. Folding a prose-reconstructed row and a recorded row
into one table is the fidelity blend that was blocker 1 on that very PR wearing
a different hat. The replay is history for this lane, not population.

The first recorder output lands with the 2026-09-01 10:30 CT slate. Until then
this lane reports zero rows with every date named — both the dates with no
schedule file at all and, separately, the dates whose schedule carried no
usable denominator — which is the correct output,
not a failure, and the report says so rather than printing an empty table that
reads like a clean bill of health.
