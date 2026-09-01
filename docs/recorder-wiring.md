# Wiring the recorder into the run, and closing the deployment gap

## What happened on 2026-09-01

PR #74 deployed the per-game recorder to the runtime at 2026-09-01T00:04Z. The
first slate to run after it — cron `c9452052719c`, 10:31:43 CDT — produced:

- `.picks/slate/2026-09-01.md`, 163 lines, a full read on **15** games;
- `.picks/execute/2026-09-01-schedule.json` with keys `date`, `sport`,
  `market_type`, `candidates`, `lineup_watchlist` — **no `game_reads`, no
  `slate_denominator`**;
- `latest-action.md`: "Slate complete. 0 candidates pending Vig review";
- a run journal recording `no_work` / `no_reviewable_work`, the same record a
  day with no card produces;
- cron fields reading `Last run: ok` and `Execution: failed` together.

Fifteen games were handicapped and refused. Zero rows were recorded. Every
signal available said the run succeeded.

The validator that would have caught it works correctly and was never run:
`SKILL.md` and `references/mlb.md` said to run it "before reporting slate
success", which is a sentence, not a rail. Its `--denominator` cross-check
could not have run either — the flag wants `mlb_stage2_scan`'s output, nothing
ever told the scan to persist that output, and the newest scan artifact on the
runtime was `stage2-2026-08-14.json`, three weeks stale while the slate ran
daily.

This is the same defect in four places: a check that exists, reads correctly,
and is never reached.

## What changed

**The denominator is an artifact, not a shell variable.**
`mlb_stage2_scan.py` writes its roster to `.picks/tmp/stage2-<date>.json` on
every run, and `mlb_game_reads.py --validate` resolves that path from the
schedule's date. Both sides derive it from one function
(`conventional_denominator_path`), so they cannot drift into looking for each
other in different directories. A **missing** scan is an error: "nobody ran the
scan" and "the scan agrees" no longer share an exit code.

**The scheduled run notices — including the trimmed case.**
`vig_review_gate_common.run_gate` validates the day's per-game record on every
cycle, **against the scan as well as against the schedule**
(`mlb_game_reads.validate_with_denominator`), and journals stage
`recorder_missing` instead of `no_reviewable_work` when it fails. The scan half
is not optional here either: a run that trimmed `game_reads` and
`slate_denominator` to the same short set agrees with itself, passes every
check keyed on the schedule alone, and would otherwise journal a clean
`no_reviewable_work` — the identical record the 09-01 run produced, reached by
a different route. The gate reaches the cross-check through `mlb_game_reads`,
which is in `deploy-runtime.sh`'s `PROFILE_MANIFEST`; `mlb_slate_receipt.py` is
not, so a gate importing the receipt would pass every test here and
`ImportError` on the runtime's profile-local copies. The notice prints once per
day,
not on all ninety-six cycles — a repeating alarm is the same as no alarm, which
this lane already paid to learn with the stuck watchlist entry. The gate's exit
code is deliberately unchanged: a defect in a measurement artifact must not
take the reviewer offline.

**`mlb_slate_receipt.py` is the deterministic receipt.** Both cron fields are
Hermes', not ours; the discrepancy between them cannot be fixed on this side of
the boundary, but the dependence on them can. The receipt is written by
repository code, next to the run journal, on a closed vocabulary:

| verdict | meaning |
|---|---|
| `complete` | every scheduled game has a read and the record validates |
| `honest_zero` | the scan enumerated no games; zero reads is correct |
| `recorder_failed` | games were scheduled and the record does not cover them |
| `no_schedule` | no schedule file for the day |

`honest_zero` and `recorder_failed` never share an exit code. The count that
decides between them comes from the **scan**, never from the schedule's own
copy of the denominator: a run that trimmed both together is perfectly
self-consistent and would otherwise certify itself `complete`.

An unreadable scan yields `recorder_failed`, not `honest_zero`. A day whose
size we could not establish tells us nothing about whether zero was the right
answer, and treating "unknown" as "empty" is precisely how a recorder failure
would be laundered into an honest one.

## The producer half: landing the schedule through code

Everything above makes the *checks* reachable. None of it changes who writes the
record. The schedule was authored by the run, from a prompt, and every rail sat
downstream of the write — so the strongest thing the repository could say about
a bare schedule was that it had already happened.

`scripts/mlb_slate_writer.py` is the other half. A schedule lands through it or
it is not a supported artifact:

- **The denominator is derived, never transcribed.** It is built from
  `.picks/tmp/stage2-<date>.json` and a draft carrying its own
  `slate_denominator` is **refused** — not overwritten, and not accepted when it
  happens to agree. "Accept it if it matches" is how transcription survives: the
  day it stops matching is the day nobody is checking. `fetched_at_utc` comes
  from the artifact's mtime, so it is a fact about the file the roster came from
  rather than a claim the writeup made about a scan it could not see.
