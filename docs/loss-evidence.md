# Executed-loss postgame evidence analysis (read-only)

`scripts/vig_loss_evidence_report.py` runs the postgame collector's
deterministic thesis-pillar grading (`mlb_postgame_evidence.auto_pillar_grades`)
across every executed, officially decided MLB pick the audit can reconcile —
the merged replay's 51-win / 38-loss cohort — and aggregates pillar failures
with explicit denominators. The wins are graded with exactly the same code and
reported next to the losses, because a loss-only failure rate invites the
base-rate fallacy this repo has already committed once.

## Running it

```sh
# Offline, byte-deterministic once the evidence cache exists:
python3 scripts/vig_loss_evidence_report.py --picks-dir <corpus> --json

# One-time cache population (fetches feed/live per gamePk, stores the
# collector's output under <corpus>/postgame-evidence/<gamePk>.json):
python3 scripts/vig_loss_evidence_report.py --picks-dir <corpus> --fetch
```

The committed report below was generated with the window pinned, and a re-run
that means to reproduce it must pin the same window:

```sh
python3 scripts/vig_loss_evidence_report.py \
    --picks-dir <corpus> --until 2026-08-30 --json
```

Without `--until`, a live corpus grows and a later run reports a different
candidate count — a difference in the window, not a regression.

`<corpus>` is a `.picks`-shaped directory: `execute/<date>-schedule.json`
slates plus an `audit-results/` cache of MLB Stats API schedule payloads (the
same layout `vig_historical_audit` and `vig_pick_replay` read). Corpus
selection, reconciliation, and official rows come from
`vig_historical_audit.build_report` — this tool never re-derives an outcome.

Exit codes: 0 when every cohort game was graded from a usable evidence file;
1 when any game is `missing` or `invalid` (the report still prints, with the
holes named); 2 on configuration errors.

## Honesty properties, enforced by tests

- **Corpus selection is a receipt, not an assumption.** The report counts
  every executed record, names every excluded one (pushes, unreconciled), and
  zero-fills the loss classification counts over the replay's closed set.
- **No hindsight leakage.** Bet-time evidence reaches the grader only through
  the `PREGAME_EVIDENCE_FIELDS` allowlist; a test proves flipping every
  outcome label moves games between cohorts without changing one pillar grade.
- **No silent drops.** Every cohort game produces a row; ungradeable games are
  listed in `coverage` with reasons and excluded from denominators out loud.
- **Corrupt cache never launders into a graded game.** Evidence files are
  shape- and `game_pk`-validated before use.
- **Determinism.** Same corpus and cache → byte-identical report, regardless
  of input order.
- **Coverage is measured per field, not as a roll-up.** `records_by_field`
  counts each allowlisted field separately and zero-fills, so a field that is
  absent from the cards is distinguishable from one the audit layer does not
  carry. The sibling-import closure is pinned too — the read-only claim is a
  property of what this module reaches, not of its own source.
- **Recorded and gradeable are counted separately.** Each field reports
  `{recorded, gradeable}`: `expected_ip: 0`, or a `starter_role` outside the
  grader's vocabulary, is a value the card holds and the grader refuses. It
  counts as recorded, is excluded from gradeable, and is named individually in
  `recorded_but_ungradeable` — a coverage number that claimed an input while
  the pillar it feeds reads `unknown` would be the same defect as measuring
  the carrier instead of the cards. The gradeability rules are imported from
  `mlb_postgame_evidence` (`usable_expected_ip`, `EXPECTED_STARTER_ROLES`),
  not restated here; two copies agree only until one moves.

## Findings over the 2026-05-19 → 2026-08-30 corpus (89 games)

Committed machine-readable report: `docs/loss-evidence-report-2026-08-31.json`
(generated from the runtime `.picks` corpus snapshot taken for the merged
side-selection-attribution slice; evidence coverage 89/89 `complete`).

### Pillar grades (deterministic, collector output)

| Pillar | Losses (38): failed/decided | Wins (51): failed/decided |
|---|---|---|
| offense_conversion | 19/38 = **50.0%** | 3/51 = 5.9% |
| bullpen_availability | 7/38 = 18.4% | 5/51 = 9.8% |
| starter_role | 0 decided (38 unknown) | 0 decided (51 unknown) |
| starter_quality | 0 decided (38 unknown) | 0 decided (51 unknown) |
| named_risk | 0 decided (38 unknown) | 0 decided (51 unknown) |

