# Starter features: exploratory benchmark

Adding starter strikeout and walk history does **not** establish a useful predictor on the retained sample. The three-feature model loses to both 50/50 and frozen Elo in May and June. It is slightly better than the matched-training team-only model in June but slightly worse in May. No model is admitted or substituted for the market fallback.

## Declared experiment and evidence

Specification commit `cc2a916` preceded fitting/scoring. The separate experiment binds the exact participant-v2 and chronological team-admission report bytes. It keeps the original source contracts unchanged, trains on 87 complete March–April rows, and predicts 148 May and 187 June rows without tuning or refitting. The earlier team-only benchmark was trained on 283 feature-complete rows. Both that model and a team-only model fitted on the same 87 rows are included to separate added-feature effects from different training coverage.

This is retrospective, exploratory reuse. June outcomes have already been examined in prior research, so June is not an untouched confirmatory holdout. Input digest validation establishes retained-byte identity, not original pregame publication. There are no matched verified sportsbook probabilities or return-on-investment claims.

| Model | May Brier, n=148 | June Brier, n=187 |
|---|---:|---:|
| 50/50 | 0.250000 | 0.250000 |
| Frozen Elo | 0.244920 | 0.251710 |
| Training home-rate baseline | 0.264606 | 0.262100 |
| Team-only, 283 training rows | 0.262162 | 0.261612 |
| Team-only, matched 87 rows | 0.261422 | 0.261398 |
| Team + starter K/BB, 87 rows | 0.261577 | 0.260267 |

Lower Brier and log loss are better. Starter-model log loss is 0.716690 in May and 0.714201 in June, versus Elo's 0.682274 and 0.696498. All six models use identical evaluation rows. Coverage is 148/422 May and 187/402 June regular-season schedule occurrences; refused rows remain in those denominators. These are selected subsets, not all-game performance.

The fitted model's mean home-win probabilities are 59.39% in May and 59.79% in June, against observed rates of 47.97% and 49.20%. This is a substantial sample calibration mismatch. It supports investigating training coverage and baseline stability; it does not establish a universal home-bias correction. Changing the intercept to fit these months would reuse evaluation outcomes for tuning.

`mlb-starter-benchmark-result.json` preserves coefficients, training-only scaling, paired metric differences, source/specification/implementation hashes, and full-report hash. The full report additionally retains per-game predictions and calibration bins. The optimizer converged in three iterations, with maximum gradient approximately 3.2e-13. No new dependencies were added.

## Next work, in order

1. **Broader development data, before more features.** Inventory a complete earlier season for training and an adjacent season for validation, including warmup histories, prior starter appearances, and schedule exceptions. First use a bounded acquisition feasibility check; do not assume a full season is affordable or yields complete rows. Preserve raw bytes, retrieval failures, coverage denominators, and historical reconstruction labels.
2. **Freeze a new evaluation window.** Treat the already-inspected 2025 months as development evidence. Do not retune on June and call it a held-out improvement. Declare training/validation dates, feature and calibration choices, and a fresh evaluation window before looking at its outcomes. The current June intersection remains below the installed 300-game evaluation minimum; pooling May and June after looking at both would not cure that.
3. **Prospective same-game market comparison.** Resolve the existing quote-source and trusted timing-evidence requirements, then collect immutable forecasts and same-book prices before games. Compare probability quality, calibration, coverage, and actual quote availability before claiming actionable edges.

This benchmark does not justify expanding live market/sport coverage or changing probability haircuts, floors, sizing, or execution authorization. First establish that a model forecasts better on fresh data; then assess which quotes can support picks.

## Reproduce

```sh
PYTHONPATH=scripts python scripts/mlb_starter_benchmark.py \
  --participants <participant-v2-report.json> \
  --team-admission <chronological-team-admission.json> > starter-benchmark.json
PYTHONPATH=scripts python -m pytest -q
```

Numerical tests compare the one-feature reduction with the previous solver, check a closed-form constant-feature case, and independently check the multivariate objective's local gradient/minimum. Mutations verify input/spec byte binding, duplicate/cutoff/split refusals, missing-feature coverage, and that evaluation labels/features cannot alter training coefficients or scaling.

## Merged runtime changes verified separately

PRs #112 and #113 are deployed at `c4e8c2fd677edc6890eb0e2e8aa0ba0992da390e`, receipt `.deploy/receipt-20260914-204751.txt`. All 28 managed script copies match, and 26 deployed research tests pass. Risk file SHA256 remains `bb0f140db8d22dceb7cc9a5c4c7dfe6ab0273ee36a2bbfc7a3ca73e38027cfc6`; execution job `84095861d05d` remains disabled/paused. The last observed 15:45 CT review succeeded before this deployment; this is not evidence of a subsequent natural producer invocation. The new benchmark itself is offline and unmerged.
