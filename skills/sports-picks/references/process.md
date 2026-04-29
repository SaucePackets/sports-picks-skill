# Sports Picks Process and Settlement

Load this when settling picks, reflecting on wins/losses, auditing the record, or updating `.picks/` files.

---

## Source of Truth

Use the installed workflow's `.picks/` directory as the source of truth:

- `.picks/PROCESS.md` — current process + hard rules + recurring failure patterns
- `.picks/REFLECTIONS.md` — post-game review log
- `.picks/INDEX.md` — running pick history and W/L record

If chat analysis, memory, or a stray topic summary conflicts with `.picks/INDEX.md`, verify and then update `.picks/INDEX.md` so official card and official record stay aligned.

Do not infer official picks from broad slate analysis after the fact. Log only picks that were actually locked as the card.

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
