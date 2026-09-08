# Real-source shadow refusal contract v1

`scripts/mlb_real_shadow_capture.py` ingests a bounded retained source bundle
offline. It produces a refusal census, not admitted predictions or a completed
trusted capture. It does not fetch URLs, write files, read credentials, call the
fixture verifier, calculate probabilities, score models, or integrate with runtime.
The existing fixture collector and historical admission checkpoint are unchanged.

## Run and exit contract

```sh
python3 scripts/mlb_real_shadow_capture.py --bundle /path/to/retained-bundle
```

Exit **1** means a refusal report was produced, including an unknown denominator
if schedule evidence is unavailable or invalid. Exit **2** means bundle integrity
or manifest schema failed. There is no successful admission exit. Every output
has `eligible: false`; reports also have `dry_run_pass: false`,
`scoring_enabled: false`, and no admitted game IDs. Tests use synthetic schema
inputs only; passing them does not qualify a real row.

## Retained input interface

A bundle is an owner-controlled stationary directory containing `manifest.json`
and `objects/<sha256>` files holding exact original response or receipt bytes.
Do not rewrite an earlier manifest to insert later captures: use a separate bundle
for each attempt, retaining previous bundles. The command is read-only and checks
every referenced byte digest before parsing, even for unsupported or unjoined
sources. It rejects symlinked manifests/objects, digest traversal, JSON duplicate
keys, and nonfinite JSON. This is integrity checking, not WORM storage or proof of
the manifest's completeness. Parent directories and the host remain trusted for
storage access; administrator rewrites are outside this contract.

The manifest has exactly `schema` (integer 1), `slate_date` (YYYY-MM-DD), and
`sources` (array). Each source has exactly these fields:

| Field | Contract |
| --- | --- |
| `kind` | `schedule`, `markets`, `final`, or `timestamp` |
| `game_id` | Numeric MLB ID encoded as a canonical string for finals; null otherwise |
| `source_id`, `version`, `url` | Nonempty source labels; these never authenticate a provider |
| `body_sha256` | Lowercase SHA-256 of original bytes, or null for an acquisition failure |
| `retrieved_at_local` | Nullable timezone-aware diagnostic time; never trusted evidence |
| `failure` | Null with bytes; otherwise `not_acquired`, `access_unavailable`, or `fetch_failed` |

Exactly one schedule, market, and timestamp slot is required, so unconfigured
sources must be explicit. At most one final source per game is accepted in one
manifest; repeated attempts belong in separate retained bundles. Unknown trust
flags/extra fields are rejected. A timestamp slot can retain arbitrary original
receipt bytes, but all such receipts remain **unverified**, including fixture
signatures and any receipt claiming to be valid. There is no implemented real
timestamp verifier, configurable trust switch, or accepted test key.

## Supported source shapes and limits

Schedule: `mlb_statsapi`, version `v1`, exact URL
`https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD&hydrate=linescore`.
The parser checks date blocks, total counts, unique numeric game IDs, distinct
numeric away/home team IDs and scheduled starts. The denominator is the **supplied
source census**, not proven complete MLB coverage. Missing or invalid schedule
evidence means unknown denominator, not zero. A valid empty source census can
report zero, but still cannot establish completeness. A malformed identity or
duplicate game invalidates the census instead of silently dropping that game.

Markets: `espn_scoreboard`, version `v2`, exact URL
`https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates=YYYYMMDD&limit=100`.
Only a unique exact away/home-name and scheduled-time match is exposed as a
**candidate**, with its original JSON pointer and event ID. Missing, duplicate,
malformed and mismatched matches remain refused. Event IDs must be nonempty,
unpadded strings; malformed IDs refuse the source. This is not corroborated numeric
ID equivalence across providers. Raw odds bytes are retained; this slice does not
interpret `close` as a pregame quote, authenticate same-book market rules, or emit
prices/probabilities. `odds_present` is presence only, never usable-price coverage.

Finals: `mlb_statsapi`, version `v1.1`, exact URL
`https://statsapi.mlb.com/api/v1.1/game/<game_id>/feed/live`.
Both game ID locations, both team IDs, scheduled start and official date must
agree with the schedule. Both sources must say Final, and their nonnegative integer
scores must agree without a tie. Structural corroboration is reported separately
from final authentication. Source conflicts never resolve by choosing a favorite.
Finals absent for unfinished games stay unavailable; the importer does not wait
or fetch them. Both documents are from the same provider, not independent witnesses.

Local retrieval time, HTTP Date, source metadata, final status and a purported
timestamp are not pregame proof. `capture_late` and actual first pitch stay null:
an unproven boundary is not proven lateness. Every row carries nonexclusive reasons
for missing provider authentication, complete-slate provenance, pregame/as-of
semantics, independent timestamp trust, usable same-book market evidence and any
missing/invalid source. No first-refusal short circuit erases other evidence.

## Real-artifact checkpoint

On September 7, 2026, manual HTTPS acquisition retained one MLB schedule response,
one ESPN scoreboard response and eight game feeds for games marked Final in that
schedule. The schedule census contains 11 games. The offline report preserves all
11 as refused; eight finals structurally agree, three are unavailable, and all 11
market matches are candidates only. No independent capture receipt was acquired.
This is a real-byte ingestion/refusal exercise, **not a passed trusted dry run**.

The machine-readable checkpoint records source/manifest/report hashes in
`mlb-real-shadow-checkpoint-2026-09-07.json`. Raw bytes stay in the owner-retained
bundle; re-fetching these changing endpoints is not reproduction of that snapshot.
Reproduce by running the command against the retained bundle and comparing the
report digest, not by substituting current API responses.

The next admission slice still needs verified source access/authentication,
complete-slate provenance with corrections, corroborated cross-provider IDs and
market rules, authoritative game-start semantics, and an independently trusted
receipt binding exact bytes. Permission to implement this refusal interface does
not establish those facts. Runtime, bets, scoring and policy changes remain excluded.

## Checks

```sh
python3 -m pip install pytest -r scripts/requirements-shadow.txt
python3 -m pytest -q
```

The focused tests additionally mutate byte integrity, missing/symlinked files,
schedule census/identity, source shapes, duplicate candidates, final side/score/
time conflicts, forged trust flags and receipts, and asserted local times.