Joint view of the two decidable pillars in the losses: offense failed alone in
16/38, bullpen failed alone in 4/38, both failed in 3/38; in 2/38 both held
outright (and in the remaining 13 at least one graded `mixed`).

### Descriptive game-script facts (outcome-side; never pregame rationale)

- Backed side scored ≤2 runs in 19/38 losses and 3/51 wins.
- Backed starter allowed ≥4 earned runs in 17/38 losses vs 7/51 wins — the
  starter line separates the cohorts even though the *contract* pillar is
  ungradeable (below).
- Backed-side actual pitching role: losses 32 starter / 5 short_start /
  1 opener_bulk; wins 45 / 5 / 1. Role busts do **not** differentiate losses
  from wins in this corpus.

### The structural finding: 3 of 5 pillars are historically ungradeable

0 of the 89 cohort cards carry a `baseball_evidence` block at all, so all three
allowlisted bet-time fields are absent — `records_by_field` in the report reads
`{recorded: 0, gradeable: 0}` for `starter_role`, `expected_ip`, and
`named_risks` alike, with `recorded_but_ungradeable` empty. `starter_role`,
`starter_quality`, and `named_risk` therefore grade `unknown` across the entire
corpus by contract — the deterministic grader refuses to invent an expectation
it was never given. This is the same shape the attribution slice found on the
opponent side (0/109 cards record `opponent_shutdown_path`): the postgame
contract shipped before the pregame recording that feeds it.

**The recording change is already starting to land.** 1 of the 111 corpus
candidates does carry a full `baseball_evidence` block (2026-08-30, Houston at
NY Mets: `starter_role: "starter"`, `expected_ip: 5.5`, populated
`named_risks`). It is outside the cohort because it was not executed, so the
89-card zero is real — but it says the slate prompt can already produce these
fields, and a corpus of new cards will not stay at zero.

That is why `vig_historical_audit.recorded_rationale` now carries
`STRUCTURED_EVIDENCE_VALUE_FIELDS` (`starter_role`, `expected_ip`) alongside
the text subset. Before that, those two fields were dropped at the audit layer
whatever a card recorded, so the coverage number above would have measured the
audit's schema rather than the corpus, and the recommended recording change
could have shipped in full while this report still read `unknown`. An
end-to-end test builds a corpus through `write_day` with a real
`baseball_evidence` card and asserts `starter_role`/`starter_quality` grade
`held` and `failed` from card state alone.

### Evidence vs inference

**Evidence** (deterministic, reproducible from the committed report): the
failure-rate table, the joint counts, and the descriptive facts above.

**Inference** (clearly labelled as such): executed losses are predominantly
"our offense didn't convert" games — offense_conversion is the dominant failed
pillar and the win denominator shows winning with ≤2 runs is rare — with the
backed starter's run prevention as a strong secondary separator. Whether
either was *foreseeable at bet time* cannot be answered from this corpus,
because no pregame offense or starter expectation was recorded to compare
against. A 50% failure rate on an outcome-correlated pillar is partly
mechanical; it becomes actionable only once a recorded pregame expectation
exists to grade it against.

## Bounded decision

**No model change in this slice.** Under the honesty rails, no
probability-component adjustment mined from these numbers is a legitimate
candidate today: every candidate would key on pillar outcomes whose pregame
expectations were never recorded, so it could not be stated as a bet-time rule
and could not be graded leave-one-month-out against recorded inputs. The
replay's LOPO grader already showed no price-band rule survives out of sample;
nothing here revives one.

The deployable action is a **recording change, not a model change**: the slate
prompt should write the structured bet-time fields the postgame contract
already consumes (`starter_role`, `expected_ip`, `named_risks` — and the
opponent case) onto every card. That closes the 0/89 coverage gap, makes
`bad_read_*` attribution possible at settlement time, and gives a future slice
LOPO-gradable pregame features. Scoping that prompt change belongs to a
separate slice; this one stays read-only.
