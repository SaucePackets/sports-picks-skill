# Vig observability and ledger reconciliation

Three questions this lane could not previously answer, and where each is now
answered on disk.

| Question | Artifact | Tool |
| --- | --- | --- |
| Did the gate run, and what did it decide? | `.picks/journal/<day>-runs.jsonl` | `scripts/vig_run_journal.py` |
| Do the performance numbers agree? | `picks.json` vs `record.json` | `scripts/vig_ledger_reconcile.py` |
| Is cron running the code we think it is? | `.deploy/runtime.marker`, cron `jobs.json` | `scripts/vig_runtime_verify.py` |

Everything here is read-only about money. Nothing in this lane creates,
previews, signs, or submits an order, and nothing reads or writes a betting
rail.

`tests/test_observability_adds_no_execution.py` checks that for **these three
modules by name**, and pins their sibling imports so the lane cannot acquire a
dependency on the execution path. It does not discover the lane's membership:
a fourth module added to `scripts/` and imported by none of the three is not
scanned, and the test will not say so. Add a module to this lane, add it
there — nothing mechanical will remind you.

## 1. The gate run journal

### What was wrong

`run_gate` wrote exactly one artifact, on exactly one path: a completed review
overwrote `.picks/latest-action.md`. Every other outcome printed to stdout and
returned. So:

* A day with no schedule file (2026-08-12, 13, 14, 19, 20) left nothing —
  indistinguishable afterwards from a day the cron never fired.
* An explicit PASS (a schedule with no candidates and no due watchlist entries)
  left nothing.
* A review the gate **rejected and rolled back** left nothing, and the rollback
  restored the schedule file to its pre-review bytes, so even the input carried
  no sign the review had run.
* A second cycle of the same day overwrote the first cycle's `latest-action.md`.

### What it is now

One append-only JSONL file per Chicago schedule day, shared by every sport.
One object per gate invocation:

```json
{"schema":"vig-run-journal-v1","recorded_at":"2026-08-19T15:30:12Z","sport":"MLB",
 "day":"2026-08-19","outcome":"no_work","stage":"no_reviewable_work",
 "detail":"schedule present with no candidates and no due watchlist entries",
 "schedule_path":"/…/.picks/execute/2026-08-19-schedule.json",
 "counts":{},"notices":[],"deferrals":[]}
```

`outcome` is one of `no_schedule`, `no_work`, `reviewed`, `error` — a closed
vocabulary, validated on construction, so a report that groups by it cannot
silently drop rows. `stage` names *where* the run stopped
(`schedule_missing`, `child_timeout`, `review_transition`, `persist`,
`complete`, …). Outcome plus stage is what makes a failure diagnosable from
the artifact alone.

`deferrals` carries every input the gate could not use, each with the **source**
that reported it (`lineup_feed` / `price_feed`), the reason, and the instant it
was observed. "Entry X was not reviewed" is unactionable; "the price feed had
no executable ask for X at 15:29Z" is the 2026-08-16 diagnosis that took a
week to reconstruct by hand.

Each item also carries a **kind**, a second closed vocabulary: `outage` means a
live input went quiet and a retry is the fix, `data_defect` means the entry
itself is wrong and no retry can help (an unresolvable Polymarket slug is the
standing example — it is deliberately *not* deferral-eligible). `format_record`
renders an outage as `deferred:` and a defect as `skipped:`, because a
permanently-broken entry printed as "deferred" tells the reader to wait for a
retry that is never coming.

### Two deliberate properties

**Append-only.** A later cycle never rewrites an earlier one's record. That is
the exact defect `latest-action.md` had.

**A journal failure never changes a verdict.** `record_run` returns an error
string rather than raising, and the gate prints
`… review gate JOURNAL CRITICAL: …` and continues with its own exit code.
Observability that can fail a review would add an outage mode to the lane whose
whole problem is losing work silently. The failure is loud; it is not fatal.

### Using it

```bash
python scripts/vig_run_journal.py --day 2026-08-19            # what ran that day
python scripts/vig_run_journal.py --since 2026-08-01 --until 2026-08-29
python scripts/vig_run_journal.py --since 2026-08-01 --until 2026-08-29 --sport MLB
```

The range form exits 1 and names every day with no gate record at all. That is
the coverage signal the lane never had: a day the cron did not fire, or fired
into a crash before the journal, is now a named gap rather than an absence
you have to infer from a missing schedule file.

