# Offline MLB shadow evidence contract v1

This tool stores prospective evidence and exercises an offline receipt contract.
It places no bets, calls no data providers, changes no runtime files, and emits no
performance metrics. It is not a live collector or three implemented models.
The only implemented estimator is the same-book, full-game moneyline including
extra innings baseline: `(1 / away_decimal) / (1 / away_decimal + 1 / home_decimal)`.
Both decimal odds must be finite numbers greater than one in a single source
snapshot. Team strength and pitcher/bullpen/context always report unavailable,
including when market data is present. Their required inputs are placeholders
for future reviewed estimator specifications, not claims of trained artifacts.
The baseline is untrained; its required training cutoff records the declared
as-of boundary and cannot be later than the source observation.

## Reproduce the offline vertical slice

From a checkout, create an isolated environment and install the optional dependency:

```sh
python3 -m venv .venv-shadow
.venv-shadow/bin/pip install -r scripts/requirements-shadow.txt
.venv-shadow/bin/python -m unittest tests.test_mlb_shadow_collection -v
.venv-shadow/bin/python -m unittest discover -s tests
```

`test_complete_offline_dry_run` generates a TEST Ed25519 key, ingests retained
source bytes for all three families, appends signed fixture receipts, appends
finals and closing bytes, and reports all three schedule rows. Other tests mutate
binding, signature, timing, identity, model versions, probabilities, and storage.
Fixture keys are generated in memory and never installed into any runtime.

For existing offline artifacts:

```sh
.venv-shadow/bin/python scripts/mlb_shadow_collection.py \
  --store /path/to/offline-store --schedule /path/to/schedule.json \
  --fixture-keys /path/to/test-public-keys.json
```

No output path is inferred; JSON goes to stdout. There is no live-mode flag.
The schedule is a JSON array of games. Fixture keys are an externally supplied
JSON array of raw Ed25519 public keys encoded as lowercase hex; never load trust
keys from a receipt itself.

## Ingestion API and schemas

`scripts.mlb_shadow_collection.Store(root)` creates an explicitly chosen offline
store. `capture(store, game, source_bytes, family, producer_key, training_cutoff)`
returns the digest of a canonical bundle. It does not issue a receipt. Call
`store.append('attempts', record)` for each receipt/no-receipt attempt. The record
has `game_id`, `family`, `bundle_digest`, and `receipt` (null if absent).

A game contains nonempty, unpadded string `game_id`, `away_id`, `home_id` (distinct teams),
and timezone-aware string `first_pitch`. Padded IDs are rejected, never silently
normalized; this applies before distinct-team and unique-game checks. Sources corroborate all four against the
supplied schedule, and contain `source_id` and timezone-aware `observed_at`.
A market source contains `odds` with `book`,
`market: "full_game_moneyline_including_extras"`, `away_decimal`, `home_decimal`.
This normalized snapshot asserts both sides are from that book/market. The tool
checks consistency of the supplied evidence; it does not authenticate a provider
or independently establish whether the snapshot's claims are true.

Bundles contain schema/model version 1, game, source digest/identity/time,
producer key, family, specification digest, implementation-file artifact digest,
training cutoff, and prediction probability/missing reasons. Source bytes are
retained exactly. Bundle and record canonicalization is Python JSON with sorted
keys, compact separators, ASCII escaping, no NaN, UTF-8 bytes. V1 pins the entire
module artifact, so code updates invalidate old artifacts until a future explicit
version migration is reviewed. Specification hashes are derived from `SPECS`.

Append outcome sources separately with
`store.append('finals', {'source_digest': store.put(original_bytes)})`.
Final and closing sources require the same nonempty-string source identity as
pregame sources. Invalid timestamp types are rejected as invalid artifacts without
suppressing the schedule coverage rows. Final sources contain the corroborated game, source identity/time, `status: Final`,
nonnegative integer away/home scores, and must be observed after first pitch.
Ties, mismatched teams, and multiple distinct finals are rejected. The evaluated
side is always away. “Verified” means structural consistency of these retained
inputs, not independent provider authentication or a live settlement receipt.

Closing sources are stored similarly in `closings`, using the same odds schema.
The observation must be at/after the selected fixture witness observation and strictly before first
pitch, with the identical book and market. Multiple distinct observations are
ambiguous. The supplied closing observation is not proven to be the last quote;
its fair probability is exposed as a comparison input only. No CLV or performance
claim is calculated. Missing/noncomparable closing data leaves predictive fixture
verification intact.

## Receipt and storage trust boundaries

The TEST receipt is `{payload, signature}`. Its exact signed payload is produced
by `receipt_payload(bundle_digest, observed_at, witness_key)` and includes schema
1 and purpose `mlb-shadow-fixture-only`. Signature is Ed25519 over canonical
payload bytes, hex encoded. The witness must be in the externally trusted TEST
key set and differ from the bundle's declared producer key. Observation must be
at/after source observation and training cutoff, and strictly before first pitch.

This checks real signatures in a test domain. Key inequality alone does not prove
organizational independence, and a producer declaration is not authenticated
identity. Clock accuracy, witness operator independence, key provisioning,
revocation, provider authenticity, and a real witness service are all unresolved
live trust concerns. Signed times here are fixture assertions. A receipt proves
observation of bytes only to the extent the witness is trusted, not source truth.
Local time, Nostr created_at, hashes, fixture signatures, and unsigned records
never admit a live row. `eligible` is unconditionally false for every row;
`fixture_contract_pass` is deliberately separate. A future live verifier requires
separate scope and review, not a configuration switch.

Objects/records use SHA-256 filenames. Writes create a temporary file, flush it,
and atomically hard-link into the destination without replacing existing bytes.
Identical replay is idempotent; distinct attempts and source versions remain.
Every read rehashes bytes. This is an append-only API, not a WORM device: an OS
administrator can rewrite/delete storage, and hashes cannot prove completeness.
Persist backups externally before any future live use. Corrupt record files abort
reporting; corrupt referenced bundles/sources are rejected during admission.

## Selection and coverage

Schedule × three families is the denominator. Duplicate schedule game IDs abort.
Every matching attempt and reason is retained in the report; attempts outside the
schedule or known families appear separately in `unmatched_attempts`. Among passing TEST
receipts, select the earliest witness observation, then bundle digest, then attempt
digest; there is no best-outcome or highest-probability selection. Byte-identical
retry does not add a second game. With no passing fixture, the lowest attempt
record digest supplies the summary state; the complete attempt list is authoritative.
States are captured, missing, late, invalid; eligible is reserved but disabled.
The shared eligible cohort is the actual intersection of per-family game IDs,
and is necessarily empty in this slice. No scored evaluation is produced.