- **One validated read per scheduled game, before anything is written.** The
  composed record goes through `mlb_game_reads.validate_with_denominator` and
  `mlb_lineup_watchlist.validate_watchlist` — the functions the gate and the
  receipt already call, consulted rather than re-derived. A landing check that is
  a second opinion is worse than none, because a record it accepts and the gate
  rejects is a record nobody expected to be wrong. The write is atomic, so a
  refusal leaves the previous schedule byte-identical rather than truncated.
- **`--skeleton` moves the enumeration out of the prompt.** It emits one
  `game_reads` stub per scanned game with both id spaces, the team names and the
  DK fair prior the scan already computed. Enumerating the slate and copying ids
  across two id spaces was the producer's job and the part that silently went
  missing; it is now a file. The stub names no disposition and no prices, so an
  unfilled skeleton cannot land — a head start, not a bypass.
- **Landing never clobbers a decision.** A schedule whose candidates carry
  `vig_approved`, `vig_notes`, `execution_status` or `executed`, or whose
  watchlist entries have been rechecked, is refused. There is no override flag,
  because an optional rail is the shape of defect this lane keeps paying for.

**What it does not close, stated plainly.** Nothing here stops a run from
writing `.picks/execute/<date>-schedule.json` by hand and skipping the writer.
That case is not left open — it is exactly what the postflight receipt and the
scheduled gate catch, and both are deliberately unchanged by this work, with a
regression pinning that a hand-written bare schedule still reads
`recorder_failed`. The claim here is the narrower one: an incomplete record can
no longer *land* through the supported path, and the supported path is now the
easier one.

## The deployment gap, and why it pointed the other way

`load_model_deployment_policy` existed and was consulted in exactly one
non-test place — `mlb_probability_model.py` — which is an offline analysis
path. The execution boundary never asked it anything.
`mlb_runtime_policy.stale_probability_field_errors` required `model_version` to
be a non-empty string, against no allowlist and against no deployment record.

So the money boundary failed **open** in the direction the versioned deployment
gate exists to close: a candidate carrying an invented, experimental or retired
non-market version would have passed every check, because "there is a version
string" was standing in for "a model was deployed". The repository's own test
fixtures used `market-only-fallback-v1`, `test-model-v1` and `market-prior-v1`
— three invented strings, none of them the real `vig-mlb-market-v1` — and all
three passed. That is the gap measured in its own tests.

`model_deployment_errors` now refuses any version that is neither the
market-only fallback nor listed in a `vig-mlb-deployed-models-v1` record in
`risk_limits.json`. It is called from `mlb_execution_gate.candidate_is_eligible`
and again at the final lock in `execution_guard._risk_limit_violation`.

The market-only fallback is exempt on the stated ground that it **asserts no
model**: it sets our probability to the book's de-vigged fair price at a zero
haircut. Since no deployment record exists on the runtime, market-only is the
only executable version today — which is exactly the behaviour that already
ran, with the difference that anything else is now refused rather than
accepted by default. **No live candidate changes disposition because of this.**

## Making the substitution visible

`mlb_probability_chain_report.py` is read-only and walks
`dk_fair -> raw -> haircut -> conservative -> ask -> edge` per recorded read,
classifying each by what the numbers do rather than what the label claims:

- `market_substitution` — raw equals dk_fair on both sides at a zero haircut.
  Our probability **is** DraftKings'.
- `independent_handicap` — raw departs from dk_fair on at least one side.
- `unhandicapped` — no model trail (a `not_priced` game).
- `indeterminate` — a trail exists but a needed field is missing. Never folded
  into either of the first two.

Classifying by `model_version` would answer a weaker question. A read tagged
`vig-mlb-market-v1` whose raw probability differs from dk_fair is not a
market-only read, and one tagged otherwise whose numbers are exactly dk_fair
is not an independent one. Both are counted as label/number mismatches and
named, rather than resolved in favour of either.

The report consults `mlb_runtime_policy.model_deployment_errors` — the same
function the money gates call — so it cannot drift into a second opinion about
what is executable.

## What this does not do

No threshold moved. `min_conservative_edge` stays at 0.05. No model was
deployed, no handicap was promoted, no gate logic changed, no execution
behaviour changed, and nothing here is on the network. The 13-row Brier
comparison from the replay is direction, not evidence, and nothing in this
change rests on it.

## Still open

The slate's market-only behaviour remains **prompt-level**, not code-level:
nothing mechanically forces `raw_probability == dk_fair_prob`. The gate agent
applies a prompt norm ("when in doubt, use the market-only fallback") backed by
a deployment gate that has never been both answerable and passing. Resolving
that either way is a behaviour change and out of scope for this lane; it is
recorded as an open question in `docs/model-evaluation.md`.
