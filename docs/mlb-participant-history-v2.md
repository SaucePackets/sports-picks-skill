# Participant evidence v2: four retained-record exceptions

An opt-in `--evidence-version 2` extends the offline participant-history experiment. Version 1 remains the default. Its retained replay's certificates, occurrences, features, and summaries match the previous report exactly; the adapter's own code digest necessarily changes. The original pinned appearance adapters, chronological contract, and model policy are unchanged. The original v1 result file remains historical evidence, not a digest for the new source file.

## Narrow evidence rules

- **777550 and 777434:** third-out pickoff plays have outer start timestamps later than their ends. The existing runner-out classifier corroborates the non-plate-appearance semantics from runner movement, counts, event references, and an explicit out. V2 additionally checks every nested event index and interval and requires its latest end to equal the outer end. The completion bound includes the later outer start, conservatively retaining the anomaly rather than repairing it.
- **777474:** the final play is an unfinished, non-scoring, non-out `game_advisory` in a rain-shortened final. Both schedule and feed must corroborate the final rain ending. Only the last play may take this exception, with valid nested event intervals inside its outer interval. Pitch totals, identities, participant membership, and final scores still reconcile. This certifies a participant superset, not a completed plate appearance or usable pitching statistics.
- **777342:** pitcher 690953 is listed with zero games pitched and all five checked activity counts zero but is absent from play/substitution logs. V2 keeps that identity in the conservative participant superset. An unseen pitcher with any active count or games-pitched value of one still fails.

Each certificate retains original source hashes, anomaly pointers or pitcher IDs, and `statistics_admitted=false`. There is no source normalization or timestamp rewriting. Unknown histories, strict cutoffs, source binding, and last-three selection remain enforced by the existing history code. This is retrospective reconstruction from retained feeds, not proof of pregame publication.

## Full retained replay

| Split | V1 pitcher pairs | V2 pitcher pairs | V2 with team feature and usable label |
|---|---:|---:|---:|
| Train | 90 | 90 | 87 |
| Validation | 148 | 148 | 148 |
| Test | 58 | 188 | 187 |

All four record exceptions now have certificates: 112 accepted participant certificates and 23 postponed-occurrence intervals. Five later makeups still lack a matching final schedule in the retained window; only their already-corroborated pre-rescheduling intervals can exclude gaps.

The supported replay reran acquisition planning, original recovery/checkpoint verification, and participant narrowing from retained bytes. `mlb-participant-history-v2-result.json` records its full report hash, adapter digest, source digests, and labeled join. Full reports remain outside the checkout. The v1 replay was also compared field-for-field against its retained report, excluding the separately added parent/code digest fields.

No predictor was fitted, scored, admitted, or deployed. Historical performance remains null and eligibility false. The June labeled intersection is still below the installed 300-game evaluation minimum. The next modeling decision needs a declared evaluation design and sufficient untouched evaluation data; these coverage gains alone do not justify more picks or weaker selection gates.

## Reproduce

Use the existing documented participant-history command with `--evidence-version 2`. Omitting the flag retains v1 behavior. Run `PYTHONPATH=scripts python -m pytest -q` for regression tests.

Tests cover all three exception types, v1 rejection, preserved source hashes, conservative completion bounds, retaining zero-activity identities, and mutations of event timestamps, indices, terminal status, scoring/out flags, and active-player coverage. Existing contradiction tests run against both versions.
