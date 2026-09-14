# Multiseason training-history admission

The new year-aware admission path reconstructs **71 complete training rows** from the fixed first seven acquisition batches (700 games). Collection of the remaining batches is continuing separately. This is a partial-data evidence result, not a new model or a performance improvement.

The seven-batch replay contains 611 games with accepted pitching-appearance records. It reconstructs 55 warmup pitcher-feature pairs and 71 training pairs; the latter also have usable target labels and team features. All 4,945 schedule occurrences remain in the denominator. Uncollected earlier games remain explicit gaps, so partial collection cannot silently imply complete history.

## Evidence and scope

The contract fixes May 2023 through September 2024 training dates and an October 1, 2024 UTC label-completion cutoff. Targets retain the existing start-minus-60-minute observation cutoff and exclude same-day history. Team features use the last ten accepted earlier outcomes; pitcher features use the last three accepted appearances, with unresolved recent appearances blocking the selection.

The evidence module copies the explicit pinned outcome, appearance, participant-v2, and postponed-interval checks, changing only the season identity from literal 2025 to the selected source date's year. This separate module is deliberate: editing or refactoring the pinned original source files would invalidate their existing byte-bound experiments. Original-year equality tests compare accepted outputs directly with the pinned functions. The copied rules should not be updated casually or independently of their regression contracts.

Participant certificates can exclude an unrelated pitcher or a game completed before all three selected appearances, but cannot supply missing statistics. Explicitly corroborated postponed intervals exclude a gap only before the later rescheduled start; suspension ambiguity stays refused. These rules use retained later metadata for historical reconstruction, not independent evidence of original pregame publication.

The initial contract draft omitted postponed intervals. A warmup replay showed that this made already-understood postponed games block otherwise unrelated histories. The final contract carries forward the previously reviewed interval proof, with the same strict date, identity, full-play, and no-suspension requirements. No model was fitted or scored while refining this data-admission boundary.

## Verified processing

Every included batch first passes the merged collector's complete offline replay: plan, inventory, receipt identity, original response hashes, selected snapshot URL, and sealed report all bind the input. Unsealed batches are not loaded. Their games remain unavailable in this report even if some partial request files exist.

An optimization computes each immutable certificate digest once instead of once per historical exclusion. It does not change the selection rule. The optimized and original calculations match every occurrence, history, feature, and summary exactly on the same seven sealed batches. Mutation tests compare both implementations for missing certificates, wrong source bytes/game identity, empty participants, cutoff equality, future completion, pitcher presence, and older-than-three completion.

The report includes input batch bindings and source-script digests. A source-file change during replay refuses the result. The compact committed result binds the exact full report, which is retained outside Git. No fitting, scoring, original-pregame assertion, or live eligibility is emitted.

```sh
PYTHONPATH=scripts python scripts/mlb_multiseason_admission.py \
  --root <directory-containing-inventory-and-batch-NNN> > admission.json
```

The published result is intentionally pinned to batches 000–006. Running against the acquisition directory while more batches finish will produce a new report with a different explicit set of batch bindings. Do not compare those reports as though their inputs were identical.

## Live forecast recording audit

The September 14 runtime slate already records all ten games, including passes. The inspected version had eight passes and two incomplete evaluations, all with complete probability trails and the `vig-mlb-market-v1` fallback. All ten recorded DK-fair probabilities and exchange asks. Market retrieval timestamps are nested under `market_evidence`; one inspected exchange quote explicitly retained earlier research pricing. This audit establishes recording coverage, not fresh-price execution eligibility or independent source authentication.

There is therefore no need to create another forecast ledger. Once a challenger is independently validated, record it against the existing all-game denominator and compare it with contemporaneous market prices. Current fallback records cannot be relabeled as challenger predictions. Execution job `84095861d05d` was verified disabled and paused.

## Remaining work

Complete and replay the remaining acquisition batches, then rerun this admission path on the complete fixed inventory. Report the final usable training count and refusal causes before fitting the declared model. Evaluate development performance and fix the evaluation contract before opening the reserved July–September 2025 test window. The larger dataset is not itself evidence of an edge, and no historical result here authorizes a live model switch.
