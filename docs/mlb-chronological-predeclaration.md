# Chronological MLB evaluation: first checkpoint

This contract is declared after historical outcomes and the earlier short-window
baseline output exist, before this lane acquires longer-window inputs, fits a
challenger or computes performance. It is not a prospective preregistration.
Base: `95b2fce6e8ee634f9d192e598bc0b9bf0f958a57`. The machine-readable
[contract](mlb-chronological-contract.json) fixes the choices below. Do not move
dates in response to missing data, scores, or source success.

## Reviewer replay and access boundary

The exact retained source bundle remains owner-controlled, outside Git. On the
shared Mac nest its path relative to the workspace root is
`RESEARCH/MLB_MARKET_FREE_BENCHMARK_2026_09_07/ACQUISITION`.
A reviewer with access to that workspace can pass that directory directly.
A reviewer on another machine needs an owner-authorized copy of `manifest.json`
and every referenced `objects/<sha256>` file, preserving relative paths. Receipt
sidecars are useful acquisition diagnostics but are not needed by the replay.
Do not substitute a fresh download. GitHub access alone does not provide the
bundle; if the directory cannot be read, replay is BLOCKED, not verified.
No public upload, public availability, or independent source signature is claimed.

From the feature repository root with Python 3.14.7 (standard library only):

```sh
python3 scripts/mlb_chronological_checkpoint.py --bundle "$BUNDLE" > checkpoint.json
# Expected exit 1: retained replay verified, longer evaluation blocked.
# Exit 2: unreadable/mismatched evidence, schema or implementation failure.
python3 scripts/mlb_market_free_checkpoint.py --bundle "$BUNDLE" > replay.json
shasum -a 256 replay.json
```

The new command verifies the manifest against the committed checkpoint metadata,
verifies both adapter files, revalidates all source bytes through the existing
adapter, and compares the complete serialized replay with the pinned byte count
and SHA-256. Expected replay SHA-256:
`1a1993c45fc17668fc0e4b85d5ca06da4a91403ef7fa0d55e8e9b6c741ff0bda`.
The old checkpoint's baseline reconstruction is the only calculation replayed.
The new command cannot admit a different bundle or enable fitting/scoring.

## Fixed chronological window

Use MLB officialDate with inclusive dates: warmup March 1–17, 2025, training
March 18–April 30, validation May 1–31, test June 1–30. Warmup contributes only
prior history, never fitted labels. Retain every daily schedule response,
including empty dates and every game occurrence; use regular-season games only.
A missing day makes its census denominator unknown, not zero.

Training labels must have corroborated final completion strictly before
2025-05-01T00:00:00Z. Training games completing later are refused, even when their
official date is April 30. Each feature observation cutoff is scheduled start
minus 60 minutes, in UTC; equality is excluded. Split boundaries use officialDate,
not retrieval date. A feature's prior games must have both an earlier officialDate
and final completion strictly before its observation cutoff. This deliberately
excludes same-day games, including doubleheader game one. No target game's final
feed may establish its pregame starter or any of its features.

For every split preserve refused occurrences and reasons. Repeated IDs across
dates, moved/resumed/suspended games, conflicting teams/dates/scores, nonfinal
labels and uncorroborated completions are refused, never silently deduplicated.
No train/validation/test ID overlap. Later feeds may establish labels only.

## Sources and feature contract

Use the existing StatsAPI v1 daily schedule and v1.1 game feed contracts described
in [the retained checkpoint](mlb-market-free-checkpoint.md), plus Savant pitch
CSV with numeric game/player IDs. Pin exact URL/query, retrieval time, response
headers, body hashes, adapter version and field mapping. Retrieval time is a
local acquisition diagnostic, not historical availability. Current corrected
feeds support reconstruction only; original publication time and as-of snapshots
remain unverified. Do not label reconstructed results original pregame predictions.

The proposed challenger has exactly three features, home minus away:

