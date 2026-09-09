# Producer source representation and schema binding

The September 9 producer used writer contract v1 without an explicit recording
vocabulary. Its 15-row draft carried `extreme_park_confidence_cap`,
`missing_offense_data`, `unknown_park_environment`, and
`unavailable.away_offense=true`. The existing validator correctly rejected them.

Use `mlb_producer_prompt_contract.py --bind-schema --job-id <id> --input <prompt>
--output <new-prompt>` to prepare a separate schema-bound prompt from an existing,
valid writer-contract prompt. `--bind-schema --check` verifies the full generated
block against the current validator. This operation adds the current digest to each of the one skeleton and two land
commands and appends the block without changing existing policy prose; a stale or edited block requires explicit migration
and is refused rather than silently replaced. This source change installs nothing.

The block publishes `REFUSAL_RAILS`, `EXPLAINABLE_FIELDS` and a deterministic
fingerprint derived from those vocabularies, dispositions, schema version, and the
validator source digest. It renders both skeleton and landing calls with
`--schema-sha256 <digest>`. Bound-prompt checks inspect every invocation and reject
omitted, stale, duplicate, or unsupported command arguments even when the schema
block is intact. Unexpected or non-inline writer commands also fail closed. A mismatch returns failure before draft or schedule I/O.
The flag is opt-in for backward compatibility: unbound legacy calls do not gain
this check. The digest is compatibility evidence, not proof a producer read the
prompt, and does not bind external runtime policy files or the whole import closure.

Source representations follow existing semantics:

| Observed source reason | Recording representation |
| --- | --- |
| Evidenced extreme-park confidence cap | `park_environment_cap`, not `extreme_park_confidence_cap`; preserve source explanation in report |
| Missing offense input causing refusal | `incomplete_input_data`, not `missing_offense_data`; name the missing source input in report |
| `away_offense` absent from scan | Not an `unavailable` field; that object describes absent recorded model/price fields using nonempty string reasons, never booleans |
| Unknown park environment | Probability-model `unknown_park_environment` haircut vocabulary, never a refusal alias; follow existing no-park-adjustment rule |

No automatic alias adapter repairs failed drafts. In particular, a zero-haircut
read carrying the unknown-park refusal needs an evidenced model reassessment;
removing the token or inventing a haircut would change the decision. Unknown
fields/tokens remain invalid. Preserve every game row and retain failed artifacts
before any separately reviewed producer correction. No thresholds, policy,
probability-model rules, or execution code change here.

Incident evidence: retained draft and scan plus the dated job output confirm the
four malformed representations and five validation errors. Current profile schema
bytes match the checked-out base schema; the separate runtime checkout has an
additional unrelated context check. The captured current job prompt says producer
writer contract v1. Its current hash is not an execution-time prompt hash; the
historical output supplies text and errors, not a cryptographically bound execution
receipt. Private retained artifacts stay outside this source tree; regression
fixtures preserve the four field fragments and synthesize the full 15-row shape.
