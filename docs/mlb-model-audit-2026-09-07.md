# MLB model audit: initial provenance and coverage findings

Status: **partial audit; no model-performance verdict; not merge-ready**.
Observed 2026-09-07, approximately 16:45–16:50 UTC. Branch
`feat/mlb-model-audit`, repository base `e880c6d` (fetched origin/main).
Scope: read-only pipeline inspection and the first evidence inventory. No
selection, execution, threshold, model-deployment, or live-state changes.

## First findings

The latest priced game reads primarily compare market prices. Across the
2026-09-05 through 2026-09-07 schedule snapshots, **40/40 games with complete
two-sided raw, market, conservative, and ask values have raw probability equal
to DK fair on both sides** (absolute tolerance 1e-9). None has either side's
market-minus-ask gap reaching 0.05; none has conservative-minus-ask reaching
0.05. September 7 is an incomplete day, not a settled evaluation cohort.

That explains numerical ineligibility in those 40 recorded games without
requiring a claim about whether the baseball reads were good. It does not
establish that the floor is wrong or that another model would win. Preserve
the 5% conservative floor.

The label alone does not identify an independent model: September 3 and 5
both use `market-handicap-v1`, but September 3 has raw different from market in
all nine priced reads, while September 5 has exact equality in all fifteen.
September 6 uses `dk_fair_baseline_v1`. Neither label is the canonical
`vig-mlb-market-v1` fallback in `scripts/mlb_probability_model.py`.
This is recorded-label drift, not proof that the deployment gate authorized
either model.

## Recent schedule census

Source: top-level `.picks/execute/2026-09-01-schedule.json` through
`2026-09-07-schedule.json` in the live runtime. These mutable final snapshots
are not a census of all producer attempts or the whole season.

| Date | Reads | Complete priced games | Raw equals market, both sides | Any conservative edge >= .05 | Candidates |
|---|---:|---:|---:|---:|---:|
| Sep 1 | 0 | 0 | 0 | 0 | 0 |
| Sep 2 | 0 | 0 | 0 | 0 | 0 |
| Sep 3 | 9 | 9 | 0 | 3 | 1 |
| Sep 4 | 16 | 0 | 0 | 0 | 0 |
| Sep 5 | 15 | 15 | 15 | 0 | 0 |
| Sep 6 | 15 | 14 | 14 | 0 | 0 |
| Sep 7 | 11 | 11 | 11 | 0 | 0 |

September 1–2 have no `game_reads` key, so zero means missing recorder evidence,
not zero scheduled games. All seven schedules have empty lineup watchlists.
The September 6 unpriced row still has a model label; it is excluded from the
complete-price denominator, not asserted to have no probability.

Every recorded refusal tag in this window is counted below. Tags overlap;
these are stated reasons, not independently adjudicated causes:

- Sep 3: starter_floor 4; real_winner_conviction 3;
  bullpen_close_game_survival 2; cold_fade_reset 1;
  lineups_unconfirmed 1; incomplete_input_data 1.
- Sep 4: no_dk_price 16; game_already_started 16. All sixteen reads explicitly
  excuse the probability trail. This snapshot cannot score a pregame model.
- Sep 5: price_discipline 15.
- Sep 6: price_discipline 14; park_environment_cap 2;
  no_polymarket_market 1.
- Sep 7: price_discipline 11; incomplete_input_data 2;
  park_environment_cap 1.

The one September 3 candidate is unexecuted and explicitly rejected. Its
stored review names unavailable executable ask/liquidity, unverified rested
leverage arms, and unverified weather. Thus the observed rejection is not
attributable solely to the edge floor. Three reads clearing the arithmetic
floor but only one candidate also shows that the floor is not the sole gate.
The underlying baseball claims have not been independently verified.

## Live routing and version boundary

Read-only SSH to `saucepackets`, runtime `~/projects/sports-picks-runtime`:

- HEAD `51df0f8239cad450f5114ce8c1846d0721315498`, branch
  `fix/mlb-policy-state-resolution`; subject: `fix: resolve Vig policy state from profile home`.
- Working tree has an uncommitted 15-line addition in
  `tests/test_mlb_lineup_watchlist.py`. Ownership has not been established in
  this audit. Do not reset, clean, switch, deploy, or claim this is clean main.
- Stored morning job `c9452052719c` and evening job `27087cc00dfa` are enabled,
  target that runtime, and mention `mlb_slate_writer.py`. Their prompt hashes
  exactly match the reviewed after-hashes in
  [producer-writer contract](producer-writer-contract-2026-09-04.md):
  `5b477b8cc57eeb136748b4004d885d5780540ae9fefbba1f72e280496fa59b97`
  and `9a5c2783a764e55b06cd64da44188552e17898443d79b69cb67b1d7a0c568671`.
