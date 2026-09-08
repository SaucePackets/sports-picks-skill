# Market-free historical reconstruction checkpoint v1

Frozen before this lane's acquisition and calculation, after historical outcomes
exist. This supersedes the workspace draft PREDECLARATION.md only for this lane.
Authorized requests: `afe909440929a315e9bcb3d34491dd10897dd6bd53d5b37d97d95ee74b08fee0`
and `91caad208b03c92a9dc686d7292e6320c726a03504a69e7a9492a03d7de42391`.
Base: `9b76ed68b477b75832a59ee6f256effb5f19a0dd`.

- Training official dates: 2025-04-21 through 2025-04-27 inclusive.
- Cutoff: 2025-04-30T00:00:00Z, strictly after supported training completion.
- Evaluation official date: 2025-05-01; retain every supplied schedule row.
- Savant lineage extract: 2025-04-21, regular season, all teams, pitch detail.
- No date choice based on scores, model output or source success; no substitute
  date if acquisition fails.

Retain exact MLB StatsAPI v1 daily schedule response bytes (sportId=1,
hydrate=linescore) for all eight dates, plus v1.1 game feeds for training IDs.
Retain URL, local retrieval time, response headers, byte count and SHA-256.
Feed/schedule game IDs, numeric team IDs, official dates, starts, final status
and scores must agree. Train only regular-season finals with final play completion
strictly before cutoff; exclude resumed/suspended, conflicting or unsupported
cases. Preserve all training refusals. Verify final-play score and chronology.

Frozen Elo: initial 1500 for every team, K=20, scale=400, no home offset;
update once per accepted game in completion-time order, numeric game ID tie break.
Require one accepted game for both teams to predict. Freeze ratings at cutoff.
Empirical **home** probability = (training home wins + 1)/(accepted games + 2),
unavailable for an empty training set. Away probability is its complement. This
clarifies the draft's away-oriented wording without changing its arithmetic.
No evaluation results enter either baseline; no scoring, tuning or market inputs.

Use Savant game_pk/batter/pitcher numeric IDs, official date and team abbreviations
to corroborate training feeds and their player IDs. Retain pitch row pointers and
numeric pitch coordinates for lineage only; null coordinates remain null.
No Savant-derived feature enters a prediction. Source field reference:
https://baseballsavant.mlb.com/csv-docs (game_pk, batter, pitcher, plate_x, plate_z).

This is a new standalone research adapter. Existing synthetic-only benchmark,
historical admission, prospective refusal, runtime and policy contracts stay in
force. Historical completion is supported by MLB's current play-event records;
the fetched bytes are not an archived pre-cutoff version. Revisions cannot be
ruled out. Outputs are reconstructed probabilities, never original pregame
predictions or independently authenticated as-of observations. Actual historical
schedule completeness/corrections are not proven by a current daily census.
Standings, expected stats, season aggregates, starters, injuries, lineups, weather,
odds and every other unsupported historical feature are excluded.

Acceptance: retained source bytes, all source-census evaluation rows, explicit
missing/refused cases, auditable joins and feature lineage, byte-identical offline
replay, focused adversarial checks and full package tests. Report partial failure
as a concrete blocker; no synthetic substitution. No performance/model-improvement
claim, bets, runtime activation or deployment. Jerry alone merges.
