# Retained bulk starter snapshots

This bounded acquisition extends the three-source starter feasibility probe over
all eligible occurrences in the unchanged chronological contract. It uses the
retained daily schedules to choose requests and the existing strict starter and
appearance parsers to replay them offline. It creates **zero feature rows** and
performs no fitting, scoring, odds lookup, eligibility, policy, runtime, or
deployment work. No historical performance is claimed.

## Acquisition and integrity

`mlb_bulk_starter_snapshots.py --acquire` first verifies the retained chronological
bundle and writes an immutable plan containing every fixed-window date and
occurrence, source hashes, refusals, and observation cutoffs. Missing daily
coverage makes the denominator unknown and suppresses acquisition. Nonregular,
repeated-ID and moved/resumed targets remain in the denominator with refusals.
Training cutoffs are capped at the existing training completion boundary.

For each eligible game it requests the StatsAPI timestamp index once, then at
most one snapshot: the latest indexed timestamp strictly before the cutoff.
There are three workers, a 20-second network I/O timeout, a 16 MiB response limit,
no redirects and no retries. An oversized response retains at most the limit plus
one byte as a refused prefix, never as a usable source. HTTP error bytes are also
retained and hashed. No final-feed fallback, alternate archive, earlier-snapshot
search, expanded dates, or prior-appearance recovery occurs.

Each receipt records the exact URL, local start/completion time, HTTP status,
headers, failure, size and SHA-256 of its retained bytes. Content-addressed objects
and receipts are created exclusively. Interrupted unsealed runs can resume from
verified receipts with the identical plan; retained failures are not retried.
A sealed run replays without issuing new requests. Malformed or conflicting
cached snapshots abort before more acquisition. A crash during a write can leave
an incomplete file: validation then fails closed; it does not silently repair it.

The final manifest binds the exact receipt set and plan hash. Offline replay
reconstructs the plan from the original bundle, validates every receipt and body
including error responses, compares the manifest bindings, and enforces the
expected request set. Missing planned receipts are integrity failures. No-candidate
indexes and unavailable responses are explicit source refusals. Extra indexed
receipts, receipt replacement/removal, byte corruption, traversal digests and
receipt/object/root symlinks are rejected by this tool or the existing loader.
Unreferenced objects are not replay inputs. Hashes establish retained integrity,
not provider authenticity; compare the committed checkpoint to detect replacement
of the entire manifest and its inputs.

## Replay boundaries

Starter candidates use `mlb_bulk_starter_admission.starter`, including the strict
cutoff, latest timestamp, metadata, numeric game/team/pitcher identities, official
and original dates, scheduled start, season/type and uninterrupted Preview gates.
The target's final feed never selects its starter. A candidate is an identity
observation supported by provider timestamp metadata; original historical
availability remains independently unverified.

The existing `appearances` parser separately rechecks the retained final-game
census, completion chronology, team/person IDs, plate appearances, substitutions
and counts. The report gives counts and explicit refused games, without building
last-three histories, fractions, labels or features. Existing history date and
completion-cutoff gates remain unchanged and are exercised by the full package
suite; this acquisition does not bypass them or claim a usable history dataset.

The inherited **1,243 missing-source occurrence refusals** remain an explicitly
labeled prior-checkpoint count, not a count of new HTTP failures. The new replay
has **326 unresolved appearance-game occurrences**, an independent blocker even
where a starter candidate now exists. No usable starter/history pairs are asserted.

## Retained checkpoint and reproduction

Owner-controlled evidence is under the shared Buzz workspace:

- Original bundle: `RESEARCH/MLB_CHRONOLOGICAL_ADMISSION_2026_09_07/ACQUISITION`
- New receipts/objects/manifest: `RESEARCH/MLB_BULK_STARTER_SNAPSHOTS_2026_09_07/ACQUISITION`
- Reports, acquisition implementation and tests: `RESEARCH/MLB_BULK_STARTER_SNAPSHOTS_2026_09_07/`

The new sealed run retains 1,246 timestamp indexes and 1,213 snapshots, all with
HTTP 200 responses. Of the eligible indexes, 33 have no strictly pre-cutoff
snapshot. Three returned snapshots fail starter admission (two missing home
probable-pitcher fields, reported as `'home'`, and one non-Preview snapshot).

| Split | Known dates | All occurrences | Regular occurrences | Starter candidates |
|---|---:|---:|---:|---:|
| Warmup | 17 | 257 | 0 | 0 |
| Train | 44 | 576 | 467 | 433 |
| Validation | 31 | 422 | 422 | 390 |
| Test | 30 | 402 | 402 | 387 |

The total census remains 122 dates, 1,657 occurrences and 1,291 regular
occurrences. Its 40 duplicate-ID occurrences and five target-identity refusals
are preserved. The 1,210 starter candidates are not feature pairs.

From the repository root with Python 3.14.7, set `BUZZ_WORKSPACE` to the shared
workspace directory, then replay with no acquisition flag:

```sh
python3 scripts/mlb_bulk_starter_snapshots.py \
  --bundle "$BUZZ_WORKSPACE/RESEARCH/MLB_CHRONOLOGICAL_ADMISSION_2026_09_07/ACQUISITION" \
  --snapshot-dir "$BUZZ_WORKSPACE/RESEARCH/MLB_BULK_STARTER_SNAPSHOTS_2026_09_07/ACQUISITION" \
  > replay.json
shasum -a 256 replay.json
```

Compare the full output hash and input/implementation hashes with
[the committed checkpoint](mlb-bulk-starter-snapshots.json). Exit 0 means a report
was produced, not that histories or features are usable; integrity errors exit 2.
GitHub access alone is insufficient to replay the retained owner-controlled
bytes. No raw feeds are published. The acquisition-time implementation is retained
separately because subsequent hardening adds validation of resumed caches; the
manifest preserves the original acquisition implementation hash.

Validation: full-package pytest with Python 3.14.7, pytest 9.1.1, cryptography
50.0.1, polymarket-us 0.1.2 and httpx 0.28.1. Assigned base `ca285039` has 1,485
passes, one skip, 634 subtests and six failures: execution-lock, two execution
prompt time-window expectations, and three lineup-watchlist expectations.
The suite is not green; these failures are inherited. New controls cover request
boundaries, deterministic network-free replay, missing daily coverage, duplicate
and moved games, strict training/time cutoffs, failed and oversized responses,
receipt removal and integrity, identity/state/metadata, independent appearance
missingness, symlinks, interrupted acquisition, and cached-selection conflicts.


## Review revision: cached receipt contract

Resume now validates every cached receipt before workers start and repeats the
same validation before sealing and during offline replay. Both local timestamps
must parse with timezones and completion must be at or after retrieval start.
Sizes must be nonnegative integers, agree with actual object sizes, and remain
within the configured response ceiling. The only allowed ceiling-plus-one byte
body is an explicitly refused `response_size_limit_exceeded` prefix; it cannot
become evidence. HTTP status, headers, failure and absent-body metadata are
validated too. The shared admission loader still checks identity, paths and hashes.
The retained acquisition bytes and original acquisition hash are unchanged.

Resume regressions cover valid receipts, oversized indexes and snapshots, missing,
malformed, timezone-free, null and backwards completion metadata, and invalid
sizes. Offline replay also rejects missing completion even if manifest bindings
are recomputed. Tests separately retain the bounded oversized-prefix refusal.
