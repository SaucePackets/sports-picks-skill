# Evaluating the handicap on games we did not bet

## The deadlock this removes

`scripts/mlb_probability_model.py` owns a versioned deployment gate: a
non-market `model_version` may only be used once the gate reports it deployable
out-of-sample against the DK-fair baseline. Otherwise the **market-only
fallback** (`vig-mlb-market-v1`, `raw_probability == dk_fair_prob`,
`uncertainty_haircut == 0`) stays active.

The gate's evaluation dataset is built by `mlb_probability_model.py dataset`,
**from `picks.json` — settled, executed picks.** That is a closed loop:

- a model version cannot deploy without out-of-sample evidence,
- evidence rows come only from bets we placed,
- and we cannot place bets while no model is deployed.

Measured on 2026-08-31: 45 settled picks, of which **8** carry the full
probability trail the dataset builder requires, all between 2026-07-26 and
2026-08-10. During a drought the loop produces exactly zero new rows a day.

Why this matters in practice, measured over the 2026-08-11..08-31 window: under
the market-only fallback the executable edge reduces to `dk_fair_prob - ask`,
and across 79 games with both numbers recorded the best side offered a **median
of +0.001** and reached the 0.05 floor in **4 of 79** games (1 of 79 once a 0.03
haircut is charged, 0 of 79 at 0.05). By contrast, all 8 executed picks with a
full trail drew their edge from the model's disagreement with DK — median
`raw - dk_fair` of **+0.0545** — and the price gap alone cleared the floor in
**none** of them.

## What replaces it

The slate handicaps every scheduled game and keeps the one or two it bets.
`game_reads` now records the rest: `raw_probability`, `uncertainty_haircut`,
`conservative_probability` and `model_version` on **every** game, refused ones
included.

**A handicap on a game we passed is still a testable pre-pitch prediction.** It
is also the less biased sample, which is the substantive argument and not a
convenience: a pick exists only where the model liked itself enough to clear a
five-point edge floor, so calibration measured on `picks.json` is calibration
measured where the model was most confident. Every scheduled game is the
population the gate should be judging.

Throughput: roughly fourteen rows a night with nothing at risk, against roughly
half a row a day when the card is active.

## Building and evaluating

```bash
python3 scripts/mlb_model_eval_dataset.py \
    --schedules .picks/execute --start 2026-09-01 --until 2026-09-21 \
    --finals .picks/audit-results --out dataset.jsonl
python3 scripts/mlb_probability_model.py evaluate --dataset dataset.jsonl
python3 scripts/mlb_probability_model.py gate --dataset dataset.jsonl \
    --model-version <version>
```

`--fetch-finals` pulls any date the cache does not cover. The evaluator and the
deployment gate are **unchanged**: they were never broken, only starved.

Two properties of the builder worth knowing before reading its output:

- **One row per game, always the away side.** A row needs one probability
  against one binary outcome. Emitting both sides would double the count with
  perfectly anti-correlated rows and break the independence the metrics assume;
  choosing "the side the model favoured" would re-introduce the selection bias
  the dataset exists to remove. The rule is fixed and independent of what the
  model thought.
- **Nothing is imputed.** A read missing a probability, a `model_version`, or a
  final is skipped with a stated reason, and the reasons are printed with the
  row count. A dataset that silently drops what it could not parse is a dataset
  whose denominator nobody can check.

## Predeclared margins

`skills/sports-picks/references/mlb_model_deployment_policy.json` holds the
`mlb_model_deployment_policy` block, **written and committed before a single
evaluation row existed.** The gate is designed around predeclared margins;
choosing them after seeing the curve is curve-fitting. If they need to change,
change them in a commit that does not also carry an evaluation result.

`min_evaluation_picks` is **300**, not the built-in default of 40: that default
was sized for executed picks. At ~14 reads a night, 300 is about three weeks,
and it is the first point where a per-row measurement on this population is
worth reading — the measured per-row P/L standard deviation on the 2026-08
sample was 0.485, which needs roughly 361 rows for a five-point interval.

Installing the block does **not** deploy a model and does not change betting
behaviour. Until it is installed in `~/.hermes/vig/state/risk_limits.json`,
`load_model_deployment_policy` returns `None` and the gate fails closed to
market-only — which is the live state as of 2026-08-31, when the key was absent
from that file entirely.

## Open: the two halves of the pipeline disagree

Recorded here rather than fixed, because fixing it either way changes betting
behaviour and that is out of scope for this slice.

The slate handicaps every night and writes candidates carrying
`win_probability` well above `dk_fair_prob`. The review gate, following the
market-only contract, recomputes at `raw == dk_fair` and the edge collapses. On
2026-08-30 that is exactly what happened to the only candidate that reached the
gate all month:

> "Model-gated fallback only: dk_fair_prob 0.6898, raw/conservative 0.6898,
> haircut 0.000; refreshed edge 0.0098, below the required 0.05 floor. No bet;
> exposure $0."

The slate had handicapped Atlanta at 0.74 against a de-vigged DK fair of
0.6898. Neither half is wrong on its own terms; together they spend a slate
producing candidates the gate is bound to discard, and they make the record
harder to read — the game is stored as a `review_gate_rejected` decision about
Atlanta, when what actually happened was a decision about whose number we are
allowed to use.

Resolving it means either the gate honours a deployed handicap (which is what
the evaluation dataset above is for) or the slate stops writing one. Both are
behaviour changes and belong to a scoped lane with a human decision in it.
