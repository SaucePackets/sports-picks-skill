# MLB benchmark first slice: synthetic mechanics only

Run from the repository root:

```sh
python3 scripts/mlb_model_benchmark.py --fixture tests/fixtures/benchmark/synthetic.json
python3 -m unittest discover -s tests
```

The CLI reads the explicitly named fixture and prints JSON. It makes no network
calls, loads no runtime state or policy, writes no files, and does not change the
existing shadow collector. `evidence_kind` must be `synthetic`; attempts to enable
historical mode fail. Every output says `synthetic_mechanics_only`,
`historical_performance: null`, and `eligible: false`. Fixture probabilities and
scores are arithmetic examples, not retrospective performance or evidence of an
advantage. There is no performance verdict, deployment decision, or bet output.

## Frozen specification v1

`scripts/mlb_model_benchmark.py:SPEC` defines the versioned numerical contract:
start both teams at 1500 each season, Elo scale 400, K=20, zero home-field offset,
no carryover and at least one prior game for both teams. These are deliberately
simple fixed fixture parameters, not fitted baseball parameters. Away probability
is `1 / (1 + 10 ** ((home_rating - away_rating) / 400))`; after a completed game,
add `20 * (away_won - away_probability)` to away and subtract it from home.
Sort updates by completion instant, then game ID. Tied completion instants use
that deterministic order; their actual sequencing is not independently known.
Only one UTC calendar season is accepted. The ratings freeze at the declared
cutoff and never update from held-out results. No parameter is tuned to scores,
prices or pick counts. A future model change requires a new reviewed specification.

Inputs contain `schema: mlb-benchmark-fixture-v1`, `evidence_kind: synthetic`,
a timezone-aware `training_cutoff`, and arrays `training`, `schedule`, `markets`,
`finals` (the last two may be absent). The committed fixture is a complete example.
Games require exactly `game_id`, `away_id`, `home_id`, `first_pitch`. IDs must be
nonempty, unpadded strings and teams must differ. Duplicate game IDs, overlapping
training/evaluation IDs, duplicate market/final observations, and unmatched
observations abort instead of selecting convenient evidence.

Training accepts only the seven fields shown in the fixture: game, source ID,
observation/completion timestamps, Final status and integer scores. No market or
arbitrary feature fields are accepted. First pitch must precede completion;
completion must not exceed observation; observation must be strictly before the
training cutoff. All evaluation games must fall on later UTC dates than the
cutoff and within its year. Training order is independent of input array order.
Missing prior games leave a team prediction unavailable, without market fallback.

Market records corroborate game ID, both team IDs and first pitch and require a
source ID and observation strictly before first pitch. Odds declare one book,
full-game moneyline including extra innings, and two finite decimal prices >1.
Away fair probability is `(1/away_decimal)/(1/away_decimal + 1/home_decimal)`.
That checks a supplied same-book assertion, not provider authenticity. The
team-strength adapter never receives market observations. Finals separately
corroborate identity and chronology. Missing/invalid prices and finals retain
schedule rows with reasons. Malformed identities, duplicate joins or non-JSON
numbers fail the whole input rather than hide denominator problems.

The output hashes the canonical input, frozen specification and implementation
bytes. The CLI also hashes the exact original fixture bytes. Retain those bytes
with any output. Hashes bind an artifact to content; they do not establish that
it existed at the asserted time or that an external source supplied it.

## Coverage, scores and refusal boundary

The denominator is the supplied schedule, including missing predictions and
finals. Completeness of a real-world schedule is not proven. Score the away side
once per game. Each family reports predicted/scheduled coverage, scored count,
Brier score, log loss (probabilities clipped only for log evaluation at 1e-15),
and ten fixed reliability bins containing counts, mean forecasts and observed
frequencies. Empty cohorts have null scores. Tiny fixture cohorts carry no
statistical significance. The team/market comparison lists its exact common
scored game IDs; the three-family intersection is empty because pitcher/context
is unavailable. Each family also exposes disjoint counts for missing prediction,
missing/invalid final after prediction, and synthetic scored rows.

`qualifying_pick_count`, `candidate_count` and `review_approved_count` are **null**,
not zero: this slice has no conservative-probability/uncertainty model, retained
executable ask contract, pinned selection-policy evidence, or corroborated
candidate/review receipts. No raw Elo edge is promoted into a qualifying pick.
Price and edge floors remain unchanged in the existing policy and runtime code;
this tool cannot override or weaken them. `live_eligible_count` is always zero
because live admission is disabled. This distinction prevents a missing funnel
stage from looking like an observed refusal or a completed review.

## Remaining evidence and implementation

Before retrospective performance can be enabled, retain and review actual source
bytes, dataset/provider/version identity, normalized-to-source mappings, schedule
completeness, stable team/season identities, timestamp availability and corrections,
and a predeclared training/held-out split and parameter-selection procedure.
Reconstructed historical data cannot claim prospective capture. Freeze a dataset
manifest before scoring and independently corroborate its provenance. No such
historical dataset is admitted by this slice.

Pitcher/bullpen/context stays an explicitly unavailable, separate required follow-up
within this feature: it needs its own reproducible lagged inputs, workload and
availability history, lineup/park context, missing-input rules and frozen estimator.
Qualification and candidate/review counts need their own evidence contract.
Live witness/collection integration and any runtime deployment require separate
scope. The existing `docs/mlb-shadow-collection.md` prospective fixture contract
remains unchanged.