- Win fraction over each team's last 10 accepted prior regular-season games.
- Strikeout fraction over each starter's last 3 accepted prior MLB regular-season
  pitching appearances: strikeouts / completed batters faced, pooled across those
  appearances. Require all three and a positive denominator.
- Walk fraction on the same appearances and denominator, including intentional
  walks. No pitch-count denominator and no averaging appearance percentages.

Order history by final completion, numeric game ID tie-break. Count one completed
plate appearance per corroborated at-bat key; strikeout/strikeout-double-play
count as strikeouts, walk/intentional-walk as walks. Require complete terminal
plate-appearance data and consistent pitcher IDs; refuse ambiguous mid-appearance
pitcher substitutions. Features retain all contributing game/at-bat pointers,
source hashes, completion times, numerators and denominators. No season-to-date
current aggregates, expected stats, Savant coordinates, or future outcomes.

The target starter identity requires a retained, timestamped pre-cutoff source
with teams/game IDs and historical availability evidence. An actual starter from
a final boxscore is insufficient. This source and a validated parser are NOT
present in the retained bundle; pitcher features are currently unsupported.
Team histories are supported by the old adapter only for its short window;
no longer-window admission or feature adapter is implemented here.
Missing team history, starter identity, any prior appearance or necessary field
refuses that challenger row. No zeros, league averages, synthetic substitutions,
complete-case denominator concealment, or fallback challenger. Baselines retain
their separate coverage and refusal counts.

## Frozen models and evaluation measures

Elo starts at 1500, K=20, scale=400, no home offset, numeric game ID tie-break
on completion order; require one prior game per team. Train on accepted warmup
and training outcomes completed before the training cutoff and freeze thereafter.
Empirical home probability is (home wins + 1)/(n + 2) on that same accepted
history, unavailable when empty. Elo home probability is one minus its away
probability. Neither baseline updates on validation/test outcomes.

The sole challenger is logistic regression with intercept and the three features.
Fit only eligible training rows. Standardize using training population means and
standard deviations; a zero-variance feature becomes zero in every split.
Minimize mean binary log loss plus 1/2 times the sum of squared non-intercept
coefficients. No hyperparameter search, validation refit, ensemble, recalibration,
or test-driven changes. Solver, numerical convergence tolerances and dependency
versions must be pinned in a separately reviewed implementation before fitting;
failure to converge refuses the model. Validation is a diagnostic checkpoint,
not permission to tune. Later prior outcomes may contribute lagged history to
later observations, but never coefficient/scaler/baseline updates.

Evaluate home-win y in {0,1}. Brier is mean (p-y)^2. Log loss is mean
-[y log(p)+(1-y) log(1-p)] using natural logs and clipping to [1e-15,1-1e-15]
only for that measure. Invalid/nonfinite probabilities are refused, not clipped
into validity. Calibration uses 10 fixed equal-width bins, left-inclusive and
right-exclusive except the last includes 1; report count, mean p and home-win
fraction, null for empty bins. No fitted calibration curve.

For each split and model report supplied schedule occurrences, known regular-season
occurrences, admitted unique labeled games, predicted games, scored games and
refusals by reason. Coverage = predicted regular-season occurrences / all supplied
regular-season occurrences (null when zero or census unknown). Scoring denominator
is labeled predicted games. Report each model's own coverage and scores, plus
paired Brier/log-loss differences on the common labeled intersection of all three
models with its explicit count. Empty intersections produce null, never zero.
No accuracy/ROI/edge proxy or model-improvement claim from unequal populations.

## Current stop condition

The pinned bundle contains eight schedule dates, 86 accepted training games,
nine training refusals, 11 May 1 prediction rows and one day of Savant lineage.
It lacks the full longer-window census, labels and pre-cutoff starter/history
inputs. Deterministic replay proves the old checkpoint's reproducibility only.
The checkpoint reports exact missing schedule dates and always leaves longer
admission, fitting, scoring and eligibility false; historical performance is null.
New evidence admission and its parser require a later reviewed checkpoint.
No odds, runtime, deployment, bets, pick-policy or floor changes are made.