`--root` defaults to the same `resolve_root()` the gate uses, imported from
`vig_review_gate_common` rather than reimplemented — two copies of one
resolution agree only until one changes.

## 2. Ledger reconciliation

### The canonical ledger

**`~/notes/Sports/picks/picks.json` is canonical.** Override with
`$VIG_PICKS_FILE`. Everything else is derived and is recomputed *from* it,
never the reverse:

* `record.json` — a denormalized counter view, recomputed by the settlement
  agent. It has gone stale before and silently disabled settlement.
* The runtime's `.picks/picks.json` and `.picks/record.json` — **symlinks** to
  the canonical files since 2026-08-23. They are not copies, and they must not
  become copies; that was the split brain.

### Two conflict classes, both checked

`scripts/vig_ledger_reconcile.py` detects:

1. **Derived-view conflict** — a `record.json` counter disagreeing with the
   same counter recomputed from `picks.json`, or `record.json` disagreeing
   with itself (`wins + losses + voids` must equal `settled`;
   `decision_count` must equal `wins + losses`). The internal checks bite even
   when the ledger is unreadable, which is exactly when the derived check
   cannot run.
2. **Split-brain conflict** — more than one path claiming to be the ledger.
   Resolution is by real path, so a symlink is correctly *not* a conflict. Two
   distinct real files are reported whether or not they currently agree:
   identical today is not the same as kept identical.

Only derivable counters are compared. `current_streak` and the free-text notes
are excluded deliberately — a checker that guesses at a field it cannot derive
produces false alarms, and one false alarm makes the whole check ignorable.
Money comparisons tolerate half a cent, because both sides store rounded
dollars.

```bash
python scripts/vig_ledger_reconcile.py
python scripts/vig_ledger_reconcile.py \
  --also-ledger ~/projects/sports-picks-runtime/.picks/picks.json
```

It runs automatically inside `vig_postgame_gate.py`: a conflict triggers a
settlement cycle and is handed to the agent with the instruction that
`picks.json` is canonical — recompute the view from it, never edit a counter
to match a report.

### The populations, which are not a conflict

Two reports quote different headline totals from the same ledger, and both are
correct:

| Report | Population | On 2026-08-30 |
| --- | --- | --- |
| `vig_calibration_report.py` | settled **and decided** (win/loss) | 44 picks, \$842.36 staked |
| `record.json` | settled, **voids included** | 45 picks, \$859.96 staked |

A void has no outcome to calibrate against, so the calibration report excludes
it. Unlabelled, that reads as a ledger conflict. The calibration report now
states its population and prints the arithmetic bridge to `record.json`
(`+ 1 void/push ($17.60) = 45 settled, $859.96 staked`), and the reconciler
uses the settled-population definition so a population difference can never be
reported as a counter conflict.

`receipts_ledger_reconcile.py` answers a different question and remains
separate: whether every filled Polymarket receipt reached the ledger at all.
This one asks whether everything reading the ledger sees the same numbers.

## 3. Runtime / cron divergence

`deploy-runtime.sh` verifies the runtime at deploy time and then stops looking.
Everything that goes wrong afterwards was invisible: the two reporting jobs sat
paused with `workdir: null` (which makes `resolve_root()` fall back to the
*developer* checkout), and the runtime checkout itself was eight merges behind
`main` on 2026-08-23 with nothing on the box saying so.

```bash
python scripts/vig_runtime_verify.py --expect-sha <deployed tip>
python scripts/vig_runtime_verify.py --strict     # warnings are failures too
```

It checks four things, each against the artifact that *defines* it rather than
a reconstruction of it:

1. **`.deploy/runtime.marker`** — the file `deploy-runtime.sh` writes into a
   checkout it created. Not "has a `.picks/`", which is *has pick state* and is
   true of a developer checkout too; that distinction cost PR #59 a review
   round. A test pins the marker path this reads to the one the deploy writes.
2. **Every cron job's `workdir`**, compared after symlink resolution. An
   enabled job on a foreign or null workdir is a failure; a paused one is a
   warning, because a paused job runs nothing but is one `cron resume` away
   from being an outage — which is how it happened.
3. **The runtime's HEAD**, optionally pinned with `--expect-sha`.
4. **A clean runtime tree.** `deploy-runtime.sh` hard-resets, so a local
   modification is both a deploy blocker and unreviewed code in production.

It writes nothing, which a test asserts by diffing the tree around a run.
