# Bounded archive recovery: result and next step

**Recovery found useful additional evidence, but did not establish a scored
three-family cohort. Stop historical scoring and propose prospective shadow
collection.** This does not mean performance is zero, or that historical
scoring is impossible from every archive that might exist.

Jerry authorized a maximum one-hour read-only search. Search started
2026-09-07 19:59:03 UTC and stopped at approximately 20:05 UTC after the
identified offline roots were exhausted. Report preparation and verification
followed. No runtime access, network data acquisition, purchases, model fitting,
prediction backfilling, deployment, policy changes, or performance scoring
occurred during this pass.

Admission rules were frozen before archive-content inspection in the local
`RESEARCH/MLB_MODEL_AUDIT_2026_09_07/RECOVERY_RULES.md`. Its digest and the
inventory digest are in the [recovery summary](mlb-model-audit-recovery-2026-09-07.json).
An original prediction requires a corroborated game/side, numeric probability,
model identity, independently timestamped evidence binding prediction bytes
before first pitch, and a verified final. Retrospective reconstruction needs
recoverable as-of inputs and a separately frozen modeling/training protocol;
it must not be represented as an original pregame prediction. No such model
was built here. CLV and realized performance have separate evidence gates.

## Search boundary and retained inventory

The JSON summary enumerates **15 local roots, 2,753 file instances and 1,314
unique file digests**, with zero read errors. These are file counts, not game
counts. Duplicate copies are not independent evidence. Roots include:

- `.scratch/claude-{attrib-corpus,devcorpus-20260831,freshcorpus-20260831}`;
- `.scratch/claude-{drought-vps,mlb-audit,replay21,rootcause-before,nearmiss}`;
- `.scratch/claude-dryrun-0902` and the `snapshot` directory only under
  `.scratch/claude-dryrun-2026-09-02`;
- `.scratch/claude-{selhandoff,wii-probe,obs}`;
- `.scratch/builder-mlb-model-audit/live`, an **already-copied local archive**,
  despite its directory name; no refresh from the live machine;
- `RESEARCH/DROUGHT_COUNTERFACTUAL_2026-09-01`, which contains Python analysis
  scripts, not additional non-Python input files.

Discovery used workspace filename inventories and existing dry-run/audit
references. All regular non-Python files in those roots were hashed, including
`.bak`, pre-rerun variants, and completion markers. Symlinks were not followed;
none were encountered in the selected roots. JSON and JSON-backup objects were
inspected recursively for probability, model, capture and hash fields; named
scan, result, ledger and receipt schemas were then checked directly. This is
not an automatic proof that arbitrary prose contains no relevant information.

Additional exclusions: detached code/test worktrees and build caches are not
historical inputs. The two named sports repository roots have no `.picks`
directory. The known local `archives/engrams-*` directories were inspected at
the filename level and contain memory exports; they were not treated as
prediction archives. Other machines, cloud storage, unlisted directories and
provider accounts were not searched. Missing-evidence claims below are scoped
to this pass, not all possible storage.

The private owner-accessible evidence bundle is retained at
`~/.buzz/RESEARCH/MLB_MODEL_AUDIT_2026_09_07/RECOVERY/`:
`manifest.json` maps original relative paths to SHA-256 and size, and `blobs/`
holds the corresponding bytes by digest. `key-census.json` records schema
discovery locations. Raw trading history, order receipts and message exports
remain outside the repository. The committed summary is aggregate metadata.

## Recovered evidence and limits

| Evidence | Recovered | What it supports | What it does not establish |
|---|---|---|---|
| Official-shaped MLB result cache | 99 date files, 1,302 game occurrences, 1,287 unique game IDs; 1,274 IDs have a Final-status occurrence | Potential outcomes and prior-game results for a future reconstructed team-strength experiment | A full season, authenticated pregame features, or any new predictive model |
| Historical pick ledger | 45 settled rows; eight have both numeric win_probability and dk_fair_prob, dated Jul 26–Aug 10 | Candidate selected-pick cohort for further provenance work | A whole-slate sample; raw/conservative/model-version contract; immutable prediction-time proof |
| Order/proposal receipts | 463 JSON files in the canonical fresh corpus; all eight paired ledger rows resolve at least one local receipt link | Potential order/fill evidence for a separately reconciled execution audit | Proof that the ledger probability existed at execution time, closing prices or computed ROI |
| Sep 2 scan | 15 games; nine have two-sided moneylines and fair probabilities; starter/form/bullpen/park fields present with some missing starter stats | One recoverable historical input snapshot | Prediction capture time, an independent model, or the missing Sep 4–7 payloads |

The canonical cache is
`.scratch/claude-freshcorpus-20260831/audit-results/`, with filename dates
May 19–Aug 30. Its `_audit_fetched_at_utc` values span
2026-08-30T19:42:12Z–19:42:36Z. These are later-fetched outcomes, which are
appropriate as outcome candidates but cannot be reused as earlier feature
observations. Counts above deduplicate `gamePk` only; they do not claim to have
reconciled all conflicting/canceled/rescheduled occurrences or their sides.
They also provide no outcomes for the September 2 scan or September 3–7 reads.

