# Market-free historical reproducibility checkpoint

This standalone offline adapter reconstructs frozen Elo and empirical home-win
probabilities from a bounded, newly retained public MLB source bundle. It proves
replay mechanics and structural joins, not model improvement, original pregame
predictions, independently verified historical availability, or live admission.
The earlier synthetic benchmark and real-source refusal tools remain unchanged.

The [predeclaration](mlb-market-free-predeclaration.md) was committed as `40b8e5a`
before this lane's acquisition. It fixes April 21–27, 2025 training, a strict
April 30 00:00 UTC completion cutoff, May 1 evaluation, and one April 21 Savant
extract. The exact numerical specification is `SPEC` in the new adapter. Elo
starts at 1500, K=20, scale=400, no home offset, no tuning; completion ordering
uses numeric game ID ties. Empirical home-win probability uses Laplace smoothing.
Neither baseline receives evaluation results or Savant coordinates.

## Retained evidence and reproduction

The owner-retained workspace directory is
`RESEARCH/MLB_MARKET_FREE_BENCHMARK_2026_09_07/ACQUISITION`. It contains
`manifest.json`, individual acquisition receipts, and `objects/<sha256>` with
exact response-body bytes. Adjacent `CHECKPOINT_REPORT.json` retains the full
normalized report. Public [checkpoint metadata](mlb-market-free-checkpoint.json)
binds the manifest, every source body, complete report, and implementation by hash.
Raw source bytes and full report are retained in the workspace, not bundled in
this public code repository. A reviewer needs that retained bundle for exact
reproduction; downloading current responses does not reproduce the snapshot.

From the repository root, supply the retained directory explicitly:

```sh
python3 scripts/mlb_market_free_checkpoint.py --bundle "$BUNDLE" > replay.json
shasum -a 256 replay.json
python3 -m pip install pytest -r scripts/requirements-shadow.txt
python3 -m pytest -q
```

The output is deterministic sorted JSON, including all normalized training
rows/refusals, frozen Elo updates, team ratings/counts, each evaluation game and
its feature lineage, and every Savant row's source values, coordinates and join
result. Compare `replay.json` against the report digest in the metadata. Exit 0
means the reconstruction/lineage checkpoint completed, **not model qualification**.
Exit 1 reports missing census/Savant evidence or no accepted training; exit 2
aborts on an integrity/schema failure. Individual training/evaluation refusals
remain visible in an otherwise completed reconstruction.

All 100 acquired responses succeeded: eight daily schedules, 91 distinct training
game feeds, and one Savant CSV. Total original body bytes: 74,481,706. The seven
training schedule responses contain 95 occurrences. Four game IDs occur on two
dates because the current schedule includes postponed and later entries. All
eight occurrences are refused as repeated IDs, without selecting a favorite;
one additional occurrence lacks Final status in both sources. The remaining 86
training rows have corroborated team IDs, start/date, final scores and final-play
completion before cutoff. All 11 May 1 census games have both baseline outputs.
All 2,211 Savant rows structurally join, with original missing coordinates retained.

## Source contracts and limitations

Sources are manually acquired HTTPS responses from these MLB endpoints:

- `https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD&hydrate=linescore`
- `https://statsapi.mlb.com/api/v1.1/game/GAME_ID/feed/live`
- `https://baseballsavant.mlb.com/statcast_search/csv` with the exact one-day,
  regular-season pitch-detail query recorded in the manifest.

The [Savant field documentation](https://baseballsavant.mlb.com/csv-docs) defines
numeric MLB player IDs and game IDs. For each CSV row the adapter corroborates
game/date, numeric team identities and abbreviations, roster player identities,
and the batter/pitcher pair in its plate appearance. The composite pitch key
must be unique. Pitch coordinates are normalized only for lineage; their values
are not independently verified against another tracking source or used as model
features. No claim of complete pitch coverage follows from 2,211 successful joins.

Daily source counts must agree with the parsed rows. Postponed occurrences retain
their source date and original pointers even when the official date moved.
Duplicate IDs within a date abort; IDs repeated across dates refuse every training
occurrence. Feed IDs alone never corroborate a game: both sides, dates, start,
status and scores must agree. Last-play score and completion chronology are also
checked. Resumed/suspended evidence is excluded. Missing inputs retain refusals;
a missing daily response makes that denominator unknown rather than zero.

Schedule responses and feeds are from the same provider, not independent witnesses.
This is the **current supplied schedule census**, not proof of the original
historical schedule or all subsequent corrections. Play-event timestamps support
historical completion, but these responses were retrieved in September 2026;
original publication times and later revisions are unverified. HTTPS acquisition
receipts and hashes preserve locally retained integrity, not independent provider
signatures or trusted timestamps. No caller-supplied metadata enables live trust.

Only reconstructed final outcomes, team IDs and completion order drive ratings.
The report exposes every training update, source pointer and hash; prediction
rows reference the team histories and final ratings used. Empirical home wins
are explicitly counted over the accepted training set. Standings, expected stats,
season aggregates, actual starters, lineup/injury/weather context, market prices,
performance scores and candidate/pick outputs are absent. All reports say
`eligible: false`, `scoring_enabled: false`, `asof_snapshot_verified: false`, and
`historical_performance: null`. These labels cannot be overridden by the manifest.
The script has no acquisition, runtime-state, installation or deployment path.

## Validation boundary

Unit tests use synthetic source-shaped fixtures to attack byte corruption,
ambiguous census IDs, source metadata, side/score/completion conflicts, cutoff
inclusion, missing inputs, historical overlap, game/player/matchup joins, duplicate
pitch keys, nonfinite coordinates and evaluation-result leakage. Completion-order
mutation changes Elo while row reversal does not. Changing Savant coordinates
cannot change predictions. The separately retained real-source replay is the
checkpoint evidence; passing synthetic tests alone is not that evidence.
