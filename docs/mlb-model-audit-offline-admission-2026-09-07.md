# Offline replay admission: seven preserved schedules

Observed 2026-09-07 after authorization to resume offline work. This pass uses
only the seven retained schedules identified by SHA-256 in
[the initial inventory](mlb-model-audit-2026-09-07-inventory.json). All seven
hashes were checked again before inspection. No live checkout was accessed.

**Result: none of the three model families has an admissible scored game in
this corpus.** This is a data-admission result, not a model comparison or a
finding that the models have equal performance. Producing numeric scores from
these seven files would require adding evidence not present in them.

## What is actually available

There are 66 game reads on five dates, plus two schedules without game reads.
All five recorded denominators have the same row count as their respective
reads (9, 16, 15, 15, 11). Count equality does not establish correct identities
or coverage of the official schedule. The two missing denominators leave the
overall scheduled-game coverage unknown.

- 50/66 reads contain valid two-sided DK fair, raw, and conservative
  probabilities. Each value is a finite non-boolean number strictly between
  zero and one.
- 49/66 also contain valid two-sided asks. The remaining probabilistic read
  lacks asks; sixteen reads lack the entire numeric probability trail.
- Raw equals DK fair on both sides in 41/50 probability-bearing reads. This
  differs from the initial report's 40/40 because this denominator includes
  September 3 and the September 6 read without asks.
- No game-read object contains a prediction capture timestamp or first-pitch
  timestamp. One candidate contains first-pitch timestamps, but no independent
  prediction capture receipt. Candidate prose is not an immutable receipt.
- Recursive JSON-key inspection finds no structured outcome, final score,
  closing quote, training cutoff, feature snapshot, or fitted model artifact.
  This statement concerns these seven files only. It does not establish that
  such evidence is absent from other archives.
- The single candidate has `executed: false`. There are no executed fills or
  settlement objects here. Zero executed candidates in this snapshot is not
  a season-wide execution or profit-and-loss census.

The candidate's thesis contains pitcher/form/bullpen claims. Those are prose
claims on one selected game, without a reconstructible numerical model or
as-of feature archive. They cannot stand in for independent predictions on
the full population.

## Scan evidence is not prediction evidence

| Schedule date | Recorded scan fetched_at_utc | scan_sha256 present |
|---|---|---|
| Sep 1 | absent | no |
| Sep 2 | absent | no |
| Sep 3 | 2026-09-03T15:32:26+00:00 | no |
| Sep 4 | 2026-09-05T03:41:29+00:00 | yes |
| Sep 5 | 2026-09-05T21:32:28+00:00 | yes |
| Sep 6 | 2026-09-06T21:32:44+00:00 | yes |
| Sep 7 | 2026-09-07T15:36:36+00:00 | yes |

The denominator's game objects contain only `away`, `home`, `event_id`, and
`game_pk`. Thus even the scan timestamp cannot be compared against a per-game
start time from those objects. Four recorded scan hashes identify intended
source bytes, but the source scan payloads are not among these seven files;
their hashes cannot be independently recomputed from a hash string. Neither
the scan timestamp nor the schedule filename authenticates prediction time.

The September 3 read note says a watchlist promotion was removed during
integrity cleanup. Together with the stored review on the candidate, this
demonstrates that the schedule is a downstream state snapshot, not necessarily
the original prediction artifact. Preserve the snapshot; do not reinterpret
its current contents as an untouched pregame record.

## Three-family admission comparison

| Family | Available evidence | Missing evidence | Admitted scored games |
|---|---|---|---:|
| Market/de-vig | 50 stored two-sided DK fair estimates; 49 paired asks | Original two-sided odds and price timestamps to reproduce de-vig; prediction-time proof; verified outcomes | 0 |
| Independent team-strength | Game/team identifiers; nine reads differ from market, but no reproducible independent estimator | As-of team features or prior-game results, frozen model specification/artifact, training cutoff, predictions and verified outcomes | 0 |
| Pitcher/bullpen/context | One candidate's narrative baseball claims | Structured as-of starter, workload, bullpen availability and context inputs; frozen estimator and cutoff; predictions and verified outcomes | 0 |

