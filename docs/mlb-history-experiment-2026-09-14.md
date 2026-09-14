# Retained-history and team-form experiments — September 14, 2026

PR #108 is deployed at `54db7909f8a7f42705ccb44270f19840a50c21bb`.
Receipt: `.deploy/receipt-20260914-191932.txt` on the runtime server.
All 28 managed profile scripts match. The deployed queue/producer/audit tests
passed: 29 tests and 2 subtests. A manual invocation of the deployed review gate
completed and wrote the runtime decision audit at 19:27:05 UTC. The queue tracks
all ten games; first retries are due at 19:40 UTC (14:40 CT), within the next
15-minute review invocation. No research request was due during that check.
The natural 14:30 CT review then completed successfully and wrote its runtime
audit at 19:30:35.845863 UTC with no input errors or recheck warnings; all ten
games remained tracked as pending. This verifies deployment, scheduler invocation,
and the initial queue path, not a completed live producer handoff. There are no new picks from this verification.

Execution job `84095861d05d` remains paused. The risk file SHA256 remains
`bb0f140db8d22dceb7cc9a5c4c7dfe6ab0273ee36a2bbfc7a3ca73e38027cfc6`.
No model registry, betting threshold, cron schedule, or order path was changed.
The historical experiments below are offline and are not in the deployment manifest.

## Pitcher-history coverage

`mlb_recovered_history_experiment.py` composes the retained bulk-starter replay
with the already-reviewed appearance census. It recomputes the census from bytes
and requires its exact committed checkpoint before combining results. All original
pinned adapters and contracts remain unchanged.

| Split | Starter identities | Feature pairs before | Feature pairs after |
|---|---:|---:|---:|
| Train | 433 | 0 | 0 |
| Validation | 390 | 0 | 0 |
| Test | 387 | 0 | 0 |

Recovering 189 appearance-accounting occurrences leaves 137 unresolved occurrences.
The earliest are March 27: game 778554 (ambiguous substitution), 778559 (unsupported
play event), and 778562 (play chronology). The frozen rule blocks either pitcher's
history for **any** unresolved earlier regular-season game, regardless of team.
Thus all 780 validation and 774 test pitcher-side histories remain blocked. Train
has 834 blocked histories and 32 with fewer than three prior appearances.

This explains why counting recovered games overstated practical model readiness.
The report preserves exact remaining gaps and contributing appearance pointers.
Its checkpoint and replay digest are in `mlb-recovered-history-experiment.json`.

## Separate team-only experiment

The distinct specification `mlb-team-form-experiment.json` was committed in
`893d995` before fitting or scoring. It does not replace the frozen three-feature
challenger. No hyperparameters were changed after seeing validation or test results.

The single feature is the difference between home and away win fractions over the
last ten **accepted** earlier games. Same-day results are excluded and contributing
results must complete before the prediction cutoff. The existing adapter skips
unaccepted outcomes, so this is not necessarily each team's last ten actual games.
That missing-data selection is a research limitation, not evidence of complete
historical availability.

The model is logistic regression with an intercept, training-only standardization,
and fixed L2 penalty 1 on the slope. It trained on 283 feature-complete, temporally
admissible games. The empirical-home and Elo baselines use 438 admissible training
labels; Elo freezes at the training cutoff. Validation and test predictions use
prior results available under the reconstruction contract but never refit weights.

All models are compared on the same 401/422 validation and 390/402 test schedule
occurrences. Missing or refused rows remain in the coverage denominator. Scores
are point estimates; no claim of statistical significance or betting profitability
is made.

| Model | May Brier | June Brier | June log loss |
|---|---:|---:|---:|
| Constant 50% | 0.250000 | 0.250000 | 0.693147 |
| Frozen Elo | 0.242412 | 0.254707 | 0.702678 |
| Empirical home rate | 0.257654 | 0.257837 | 0.709197 |
| Team form | 0.255344 | 0.256853 | 0.707164 |

Lower is better. Team form fails to outperform either coin or Elo on both held-out
splits. The result does not support live admission. It also demonstrates why more
picks should not be the model-selection objective: this model can generate a
probability for almost the full test slate without improving probability accuracy.

Full reports retain per-game predictions, source digests, fitted parameters,
optimizer convergence, calibration bins, and coverage. Compact results and the
full-report digest are in `mlb-team-form-result.json`.

These are reconstructed historical evaluations, not independently timestamped
original pregame predictions. There is no same-book sportsbook comparison and no
live model admission. `eligible`, `asof_snapshot_verified`, and
`original_pregame_predictions` remain false.

## Reproduce

Use the owner-controlled retained acquisition directories; neither command makes
network calls or modifies them. Redirect full reports outside the checkout:

```sh
python scripts/mlb_recovered_history_experiment.py \
  --bundle <chronological-acquisition> \
  --snapshot-dir <bulk-starter-acquisition> \
  --baseline-replay <bulk-starter-directory>/R2_TIP_REPLAY.json > history.json
python scripts/mlb_team_form_experiment.py \
  --bundle <chronological-acquisition> > team-form.json
PYTHONPATH=scripts python -m pytest -q
```

The full suite passed: **1,830 tests, 714 subtests**, with one optional dependency
skip. Tests check recovery source/denominator binding, unresolved unrelated games,
strict completion cutoffs, same-day exclusions, target refusals, numerical
optimization against a closed-form intercept and an independent objective
calculation, held-out label isolation, paired coverage, and hand-computed metrics.

## Next useful work

The pitcher lane needs an evidence-backed way to establish participant completeness
for unresolved games before any narrower gap rule can be justified. Simply ignoring
unrelated-looking games would change the frozen contract without proving that a
pitcher's latest appearance was not skipped. Keep that experiment separate.

The negative team-form result should remain a recorded baseline. A stronger
challenger needs a separately declared feature/data hypothesis and new evaluation;
do not tune this specification on the June test results. Prospective comparison
still needs the user's existing odds provider/configuration location and an
accepted independent timing-evidence path.
