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
which is in `deploy-runtime.sh`'s `PROFILE_MANIFEST`. (`mlb_slate_receipt.py`
was not in that manifest when this was written, which is why the gate went
through `mlb_game_reads`; it was added on 2026-09-03, and the gate now imports
the receipt directly — see *Who writes the receipt* below.) The notice prints
once per day,
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
- **Landing never clobbers a decision, and never invents one.** A schedule whose
  candidates carry `vig_approved`, `vig_notes`, `execution_status` or `executed`
  is refused as the target of a landing, and a *draft* carrying any of them is
  refused as the source of one. The second half matters because the executor
  reads `vig_approved` straight off the schedule and the review queue holds only
  candidates whose value is not yet a bool, so a producer-authored `true` would
  reach the executor having never been reviewed. Both sites ask one predicate,
  `decision_fields`, of two different records — the question "does this card
  carry a decision" has one answer and two copies of it would drift. The rule is
  presence of a *value*, not of a key: the producer's own template spells all
  four out as `null`/`false`, so a key-presence test would refuse every slate.
  `execution_mode` is not on the list — `standing_authorized` says what may
  happen after a review, not that one happened. Watchlist entries that have been
  rechecked are refused on the same principle. There is no override flag,
  because an optional rail is the shape of defect this lane keeps paying for.

**What it does not close, stated plainly.** Nothing here stops a run from
writing `.picks/execute/<date>-schedule.json` by hand and skipping the writer.
That case is not left open — it is exactly what the postflight receipt and the
scheduled gate catch, and both are deliberately unchanged by this work, with a
regression pinning that a hand-written bare schedule still reads
`recorder_failed`. The claim here is the narrower one: an incomplete record can
no longer *land* through the supported path, and the supported path is now the
easier one.

## What happened on 2026-09-04

The writer was deployed, present in the runtime checkout and documented in the
deployed `SKILL.md` at lines 78 and 87. The slate run did not use it. It
hand-wrote a 1353-byte schedule with `candidates: []`, one watchlist entry, **no
`game_reads` and no `slate_denominator`** — the 09-01 shape again, by the route
the writer explicitly does not close.

Everything downstream worked. `mlb_stage2_scan.py` had persisted
`.picks/tmp/stage2-2026-09-04.json` with **16** entries, 16 distinct `game_pk`
and 16 distinct `event_id` (including a legitimate Detroit–Cleveland
doubleheader). The gate cross-checked against it on every cycle and journalled
`recorder_failed` seven times, listing exactly 16 `scan lists game N but
slate_denominator does not` errors.

And there was **no receipt on disk at all**, because
`mlb_slate_receipt.py --write` was another sentence in `SKILL.md` that the same
run skipped. The one artifact designed to carry the day's verdict in a closed
vocabulary depended on the cooperation of the component whose failure it exists
to catch.

Three numbers described one slate and none of them reconciled: the writeup's
prose opened with "Fourteen games scanned" and then discussed 16, the scan said
16, and the first report of the incident said 18. Nothing in the repository
tied the count in the prose to the count in the record.

## Who writes the receipt

`vig_review_gate_common.run_gate` writes `.picks/journal/<date>-slate-receipt.json`
on **every MLB cycle**, from `mlb_slate_receipt.build_receipt`. This is the
natural owner: scheduled code with no agent in it, already running the identical
validation every fifteen minutes, and running whether or not a slate was
produced. Running `mlb_slate_receipt.py --write` by hand is still supported and
is still what the slate summary should quote; it is no longer what the receipt's
*existence* depends on.

Three properties, each pinned by a test:

- It is written in a `finally`, so a receipt exists for the paths that return
  early (no schedule at all) and for the ones that raise. A receipt written only
  on the happy path would reproduce the 09-04 failure in a narrower form.
- It is not a second opinion. `build_receipt` calls the same `mlb_game_reads`
  functions the gate calls, so the journal and the receipt cannot answer
  differently about one file.
- A receipt that cannot be written prints `RECEIPT CRITICAL` and is swallowed —
  the same asymmetry as the run journal. Failing a review because a measurement
  artifact could not be persisted would add an outage mode to the lane whose
  actual problem is losing work silently.

It writes silently on success, `recorder_failed` included. The gap already has
exactly one stdout notification per day from `recorder_gap_notice`, and a second
line on each of ninety-six cycles — on the same stdout the reviewer's approval
card is written to — is the alarm-fatigue failure this lane has already paid
for once.

Rewritten each cycle rather than once: the day's record changes as a schedule
lands and reads get filled, so the last cycle is the one that counts and
`recorded_at_utc` says which cycle wrote it.

## Writer provenance — diagnosis, not a rail

`slate_denominator` now carries `scan_sha256`, the digest of the scan bytes the
landing derived the roster from, taken from the same bytes that were parsed
rather than from a second read of the path. The receipt re-hashes the scan still
on disk and reports one of four values beside the verdict:

| `writer_provenance` | meaning |
|---|---|
| `absent` | no digest recorded; the schedule did not come through the writer |
| `corroborated` | the recorded digest equals the scan's |
| `contradicted` | a digest is recorded and does not match, or is not a string |
| `unverifiable` | a digest is recorded and the scan could not be hashed |

**This is diagnosis and it is not enforcement.** A hand-authored schedule can
copy a correct digest exactly as easily as a correct roster, so `corroborated`
is evidence of a consistent record and not proof of a code path. The
load-bearing value is `absent`, which is the one a bypass cannot avoid without
going to the trouble of forging a digest — and even that only *suggests* the
bypass, it does not prove one.

Accordingly `writer_provenance` is **not an input to the verdict**, and a test
pins that: strip the digest or corrupt it, and the verdict, the error list and
the scheduled-game count are identical. Content validation against the scan
stays the enforcement. A guard keyed on a field the producer writes is a guard
measuring the producer's copy of itself, and this lane has paid for that lesson
already.

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

**Nothing forces the producer through the writer.** That is unchanged by the
09-04 work and stated here so nobody reads the receipt's new owner as a rail it
is not. What changed is that the bypass can no longer also suppress the
evidence: the gate writes the verdict, and `writer_provenance: absent` names the
bypass on the receipt. Both are detection, after the fact. Structural forcing —
making the schedule unwritable except through the writer — would be a behaviour
change and belongs in its own lane.

**The count in the prose is not tied to the count in the record.** On 09-04 the
writeup said "Fourteen" over a 16-game scan. `SKILL.md` now instructs the run to
quote the receipt's `scheduled_games` rather than count by hand, and that is
*guidance only*: no code reads `.picks/slate/<date>.md`, so nothing enforces it.
Enforcing it means parsing slate prose, which is its own
lane.
