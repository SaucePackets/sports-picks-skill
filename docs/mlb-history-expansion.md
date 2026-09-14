# Earlier-season acquisition feasibility

The bounded 2023–2024 probe supports proceeding to a broader training-data acquisition design. It does not establish a better predictor or full-season feature coverage.

The plan was committed as `663d50a` before network acquisition. It fixes six dates, selects the first two regular-season games by numeric ID per date without replacing refusals, and caps the probe at 42 requests. All original 2025 contracts and adapters remain unchanged.

## Retained result

- Six of six daily schedules retrieved, with original response bytes.
- Twelve of twelve selected game feeds available. Availability does not establish complete or admissible pitching statistics.
- Eleven of twelve games have provider-indexed snapshots strictly before start minus 60 minutes, matching game, teams, season, date, Preview state, and two distinct probable pitchers.
- Game `745130` on 2024-09-16 has no provider snapshot before that cutoff. It remains refused; no later snapshot or replacement game was substituted.
- 41 source requests, 12,229,707 retained response bytes. There were six initial schedule requests and 35 subsequent requests; all six schedules were reused byte-for-byte in the second pass.
- The successful probe report matches offline replay byte-for-byte, including receipt hashes and the final implementation digest. The compact result binds the full report hash.

The initial pass downloaded all six schedules, then encountered the old parser's hardcoded 2025 year check. Its report remains retained separately. The new probe has its own year-aware schedule parser with date/count/identity checks. It reuses validated schedules through `--schedule-cache` rather than reacquiring them or changing original experiment source code. All network failures and missing snapshots remain recorded; no retries, redirects, or forced successful records.

These historical timestamps are provider metadata, not independently verified proof of what was published at the original pregame time. This sample establishes neither the fraction of all-season games with snapshots nor completeness of last-three pitcher histories. The old 87-row training set has not been expanded yet, and no new model has been fitted or scored.

## Reserved research windows

The committed plan proposes:

| Role | Dates |
|---|---|
| Warmup | 2023-03-01 through 2023-04-30 |
| Training | 2023-05-01 through 2024-09-30 |
| Development, already inspected in part | 2025-01-01 through 2025-06-30 |
| Reserved retrospective test | 2025-07-01 through 2025-09-30 |

Only regular-season games belong in the model experiment. This probe requests no 2025 data. The reserved test is unscored by this effort, not a claim that its outcomes are unknowable or original pregame forecasts exist. Do not open it until bulk acquisition/admission and evaluation contracts are reviewed and fixed. Carry forward the declared three-feature L2=1 model and baselines; any subsequent feature/calibration choices belong to development, not test outcomes.

## Next implementation

Build a separately versioned acquisition/admission path for the broader warmup and training dates. It should support bounded batches and validated restart, explicit total request/storage estimates, complete schedule denominators, deduplication by game identity, preserved failures and raw bytes, and network-disabled replay. Acquire schedules first to establish the actual cost and game inventory, then feeds and pre-cutoff snapshots in fixed batches. A generic year parameter must not change any pinned 2025 replay.

Measure complete team/pitcher feature rows and remaining missing-history causes before fitting. Do not treat 11 successful snapshot samples as permission to skip history validation. The installed minimum of 300 paired evaluation games remains necessary but not sufficient; matched sportsbook comparison and independent timing evidence remain unresolved for live admission.

## Reproduce

```sh
PYTHONPATH=scripts python scripts/mlb_history_expansion_probe.py --root <new-directory> --acquire
# Optionally reuse the six verified schedules from an earlier attempt:
PYTHONPATH=scripts python scripts/mlb_history_expansion_probe.py --root <new-directory> --acquire --schedule-cache <earlier-directory>
# Offline only; refuses changed report, receipt, or source bytes:
PYTHONPATH=scripts python scripts/mlb_history_expansion_probe.py --root <sealed-directory>
```

Acquisition requires a new directory. Sealed reports and failed attempts are never overwritten. The cache option accepts only matching successful schedule receipts whose original objects and timestamps validate. Source bytes live outside Git; `mlb-history-expansion-result.json` contains the compact evidence.

PR #114 was separately deployed at `ab0cfdfaf7c0ba06294d74e75a553bfbf4db0494`, receipt `.deploy/receipt-20260914-205904.txt`. The new probe is offline research and is not deployed. No risk-policy, model-registry, cron activation, or execution change is part of this PR.
