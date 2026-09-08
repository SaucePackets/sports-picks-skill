# Bounded chronological source admission

The first acquisition admits reconstructed final facts and lagged last-10 team
histories for the unchanged [predeclared contract](mlb-chronological-contract.json).
It does not fit, predict, score, admit a challenger row, or change runtime policy.
The [receipt](mlb-chronological-admission.json) pins source, report, and adapter
bytes. Base is `bdf055093a9233bd130af17c7385b821f13b6f64` (PR #92).

## Retained census and outcome results

All 122 daily schedules and 1,271 unique regular-season game feeds returned HTTP
200 and were retained, including empty dates, spring games, and refused schedule
occurrences. Every daily source is represented in the census; missing or malformed
days yield an unknown denominator, never zero. This is the current provider's
historical census, not proof of its original publication state.

| Split | Daily responses | All occurrences | Regular season | Accepted outcomes | Training labels | Last-10 team pairs |
|---|---:|---:|---:|---:|---:|---:|
| Warmup | 17 | 257 | 0 | 0 | 0 | 0 |
| Train | 44 | 576 | 467 | 448 | 438 | 295 |
| Validation | 31 | 422 | 422 | 401 | 0 | 402 |
| Test | 30 | 402 | 402 | 390 | 0 | 393 |

Outcome admission compares numeric game/team identities, source date, scheduled
start, season/type, final states, and integer scores between schedule and feed.
It checks completed plays, timestamp order, and the last play's final score.
Moved dates, duplicate IDs across dates/splits, resumed/suspended status, nonfinal
sources, and contradictory completion evidence are refused with occurrence-level
source pointers and hashes. Missing census prevents outcome admission because
cross-date duplicate detection cannot be established. Source corruption aborts
replay instead of silently becoming missing data.

There are 1,239 accepted outcomes and 52 regular-season refused occurrences:
40 repeated-ID occurrences, 5 moved dates, 4 nonfinal records, and 3 play chronology
conflicts. The 366 non-regular occurrences are explicitly excluded. Ten accepted
training-date outcomes completed at/after the May 1 UTC boundary and are refused
as training labels; they may contribute to later lagged histories under the fixed
contract. Warmup never supplies fitted labels.

Team features use each side's last 10 accepted regular-season outcomes, sorted
by completion and numeric game ID. Every contributor has an earlier officialDate
and completion strictly before scheduled start minus 60 minutes. Same-day games
and equality at the cutoff are excluded. The report retains all contributing
outcomes, source hashes/pointers, wins and denominator; fewer than 10 gives null
and a refusal. A target's final feed is never used to establish its team history.
Feature counts therefore differ from accepted-label counts. Refused prior outcomes
remain visible in the census; the history is explicitly the last 10 *accepted*
games, not an assertion that no other game occurred.

## Starter feasibility: source candidate found; admission still blocked

Selection was one first regular-season occurrence per split ordered by source
date then numeric game ID, independent of outcomes. The provider timestamp index
and exact selected snapshot bytes are retained separately. The parser requires
returned metadata to match an indexed timecode strictly before the cutoff,
corroborates game/team/date/start, requires Preview state, and reads only
`gameData.probablePitchers`. It never substitutes boxscore starters.

| Split | Game | Provider snapshot timecode | Away / home probable pitcher IDs |
|---|---|---|---|
| Train | 778563 | 20250318_060807 | 808967 / 684007 |
| Validation | 778094 | 20250501_160657 | 694477 / 680732 |
| Test | 777677 | 20250601_150219 | 672456 / 663460 |

There was no regular-season warmup occurrence to probe. These three successful
samples establish a concrete source candidate, not bulk coverage, independent
historical timestamp authentication, or verified original pregame predictions.
Current retrieval headers are diagnostics only. Bulk starter acquisition and
Savant/feed corroboration of the required last-three pitching appearances remain
unimplemented; every regular-season challenger row explicitly refuses both
pitcher features. No zero, average, final-boxscore identity, fallback challenger,
or performance result is substituted. All fitting/scoring/eligibility flags remain
false. This is the next admission blocker, not a model finding.

## Offline replay and access

Retained source bytes remain owner-controlled outside Git. Relative to the shared
Buzz workspace, the evidence directory is
`RESEARCH/MLB_CHRONOLOGICAL_ADMISSION_2026_09_07`:

- `ACQUISITION/manifest.json` and every referenced `objects/<sha256>` file;
- `STARTER_PROBE/{timestamps,snapshot}-<game_id>.json` and referenced objects;
- `ADMISSION_REPORT.json`, `STARTER_FEASIBILITY.json`, and `RETAINED_REPLAY.json`.

A reviewer on another host needs an owner-authorized copy preserving these paths;
GitHub access alone is insufficient. Do not replace retained bytes with fresh
fetches. No public upload or independent source signature is claimed. The initial
exploratory May 1 `precutoff` response is retained in STARTER_PROBE as diagnostic
history; the reproducible probe uses the exact indexed `snapshot` response.

From the repository root (Python 3.14.7, standard library):

```sh
python3 scripts/mlb_chronological_admission.py --bundle "$EVIDENCE/ACQUISITION" > admission.json
python3 scripts/mlb_chronological_starter_probe.py --bundle "$EVIDENCE/ACQUISITION" --probe-dir "$EVIDENCE/STARTER_PROBE" > starter.json
shasum -a 256 admission.json starter.json "$EVIDENCE/ACQUISITION/manifest.json"
```

Compare hashes and byte counts to `docs/mlb-chronological-admission.json`.
Successful report emission exits 0; it does not mean challenger eligibility.
Admission integrity/schema errors exit 2. Probe sampling gaps are explicit
blocked rows. Run without `--acquire` for offline verification. For a separate
future acquisition, the recipe is `mlb_chronological_acquire.py --bundle NEW_DIR`;
it refuses to replace an existing manifest and resumes immutable receipt objects.
The probe adds sources only when explicitly passed `--acquire`.

The old retained replay still verifies and exits 1 (longer evaluation blocked).
Its corrected report hash is
`8a90400f9bfac8eb037c91c3ad5578eaa562fac4a37700cd1de8220b63ea6c8f`.
The predeclaration reference now matches PR #92 metadata; no old source bytes,
model contract, or pinned replay implementation were altered.

## Validation

Python 3.14.7; isolated test environment with pytest 9.1.1, cryptography 50.0.1,
polymarket-us 0.1.2, and httpx 0.28.1. Full `python -m pytest -q` comparison:
base `bdf0550`: 1,462 passed, 1 skipped, 632 subtests passed, 4 failed;
this change: 1,472 passed, 1 skipped, 632 subtests passed, the same 4 failed.
The existing failures are the execution lock test and three lineup-watchlist
policy expectations. They are not changed here; the repository suite is not green.
The ten new adversarial tests pass, covering strict training/history cutoffs,
unknown census, source corruption, cross-split duplicates, outcome corroboration,
target-final independence, and historical snapshot identity/time/state checks.