- Enabled review job `75e72e2dc5be` names `vig_mlb_review_gate.py`; enabled
  execution job `84095861d05d` names `mlb_execution_gate.py`. Both target the
  same runtime. No jobs were triggered by this audit.
- Live `~/.hermes/vig/state/risk_limits.json` contains
  `min_conservative_edge: 0.05` and a model deployment policy with minimum
  evaluation count 300. File presence does not prove effective resolution by
  every running profile script; the policy-resolution branch makes that
  distinction especially relevant.

The supported repository route is Stage 2 scan -> away/home game reads and
candidate draft -> writer validation/landing -> scheduled review -> execution
gate, with `mlb_slate_receipt.py` checking producer output. See
[producer contract](producer-writer-contract-2026-09-04.md) and
[evaluation contract](model-evaluation.md). Job configuration confirms routing,
not a successful end-to-end run or delivered channel message. Receipt, profile
script parity, and delivery-event reconciliation remain outstanding.

Live file SHA-256 values captured in this audit:

| File in scripts/ | SHA-256 |
|---|---|
| mlb_probability_model.py | 7649ea53d9da1ecf1ba8202e2d2468e42bab850c60c7e568fbdef6953a413326 |
| mlb_slate_writer.py | 2f87db432905f9e988179a040f9e96e221cdfed677e911ebbe03dfc2cfea505c |
| mlb_game_reads.py | 80426dd811d9384f227934f3ef8a2554a481f38b314c55cbb076c154d06ad759 |

## Replay admission gate and remaining work

At repository base `e880c6d`, `mlb_model_eval_dataset.py::row_for_read` joins
probabilities to finals and checks transposition, but does not check a
prediction capture time against first pitch. Its rows being scoreable does
not prove they were recorded pregame. Likewise,
`mlb_probability_model.py::walk_forward_report` sorts existing predictions
into windows; it does not fit models, verify training cutoffs, or establish
that feature inputs preceded the games. Chronological sorting alone cannot
make leaked predictions out of sample.

No Brier/log-loss, calibration, CLV, ROI, or model ranking is published here.
The three-family comparison is **not yet performed**. Admission requirements:

| Family | Required evidence before scoring |
|---|---|
| Market/de-vig baseline | Two-sided same-book odds, capture time before first pitch, verified game/side mapping and final |
| Independent team-strength | Archived as-of team inputs or reconstructible prior-game results; fixed model specification; training cutoff before each test block; no market input |
| Pitcher/bullpen/context | As-of starter identity and workload, bullpen availability, lineup/context inputs; fixed model specification and cutoff; no use of later season totals as earlier features |

Use identical admitted games for paired comparisons, report each model's own
coverage separately, retain one away-side outcome per game, and keep whole
dates together in train/test blocks. Freeze specifications and any tuning on
earlier training data before evaluation. Closing-price quality additionally
requires a timestamped close in a comparable market; slate ask is not CLV.
Realized performance needs executed fills and settlement/cost evidence;
hypothetical returns must be labeled separately. Missing evidence stays
missing. The audit does not authorize collecting it by executing bets.

Next: reconcile ownership/version of the live branch, locate immutable
pregame scan and prediction snapshots, verify their timestamps/game identities,
then measure admissible cohort sizes for all three families and closing prices.
Only after that gate can a reproducible historical performance comparison be
claimed. Do not retrofit predictions after seeing outcomes.

## Reproduction and evidence retention

The owner-accessible local snapshot is
`~/.buzz/RESEARCH/MLB_MODEL_AUDIT_2026_09_07/execute/`, retained for
independent replay. Raw live history is not
committed, following README's runtime-state boundary. The accompanying
`mlb-model-audit-2026-09-07-inventory.json` records exact source hashes and
aggregate counts. A new live fetch can differ and must not be called the same
snapshot. The following reproduces the two key arithmetic counts for each
copied September schedule; it is an inspection recipe, not a production tool:

```python
import json
from pathlib import Path

for path in sorted(Path("SNAPSHOT/execute").glob("2026-09-*-schedule.json")):
    rows = json.loads(path.read_text()).get("game_reads", [])
    complete = []
    for row in rows:
        try:
            values = [(row["raw_probability"][side], row["dk_fair_prob"][side],
                       row["conservative_probability"][side], row["polymarket_ask"][side])
                      for side in ("away", "home")]
        except (KeyError, TypeError):
            continue
        complete.append(values)
    print(path.name, len(complete),
          sum(all(abs(raw - market) < 1e-9 for raw, market, cons, ask in values)
              for values in complete),
          sum(max(cons - ask for raw, market, cons, ask in values) >= .05
              for values in complete))
```

Validation: aggregate arithmetic inspected against seven copied schedules;
no source-code changes and no test-suite result claimed. This partial report
is for provenance/methodology review, not final feature acceptance.
