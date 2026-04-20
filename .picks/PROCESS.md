# Lucy Picks — Process

## Core objective
Only log official picks I actually feel confident making.

This record exists for two purposes:
- keep an accurate score on official picks
- learn from the data so the process gets sharper and transfers to other data-analysis skills

## Official pick rule
A pick is official only when I would personally stand behind it as a confident edge play.

That means:
- edge first, not just likely winner
- current price matters
- mispricing matters
- fewer picks is better than forced picks
- pass when the number is bad, the edge is weak, or the case is mostly reputation

Do not log every lean. Do not log every game discussed. Log only the actual card.

## Source of truth rule
`.picks/INDEX.md` is the single source of truth for official picks and record.

The moment an official card is locked, log it there immediately.
If chat analysis and the index disagree, the index must be corrected to reflect the real official card.

## Loss closure rule
A loss is not closed when the result is marked `L` in `.picks/INDEX.md`.
A loss is closed only after a verified reflection is logged in `.picks/REFLECTIONS.md`.

Required closure flow for losses:
1. Mark the pick result as `L` in `.picks/INDEX.md`
2. Update running tally + current streaks in `.picks/INDEX.md`
3. Review the actual game data
4. Log the reflection in `.picks/REFLECTIONS.md`
5. Extract any durable lesson into process/skill rules when needed

Do not treat a loss as fully processed until all five steps are done.

## Streak update rule
Whenever any logged pick result changes from `Pending` to `W` or `L`, also update:
- `## Running Tally`
- `## Current Streaks`

Streak rules:
- official streaks count only rows with `Pick Type = Official`
- live streaks count only rows with `Pick Type = Live`
- compute streaks by date order using the most recent uninterrupted sequence of wins/losses within that pick type
- do not leave streaks stale after settling a result

## Current working style
Use the tighter pick format Jerry prefers:
- only what sticks out
- only confident picks
- direct rationale
- explicit price sensitivity
- structured output for easier review and postgame reflection

## Favorite guardrail
If a favorite pick is being justified mainly by a hot offense, do not make it official unless the run-prevention side also checks out.

That means verifying:
- starter current form
- bullpen trust level / recent usage
- whether the opponent is more live offensively than the surface read suggests

Hot bats can create interest. They do not, by themselves, create an official favorite pick.

## Close-game guardrail
Do not let a one-run final score trick the review.

A close final can still hide:
- a weak offensive showing
- a fragile late-inning path
- a bullpen crack that was part of the handicap all along
- a game where one side kept creating pressure even if the scoreboard stayed tight

When reviewing close MLB games, always check:
- inning-by-inning scoring flow
- hits and total bases
- left on base
- how each run was created
- which relievers allowed the swing innings

A "tough extra-innings loss" is not automatically a bad-beat result. Sometimes it is just a thin-margin game where the bullpen risk or weak offense showed up exactly the way it could have.

This is the picks-specific process file.
Use this instead of general `.learnings/` for betting workflow improvements.

## Core Principle

**Price first. Form first. Reputation never.**

Do not ask only: who is more likely to win?
Ask:
- what is each team doing right now?
- where is the actual edge?
- what number is still bettable?
- when is this a pass?

A good team can still be a bad bet.
A famous pitcher can still be overpriced.
A cold offense is not worth laying heavy juice on.

---

## Required Pre-Pick Checklist

Before every pick:

1. **Current form first**
   - Pull last 5-7 games for both teams
   - Check run scoring trend
   - Check whether offense is hot, cold, or neutral

2. **Probable starters**
   - Confirm via ESPN `summary?event=<id>` probables field
   - Never assume from memory or rotation order

3. **Starter current form**
   - Last 1-2 starts
   - Runs allowed, innings, walks, command
   - Do not over-anchor on career ERA or name value

4. **Bullpen context**
   - Availability / fatigue if known
   - If unknown, say uncertainty exists

5. **Lineup / injury context**
   - Current roster truth only
   - Key bats out? cold lineup? platoon issue?

6. **Market price**
   - Current line
   - Bettable-to line
   - Pass point

7. **Polymarket / market sanity check**
   - If available, compare
   - If missing, say so explicitly

---

## Hard Pass Rules

Pass when:
- current price is worse than bettable-to price
- case depends mostly on reputation, not evidence
- team is averaging **<3 runs/game over last 5** and you're laying heavy juice
- edge is weak and you cannot explain it clearly
- market sharply disagrees and you cannot explain why
- you are chasing a moved number late

**No pick is better than a bad pick.**

---

## Weighting Rules

### Highest weight
- recent run scoring
- current starter form
- current price

### Medium weight
- bullpen context
- lineup / injury context
- home/away splits

### Lower weight
- season record in first 10 games
- career stats vs opponent
- team reputation / star names

---

## Known Failure Patterns

### 1. Cold offense + heavy juice
Laying -150 or worse on a team averaging under 3 runs/game recently.

**Fix:** hard pass.

### 2. Reputation bias
Picking famous teams/pitchers because they are supposed to be good.