The eight ledger rows all carry `mlb_game_pk` and local receipt references.
None of the 45 ledger rows has top-level `raw_probability`,
`conservative_probability`, or `model_version`. Recursive receipt inspection
found none of the fields `win_probability`, `dk_fair_prob`, `raw_probability`,
`conservative_probability`, `model_version`, or `probability_components`.
The receipts record orders, not a byte-binding receipt for the ledger's
probability. Local creation/execution timestamps alone do not fill that gap.
We did not calculate returns or independently certify fills/settlements.

The Sep 2 scan SHA-256 is
`a719e91d1611d96f5f28e4ff719042c4e8f46dc33fbdad9140f16475c82c6a54`.
Nine copies have those same bytes. The associated dry-run report names the
hash and records zero game reads in that day's schedule. The scan rows have
scheduled game times, but no capture timestamp. The report's own
`produced_at_utc` is the incomplete string `2026-09-03T03:2x`, which is not a
usable timestamp. A later dry-run report is not an original pregame receipt.

**None of the 2,753 inventoried files matches any of the four scan hashes
recorded in the Sep 4–7 schedules.** Exact target hashes and match counts are
in the JSON summary. This does not exclude a differently serialized payload;
such a payload would still need its identity and provenance established.

## Excluded lookalikes

- `.scratch/claude-wii-probe` contains a one-game Sep 1 fixture with a padded
  event ID in its scan, swapped short team labels in its schedule, and a raw
  pair summing to 1.01. It is not admitted as a historical prediction.
- `claude-selhandoff/root/.picks/tmp/*slate-draft.json` is a generated skeleton;
  its retained `skel.log` explicitly says it wrote 15 stubs. Sandbox and probe
  outputs are not newly recovered producer predictions.
- Model-command and model-run fields found in the sports corpora refer to
  international soccer schedules. They do not demonstrate an MLB independent
  team or pitcher model.
- Two inspected Vig-channel exports carry content/event IDs/timestamps but
  omit signatures. Their text may guide an investigation, but those exports
  alone do not authenticate a probability artifact or its pregame existence.

## Admission and missing-source matrix

| Family/metric | Established admissible games | Missing evidence after recovery |
|---|---:|---|
| Market/de-vig predictive scoring | 0 | A fully joined probability/price record with independently established pregame capture, model definition and corroborated final |
| Independent team-strength scoring | 0 | Reproducible market-free estimator/predictions and training cutoff; as-of feature lineage and joined evaluation cohort |
| Pitcher/bullpen/context scoring | 0 | Reproducible estimator and cutoff; as-of starter/workload, bullpen availability and context coverage joined to predictions/finals |
| Three-family shared-game intersection | **0** | No game was established as admitted to all three families |
| CLV | not computed | A verified comparable closing quote and observation time joined to the prediction/entry |
| Realized performance | not computed | Reconciled actual fills, costs, settlement and selection denominator; receipt/ledger presence alone is insufficient |

Zero here means **zero admissions established under the frozen rules in this
pass**. It is not a statistical conclusion and not a claim that a future
expanded search could admit no games. The recovered result history is useful
for scoping a new retrospective team model, but does not supply the other two
families or cure missing pregame capture proof. The previous seven-file
admission result remains valid within its narrower boundary; the statement
that outcomes/receipts were absent there must not be extended to these archives.

## Proposed follow-up: prospective shadow collection

Propose a separate reviewed feature that records one immutable pregame bundle
for every scheduled game: canonical identities and start time, raw source
inputs with observed times/hashes, both sides of market odds, versioned model
outputs, training cutoff, and an independent timestamp receipt. Freeze three
estimators before collecting evaluation outcomes: de-vig market, market-free
team strength, and pitcher/bullpen/context. Record missing inputs as exclusions
for each family; do not substitute the market estimate and call it independent.

After games, join official finals with side corroboration; record comparable
closing quotes separately. Use whole-date held-out blocks and paired common
cohorts, with separate coverage reports. No bets are needed to measure Brier,
log loss or calibration. Declare sample-size and comparison criteria before
results; a few recovered games or a short winning streak is not deployment
evidence. Preserve the existing policy thresholds.

Acceptance for the first implementation slice should be capture integrity and
one complete shadow-only dry run, including late/missing-input rejection and
append-only retention. A subsequent human-approved runtime installation and
terminal producer receipt would be needed to claim collection is live.
This document proposes that work; it does not authorize or implement it.
Jerry decides whether PR #85 is rescaled to the completed admission/recovery
audit and whether this follow-up proceeds. No merge was performed.

## Reproduction

Use the private bundle and committed JSON summary. Verify the manifest digest,
then every blob's digest and size. Recompute file/unique counts and root totals
from the manifest; compare each requested scan digest against every entry.
For the result-cache census, select manifest paths under
`.scratch/claude-freshcorpus-20260831/audit-results/`, load their blobs, walk
`dates[].games[]`, count occurrences, deduplicate `gamePk`, and separately
deduplicate IDs whose `status.abstractGameState` equals `Final`.
For the ledger, load the blob at
`.scratch/claude-freshcorpus-20260831/picks.json`, enumerate `picks`, and count
the per-row conjunction of numeric `win_probability` and `dk_fair_prob`.
For the scan, load the stated digest and count the per-row conjunction of
non-null `away_ml`, `home_ml`, `away_fair`, and `home_fair`.

These are discovery counts, not an admission algorithm. The source inventory
and direct provenance inspection support the admission decisions above.
Validation of this report verifies retained bytes and aggregate recomputation;
it does not turn an audit-time hash into historical authentication.
