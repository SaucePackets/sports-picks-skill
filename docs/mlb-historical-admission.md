# Retained historical evidence: admission checkpoint v1

The bounded importer verifies retained integrity and imports outcome candidates,
but admits **zero** historical games. It does not fit or score a model. The
[versioned contract](mlb-historical-admission-contract-v1.json) and
[reproducible checkpoint](mlb-historical-admission-2026-09-07.json) define this
slice. The synthetic benchmark remains synthetic-only and is not called.

The adapter is deliberately specific to the September 7 recovery manifest.
It is not a general historical dataset validator: a different source, a new
receipt verifier, or an admission path requires a separately reviewed adapter.
No boolean supplied by a caller can establish source authenticity. All eight
required source gates remain unavailable for this bundle. Exit 1 is a verified
but infeasible checkpoint; exit 2 is an integrity/schema/contract failure.
There is no successful scoring exit or runtime installation.

## Reproduction and source boundary

From this repository, with the owner-retained recovery directory available:

```sh
python3 scripts/mlb_historical_admission.py \
  --bundle /path/to/MLB_MODEL_AUDIT_2026_09_07/RECOVERY \
  --recovery-rules /path/to/MLB_MODEL_AUDIT_2026_09_07/RECOVERY_RULES.md
```

The CLI emits deterministic JSON on stdout and writes nothing. It reads only
its committed contract, its implementation bytes, the explicitly supplied frozen
rules, the pinned manifest and the manifest's digest-named blobs. It does not
open original inventory paths, follow blob symlinks, enumerate archives, fetch
provider data, inspect runtime state or load model/deployment policy. The bundle
and its parent path should be owner-controlled and stationary during validation.
A changed manifest, missing/corrupt blob, duplicate path/key, invalid root/size,
skipped/error inventory or census mismatch aborts. Duplicate copies count as
file instances, not independent evidence.

The reference is the retained audit worktree's
`docs/mlb-model-audit-recovery-2026-09-07.{md,json}` at audit tip `f8df6ab`,
with its reproduction recipe. The manifest and original frozen-rules SHA-256
are pinned in both the contract and adapter. Recovery-file counts and missing
scan hashes are independently recomputed; those expected values cannot replace
reading and checking the bytes. Source JSON pointers and digests on normalized
outcomes and refused scan rows preserve lineage. The output binds its contract,
implementation and canonical normalized outcome candidates by hash. Raw ledgers,
receipts and private inventory paths are not copied into the repository.

## Evidence established and refused

All 2,753 file instances and 1,314 distinct blobs verify. The census reproduces
99 canonical result-cache files, 1,302 game occurrences, 1,287 unique game IDs,
and 1,274 IDs with a Final occurrence. These are outcome candidates, not a
complete schedule or a coherent training table. Fifteen IDs have conflicting
team identities or scheduled starts across occurrences; fourteen have conflicting
Final scores. No occurrence is silently selected to repair those conflicts.
The source normalization uses numeric MLB game and team IDs, preserves source
pointers, and deliberately omits standings, form, pitcher and other posterior
fields. Repeated IDs need corroboration of both sides and timing; IDs alone do
not authenticate a game. Status/score conflicts and schedule corrections need
source-specific reconciliation before any training or outcome admission.

The ledger has 45 selected rows, eight with both numeric probabilities, and zero
model-version fields. The 463 receipt JSON files have none of the recursively
searched probability/model fields listed in `PROBABILITY_FIELDS`. This confirms
the reported schema finding; it is not a proof about every possible prose claim.
All ledger rows lack established probability byte-binding provenance and remain
refused. They cannot supply a whole-schedule denominator.

The September 2 scan has 15 distinct game IDs. Nine rows contain both raw prices
and fair probabilities; the adapter recognizes this archive's signed American
price strings. Six lack two-sided prices. None establishes an authenticated
same-book pregame observation, stable numeric team IDs, actual first pitch or
a joined verified final. All 15 rows remain visible with explicit, nonexclusive
refusal reasons. Candidate raw-price coverage is 9/15, while **complete-schedule
coverage is unknown**, not 0% or 100%.

The four September 4–7 scan hashes are corroborated against their retained
schedule records and absent from every manifest entry. The checkpoint preserves
those schedules' supplied game counts and records every row as refused for
missing scan bytes; it does not claim those schedules are complete. September 3
has no scan selected by this bounded recipe and is listed separately, without
asserting absence from every archive. No synthetic fixture or skeleton is imported.

## Time, split and missing admission gates

The frozen split is a proposed **retrospective feasibility** window: May 19–August
30 training dates, cutoff August 31 00:00 UTC, and September 2–7 held-out dates.
It is declared before this checkpoint/scoring, after outcomes already exist.
It is not evidence of a prospectively predeclared study and does not authorize
scoring. The CLI rejects overlapping date blocks, mixed seasons and ambiguous
cutoff timestamps. Evaluation game IDs overlapping candidate training IDs get
an additional refusal; no ratings or parameters are trained at all.

Future admission requires source version/authentication, a complete schedule
covering every date including zero-game days and corrections, corroborated team
identities, and actual first-pitch/completion semantics. Training must establish
completion and information availability strictly before cutoff; later-fetched
finals may be outcome evidence, but retrieval is not completion time or proof
that posterior features were available earlier. The archive's August 30 retrieval
stamps therefore cannot, by themselves, pass the as-of training gate.

Original predictions additionally need independent pregame receipts binding
exact prediction bytes and model version. Retrospective reconstructed models
need independently supported as-of inputs and a frozen training specification;
they must never be called original predictions. Market admission needs both
sides from the same identified book, the same full-game moneyline rules and
observation, with authentic pregame availability. Merely having two prices or
asserting a book name does not pass. Finals need identity and side corroboration
and conflict resolution. Missing values stay null/unavailable.

Refusal counts use named populations: unique outcome-candidate IDs, selected
ledger rows, scan rows and per-date supplied schedule rows. Reason counts are
nonexclusive and must not be summed into a game denominator. The populations
are not added together. Historical scores remain null, admitted IDs empty,
`feasible: false`, `scoring_enabled: false` and `eligible: false`.

## Validation

The tool uses only the Python standard library. Tests exercise corrupted/missing
blobs, duplicate evidence, manifest traversal/symlinks, census inconsistencies,
JSON ambiguity, signed-price parsing, strict IDs, split leakage and refusal of
synthetic bundles or a scoring switch. Those generated inputs are integrity
fixtures only. Run the full package suite (including its pytest tests):

```sh
python3 -m pip install pytest -r scripts/requirements-shadow.txt
python3 -m pytest -q
```

The retained-bundle checkpoint is a separate real-artifact validation, not a
synthetic unit-test result. Repeated runs must produce identical bytes. This
slice does not implement pitcher/context models, qualification/review stages,
execution, bets, runtime collection, deployment, or any policy change. Jerry
alone merges; new source acquisition and scoring remain separate work.