**Fix:** current form overrides brand.

### 3. Career stats vs opponent overstated
Example: pitcher is 8-1 vs a team historically, but current form/offense context says otherwise.

**Fix:** treat as directional note only. Discount heavily if current form diverges.

### 4. Early season record noise
4-1 vs 2-3 records can be misleading in first week.

**Fix:** game-by-game run scoring matters more than raw record.

---

## Post-Game Reflection Loop

After every settled pick, log to `.picks/REFLECTIONS.md`.

Answer:
1. What was the edge thesis?
2. What actually decided the game?
3. Was the data available to catch it?
4. Bad bet or bad result?
5. What changes going forward?

If the same mistake happens twice, promote it into this file as a permanent rule.

## Post-Game Analysis Section

Use this when we want to study what a game taught us, even if it was a win.
This is separate from the loss reflection loop.

Post-game analysis is especially useful for:
- one-run games
- extra-inning games
- games where the final score may hide the true shape
- wins where the thesis needs to be validated, not just celebrated

Goal:
- understand what we got right
- understand what mattered more than expected
- spot useful context that may help future reads

When asked for post-game analysis, pull and review:
1. final score
2. inning-by-inning scoring flow
3. team batting lines (runs, hits, walks, strikeouts, left on base, extra-base hits)
4. starter lines
5. bullpen lines
6. major scoring plays / swing moments

Questions to answer:
1. What part of the pregame thesis held up?
2. Did the starter edge matter, and how much?
3. Did the bullpen path matter, and how much?
4. Did the offense create real damage or just traffic?
5. Did the final score flatter or hide the true game shape?
6. Was the late-inning outcome a random swing, or a foreseeable part of the win path?
7. Is there any useful second-pass lesson to reuse later?

Do not automatically turn every post-game analysis into a new hard rule.
Use it as a learning layer unless a pattern clearly repeats.

---

## Hard Rule: ESPN schedule is the source of truth for today's games

**Never use Polymarket series markets to identify today's matchups.**

Polymarket markets span multi-day series and do not map to individual daily games.

**Correct workflow:**
1. Pull today's scoreboard from ESPN first → get exact matchups + game IDs
2. Pull starters via ESPN summary for those specific game IDs
3. Pull form via ESPN team schedule for those specific teams
4. Then search Polymarket for matching markets — treat as supplementary signal only
5. If Polymarket market title doesn't match today's ESPN game exactly, note the mismatch and don't use it as the primary price source

**Wrong workflow (caught 2026-04-08):**
- Cross-referencing Polymarket series titles against the wrong day's schedule
- Reporting WSH@PIT and LAA@NYY as today's games when ESPN showed SD@PIT and ATH@NYY

---

## New Hard Rule: Bullpen Check is Mandatory (added 2026-04-08)

Two losses in one day to bullpen collapses (PIT and LAD). Both starters were fine. Both picks were right on paper. Bullpen killed both.

**Before every pick, answer these explicitly:**
- Who are the top 2 relievers for the favored team?
- Have any key relievers been used heavily in the last 2 days?
- Is the closer available?
- If the starter exits early, what does the bridge to the closer look like?

If the answer to any of these is "unknown" — that is a confidence penalty, not a footnote.

**Rule:** Bullpen unknown + starter-dependent pick = cap at Low confidence. Low confidence picks only go at clear plus-money prices.

**Rule:** Ceiling price (-150 or worse) requires HIGH confidence, not Med-High. Thin edge at max price is a pass.

---

## Hard Rule: Always Pull Game Stats After Every Loss (added 2026-04-08)

After every settled loss, pull the full boxscore via ESPN summary API before writing the reflection.

**What to check:**
- Pitching lines for both teams (IP, H, R, ER, BB, K, ERA)
- Who pitched after the starter — identify exactly who blew it
- Whether the starter held up or was the problem
- Score progression if possible (when did it break?)

**Why:** Today's PIT loss showed Keller was dominant (6 IP, 0 ER). The collapse was Lawrence (9.53 ERA) in relief — data that was available pre-game. Without pulling the boxscore, the reflection is guesswork.

**Workflow:**
```
ESPN summary?event=<game_id> → boxscore.players[].statistics[group 1] = pitching lines
```

No reflection is complete without the actual game data. No exceptions.

---

## Hard Rule: Explicit Starter Check (added 2026-04-12)

**Failure:** Recommended LAD -131 without registering deGrom was pitching for TEX.

**Fix:** Before any pick, explicitly state:
1. Both starting pitchers by name
2. Their last 1-2 starts with stats
3. Whether either is an elite arm (deGrom, Scherzer, Cole, Glasnow, etc.)
4. How the SP matchup affects confidence

**DO NOT skip this step.** Form + price means nothing if the opposing starter is elite and your starter is inexperienced.

**Today's lesson:** LAD 6-1 form + -131 price looked like value. deGrom pitching for TEX made it a pass. The pick was wrong because I didn't actually *read* the starter names I pulled.
