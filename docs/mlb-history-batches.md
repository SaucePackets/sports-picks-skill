# Broader training inventory and resumable batches

The broader warmup/training schedule inventory is complete. The first fixed acquisition batch has been collected and reproduced offline. Full training acquisition and complete-history admission remain outstanding; this change does not expand the admitted 87-row model-training set.

## Measured scope

The fixed 2023-03-01 through 2024-09-30 window contains **4,945 regular-season occurrences and 4,860 unique games**, from 19 calendar-month requests. There are 83 repeated game IDs. The collector retains every occurrence, verifies team agreement, and refuses an ambiguous starter target rather than choosing a convenient occurrence.

The inventory yields **49 batches of at most 100 games** and at most **14,580 game-data requests**, in addition to the schedule requests. Each batch uses at most three workers and 300 requests; failures are retained without retries. The final batch has 60 games. No 2025 acquisition, development scoring, or reserved-test access occurs.

Batch 000 covers 2023-03-30 through 2023-04-06:

- 100 game feeds retrieved.
- 89 structurally corroborated pre-cutoff starter snapshots.
- Seven games without a provider snapshot before the cutoff.
- Four repeated-occurrence games with refused starter targets.
- 285 requests and 97,642,039 response bytes, approximately 97.6 MB.

A linear extrapolation from this first batch suggests approximately **4.75 GB** of response data for the full inventory, excluding filesystem/receipt overhead. This is a planning estimate, not a quota or measured full-season size. The transport still imposes its 16 MiB per-response ceiling. Available local disk was approximately 166 GiB before acquisition.

## Resume and replay

The source specification was committed as `7fc85fa` before acquisition. Monthly schedules are parsed with per-day counts and identities, while every game points to its original monthly response hash and JSON pointer. Temporary per-day parser inputs are not presented as provider source bytes.

Each batch has an immutable plan bound to the complete inventory digest and batch number. Resume validates all existing receipt identities, timestamps, object bytes, and selected snapshot URLs before issuing missing requests. Failed receipts are not retried. A sealed batch is replayed without network even when invoked with `--acquire`. Unknown receipts, changed plans, changed objects, and changed report bytes refuse. An exclusive per-batch lock prevents overlapping writers. Different batches should be run sequentially to retain the intended three-worker total.

The first live invocation exposed a tuple-versus-JSON-array equality issue in inventory-plan replay, before any game-data request. Comparing canonical serialized bytes fixed it, and an acquisition-to-offline inventory roundtrip test covers the regression. The completed batch and inventory now replay exactly. `mlb-history-batches-result.json` binds both reports and the final collector implementation; raw data remains outside Git.

```sh
# Initial inventory, or validated resume:
PYTHONPATH=scripts python scripts/mlb_history_batches.py inventory --root <inventory> --acquire
# One fixed batch, or validated resume:
PYTHONPATH=scripts python scripts/mlb_history_batches.py batch \
  --inventory <inventory> --root <batch-000> --number 0 --acquire
# Offline verification (omit --acquire):
PYTHONPATH=scripts python scripts/mlb_history_batches.py batch \
  --inventory <inventory> --root <batch-000> --number 0
```

## Training coverage and next work

Complete training-row coverage is deliberately **not evaluated**, not zero and not 89. Batch 000 is warmup data; it does not yet reach the declared May 2023 training start. Starter availability alone does not establish complete last-three pitching appearances, accepted team histories, outcome labels, or cutoff consistency. The prior admission adapters are pinned to 2025 and remain byte-unchanged.

The next implementation is a separately versioned, year-aware history-admission path, tested against this retained batch and the existing contradiction fixtures. Continue the remaining fixed acquisition batches with their original failures and denominators, then report admitted warmup/training rows and exact refusal causes. Only after that can the frozen model be refitted on broader training data. Keep July–September 2025 unopened until the evaluation contract is fixed.

No fitting, scoring, policy changes, live-model admission, or execution activation occur here. Historical snapshots remain provider reconstructions, without independent proof of original pregame availability.

Merged PR #115 was separately deployed at `18f2133f8d7694d1936d90f7921b1bf3175b3529`, receipt `.deploy/receipt-20260914-223644.txt`. This new batch collector is not deployed or scheduled.