All families also lack the common pregame provenance needed for out-of-sample
claims. The absence of outcomes alone prevents Brier/log loss and calibration
from being computed here. A recorded probability's existence is a weaker
condition than admissibility. The 66 reads are an observed population, not 66
eligible training or evaluation examples.

No time-aware split can repair those missing inputs. No train/test split was
run, no estimator was fitted, and no parameter was tuned. Numeric CLV and
realized-return results are **unavailable**, not zero: closing quotes and
settled fill records are absent. The previously recorded edge arithmetic is
still reproducible, but cannot substitute for predictive performance.

## Concrete next evidence package

Continue on the same feature branch when an owner supplies an offline archive
or authorizes a specific source for these inputs:

1. Original scan payloads matching the four recorded hashes, and any available
   earlier scan payloads. Preserve source identity and timestamps.
2. Original per-game predictions plus an independently timestamped receipt
   binding their bytes before first pitch; official game start/identity data
   and final outcomes with side reconciliation. Keep late/ambiguous records
   excluded and enumerate why.
3. Historical as-of team and pitcher/bullpen/context inputs, or an archived
   pregame prediction artifact for each independent family with model version
   and training cutoff. Reconstructed historical features must use only prior
   games, not present-day season totals.
4. Timestamped closing quotes for comparable contracts, and actual fills,
   costs and settlements if realized performance is to be assessed.

Then freeze model definitions and date-based training/evaluation boundaries
before examining evaluation outcomes. Report paired metrics on the shared
admitted cohort and separate per-family coverage. If the requested archives
do not exist, a prospective recording dataset is required; this audit does
not authorize production recorder changes or bets to generate examples.

Live ownership remains a separate hold. Resolving it does not manufacture the
missing evidence, and the offline data gap does not require changing runtime.
The three-family historical replay and full feature acceptance remain open.

## Reproduce the field and probability census

Run this inspection recipe from any checkout's repository root. Set
`MLB_AUDIT_SNAPSHOT_ROOT` to the absolute path of the retained `execute`
directory (in the Buzz workspace, `RESEARCH/MLB_MODEL_AUDIT_2026_09_07/execute`).
The environment variable is required, so detached worktrees need no assumed
relative layout. The recipe verifies the exact inventory before counting.
It reads files and prints only aggregate evidence.

```python
import collections
import hashlib
import json
import math
import os
from pathlib import Path

root = Path(os.environ["MLB_AUDIT_SNAPSHOT_ROOT"])
inventory = json.loads(Path("docs/mlb-model-audit-2026-09-07-inventory.json").read_text())
reads, keys = [], collections.Counter()

def visit(value):
    if isinstance(value, dict):
        for key, child in value.items():
            keys[key] += 1
            visit(child)
    elif isinstance(value, list):
        for child in value:
            visit(child)

def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value < 1

def paired(row, field):
    values = row.get(field)
    return isinstance(values, dict) and all(
        probability(values.get(side)) for side in ("away", "home"))

for item in inventory["rows"]:
    raw = (root / item["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == item["sha256"], item["file"]
    schedule = json.loads(raw)
    visit(schedule)
    reads.extend(schedule.get("game_reads", []))

trail_fields = ("dk_fair_prob", "raw_probability", "conservative_probability")
trails = [row for row in reads if all(paired(row, field) for field in trail_fields)]
print("reads", len(reads))
print("joint_probability_trails", len(trails))
print("trails_with_paired_asks", sum(paired(row, "polymarket_ask") for row in trails))
print("raw_equals_market", sum(
    all(abs(row["raw_probability"][side] - row["dk_fair_prob"][side]) < 1e-9
        for side in ("away", "home")) for row in trails))
print("recursive_keys", sorted(keys))
```

Expected counts in order: 66, 50, 49, 41. The three-field trail predicate is
evaluated on each row; matching marginal counts cannot establish this joint
population. Both ask coverage and raw-equals-market use that same population.
Moving a conservative pair from an equal-to-market complete row to a
trail-empty row after digest validation reduces joint trails to 49 and
raw-equals-market to 40, even though the individual field counts stay at 50.
Key inspection is a schema
census, not a semantic validator of arbitrary prose or a general-purpose
admission tool. No executable repository tooling or selection behavior was
changed for this pass.
