# Sports Picks Process and Settlement

Load this when settling picks, reflecting on wins/losses, auditing the record, or updating `.picks/` files.

---

## Source of Truth

Use the installed workflow's `.picks/` directory as the source of truth:

- `.picks/PROCESS.md` — current process + hard rules + recurring failure patterns
- `.picks/REFLECTIONS.md` — post-game review log
- `.picks/INDEX.md` — running pick history and W/L record

If chat analysis, memory, or a stray topic summary conflicts with `.picks/INDEX.md`, verify and then update `.picks/INDEX.md` so official card and official record stay aligned.

When backfilling picks into another system, reconcile against `.picks/INDEX.md` first. `.picks/REFLECTIONS.md` is post-game detail only and can be incomplete; use it to enrich rows after the index count/tally matches.

Before approving a backfill, compare parsed rows against the index tally, current/archive scope, official vs live type, W/L/Pending counts, and duplicate keys such as date + pick + line + result. If the running tally disagrees with the rows, fix the ledger first and rerun the dry-run; do not import from an impossible tally. Do not import archive rows into the active record until archive/current totals are reconciled.

Do not infer official picks from broad slate analysis after the fact. Log only picks that were actually locked as the card.

---

## Optional Database-Backed Pick Storage

Use this section only when the runtime has a real database-backed picks table or API. The flat-file `.picks/INDEX.md` workflow remains the portable fallback and reconciliation source unless the user explicitly names the database as canonical.

Recommended table/object boundary:

```text
pick_analyses = full official-card artifacts, prices, result, postgame reflection, provenance
memories      = durable policies, workflow rules, recurring lessons, and preferences
```

Do not store full cards, box-score reviews, or per-game scouting dumps as generic memories. Those belong in the picks table/object store. Memory can store durable policy like “only confident winners” or “bullpen availability must be checked,” not every game card.

Minimum database fields for serious use:
- pick identity: sport, league, game/date, teams, pick side, price, stake, confidence, verdict
- settlement: result, postgame reflection, updated timestamp
- source provenance: source shell/app, source agent, persona id, source session/excerpt
- updater provenance: updated-by shell/app, updated-by agent, updated-by persona id
- audit scope: workspace, namespace, visibility/access policy, status

Database workflow:
1. Produce the official card only after the Runtime Lock Gate passes.
2. Write the full card to the picks table/object store, not generic memory.
3. Stamp `source_agent` / `persona_id` when creating the row.
4. When settling, update the same row with result/reflection and stamp `updated_by_agent`.
5. Filter record views by the same scope used for list views. If `/picks?source_agent=...` filters cards, `/picks/record?source_agent=...` must use the same filter or the record lies.
6. Keep `All agents` as an unfiltered view; agent-specific views should only include rows with that persisted provenance receipt.
7. If the UI/backend crosses namespaces, ensure visibility/scope allows the reader to see the row. Shared dashboards may need `visibility="shared"` instead of a private namespace.
8. Reconcile backfills against `.picks/INDEX.md` first, then enrich from reflections or database detail.

Agent/Telegram save seam:
- Prefer a small raw-card save path over a giant form: send the locked official-card text plus optional game metadata, parse only enough identity fields to populate the domain row, and preserve the full text unchanged in the pick-analysis object.
- Refuse to save cards whose parsed official line is `PASS` or otherwise no-pick. Analysis can exist without becoming an official database row.
- Treat the first bullet under `Official card right now` as the official pick line when using the standard output template: `[Team/side] ([price]) — [confidence]`.
- Store parser provenance in structured metadata when available, e.g. `analysis_json.raw_text`, `analysis_json.source="official_card_text"`, `metadata_json.save_trigger="official_card_text"`.
- Return the saved pick id and provenance receipt so the agent can confirm exactly what was locked.

Postgame rule for database users: settlement is not complete until both the result and reflection are persisted on the domain row. If the reflection creates a durable rule, save that rule separately as memory or update this skill/process file.

---

## Post-Game Reflection Loop

After every pick settles — win or loss — update `.picks/INDEX.md` and `.picks/REFLECTIONS.md`.

A loss is not fully processed when the index is updated. A loss is closed only after the verified reflection is written to `.picks/REFLECTIONS.md`.

Questions to answer:

1. What was my stated edge thesis?
2. What was the full pregame win path for both teams?
3. What actually decided the game?
4. Was the data available to catch what went wrong — did I look?
5. Did I lean on reputation, price, or one narrow angle instead of the full picture?
6. Was this a bad bet or a bad result?
7. What rubric change, if any, prevents this mistake again?

---

## Loss Reflection Procedure

When reflecting on a loss, do not rely on chat recollection alone.

1. Pull the actual final score and inning-by-inning scoring flow.
2. Pull the box score and review team hits/runs/errors.
3. Review the starter/primary-player line for the picked team.
4. Review bullpen/late-game lines for the picked team.
5. Identify exactly when the game swung and who allowed the damage.
6. Compare that to the stated edge thesis from the original pick.
7. Decide whether the miss was offensive read, starter read, bullpen/run-prevention read, price discipline, or variance.
8. Write the reflection from verified data, then extract the durable process lesson.

If box score / play-by-play review changes the initial impression, trust verified game data over memory.

---

## Wins

For wins, still ask:

- Did the reasoning hold up, or did we get lucky at a bad number?
- Did we beat or miss the market/closing number?
- Was this good process or just a good result?

Pull the box score anyway if the win was one-run, extra innings, overtime, or tighter than the pregame framing implied.

Winning despite bad reasoning is still bad process.

---

## Result Settlement

Whenever a pick moves from `Pending` to `W`, `L`, or `Scratched`, update `.picks/INDEX.md` so these stay correct together:

- row result
- running tally
- current streaks
- notes / scratch reason when relevant

Streak rules:

- official streaks count only `Official` picks
- live streaks count only `Live` picks
- compute streaks from the dated rows using the most recent uninterrupted sequence for that pick type
- scratches are excluded from W/L/Pending tally and streaks
- do not leave streaks stale after settling a result

---

## Pattern Log

Track recurring failure modes in `.picks/PROCESS.md`.

Current known patterns:

- **Reputation bias:** picking famous teams/pitchers on name value, not current evidence
- **Cold offense + heavy juice:** laying -170+ on a team scoring <3 runs/game is almost always wrong
- **Career stats vs specific opponent:** useful directional signal, not a standalone edge; discount for current form
- **Early season noise:** records and ERA through first 10 games are unreliable; weight game-by-game form instead

If the same mistake appears twice, promote it to a permanent rule.
