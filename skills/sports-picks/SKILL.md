---
name: sports-picks
description: >
  Data-driven official game picks for MLB, NFL, NBA, and NHL. Pull team form, injuries, starting pitcher/goalie matchups, and current market prices to decide whether there is a real pick or a pass. Use sportsbook lines as the main pricing signal and treat market odds as a sanity check for whether a side is too expensive or mispriced. The goal is learning to analyze data and keep score only on official picks with real conviction, not generating extra categories like value plays.

  Use when: user asks "who do you have winning", "make a pick", "who should I bet on", "who's favored", wants an official pick record, or any game prediction question for MLB, NFL, NBA, or NHL.
  Don't use when: user only wants raw odds (use polymarket/kalshi directly), live scores (use sport-specific data skill), or news (use sports-news).
---

# Sports Picks

Produce data-backed game picks. Always pull multiple data layers — never guess from memory.

---

## Core Betting Principle

**Official picks only. Confidence first. Price matters, but price alone does not create an official pick. Form first, reputation never.**

Do not frame a game as "who has the best number?" in isolation. Frame it as:
- What team do I actually believe wins?
- Why do I believe they win based on current form and matchup?
- Is the current price still acceptable?
- When is this a pass?

A team can be the better price and still fail the official-pick bar.
Official picks are for sides I genuinely believe win, with price acting as a filter, not the whole reason for the pick.
A famous team with a great pitcher is still a bad bet if their offense is cold and the juice is -194.

**Hard output rule:** if conviction is not real, do not make a pick.
If the edge is thin, the number is bad, the data is incomplete, confidence does not clear the bar, or the case is mostly "the dog is live at this number" without a real belief they win, output a pass and nothing else. The goal is to learn from the data, make official picks only when they are truly confidence picks, and track judgment quality over time.

Do not create side categories like "value plays," "leans," or other unofficial buckets unless the user explicitly asks for them. Default to one of two outputs only:
- official pick
- pass

A conditional official pick is allowed, but only when it is concrete.
That means it must include an exact trigger such as:
- pregame only to a stated price
- pass if it moves beyond a stated number
- live only if the team falls behind early and the line improves into a stated range

Good conditional pick:
- Dodgers ML only at -150 or better pregame
- otherwise pass pregame, or watch live if they go down early and the number improves toward -125 / near even

Bad conditional pick:
- I kind of like the Dodgers if the number gets better
- maybe live if the spot looks good

If the trigger is vague, it is not a real recommendation.

**Dog rule:** underdogs are allowed as official picks only when both are true:
1. I actually think the dog is the better side and more likely winner
2. the market price is also favorable or clearly mispriced

Do not pick a dog just because:
- they are live
- the plus money looks attractive
- the game feels close

Pick the dog only when the matchup gives a real reason to believe the market may have the teams ordered wrong.

That can mean:
- the dog has the better starter matchup
- the offenses are close, but the dog has the cleaner full-game path
- the favorite is being priced on name value more than current form
- the dog is better positioned to win the late innings

If I do not actually believe the dog is the better side, it is not an official pick.

---

## ⚠️ VERIFY BEFORE YOU SPEAK — NO EXCEPTIONS

This applies during picks AND during live game conversation:

| Claim | Tool to use |
|-------|------------|
| Team records / series standings | ESPN scoreboard API |
| Current score / game status | ESPN scoreboard API |
| Current roster / lineup | ESPN depth chart API |
| Today's starting pitcher | ESPN game summary `probables` field |
| Recent run scoring / team form | ESPN scoreboard last 5-7 games |
| My current picks record | Read `.picks/INDEX.md` |

**Never state any of the above from memory or conversation context.** Check the tool first, then speak.

---

## Core Principle

**Current form first. Prior season(s) as baseline only when current data is thin.**

Early in a season (<10 games for MLB): weight recent games heavily and flag it explicitly.

**Reputation is noise. What the team is doing THIS WEEK is signal.**

---

## Sport Workflows

Load the relevant reference file before building the pick:

| Sport | Reference | Key factor |
|-------|-----------|------------|
| MLB | `references/mlb.md` | SP matchup + current team form |
| NFL | `references/nfl.md` | QB + injury report |
| NBA | `references/nba.md` | Rest days + star availability |
| NHL | `references/nhl.md` | Starting goalie matchup |

---

## Standard Pick Workflow

1. Identify sport and teams
2. Read the sport-specific reference file
3. Resolve team IDs
4. **Pull recent scoreboard** — last 5-7 games for both teams (run scoring trend, form)
5. **Pull depth charts** — current roster truth, not memory
6. **Confirm probable starters** via ESPN game summary `probables` field
7. Pull current season stats; if thin, pull prior season(s) as baseline
8. Pull injury report
9. Pull the current primary sportsbook/game line first
10. If useful, use Kalshi or other market views only as a supplementary exchange/sentiment check, and only when the contract cleanly maps to the exact game
11. Check the full win path for both teams before finalizing the pick:
   - starter path
   - bullpen path
   - offense/form path
   - weather/park context
   - whether the market is pricing those paths correctly

   Weather rule:
   - always check weather/park context for MLB
   - if the game is in a dome or weather is otherwise irrelevant, say that explicitly
   - if weather is meaningful, explain whether it is a mild concern or a real variance factor
   - include delay/rain risk when relevant
   - do not silently skip this step
12. State the **current price** and **bettable-to / pass price**
13. Decide whether the recommendation is:
   - a real pregame pick at the current number
   - a conditional pregame pick only to a stated threshold
   - or a pass now with a specific live-watch trigger if the price improves
14. If exchange/market context was checked, explicitly say whether it was an exact-game match, a loose sentiment signal, or unavailable
15. Synthesize — weight current form heavily, reputation lightly
16. If edge is weak, number is gone, the exchange mapping is fuzzy, the full-picture win path is incomplete, or the case relies on name value over current evidence → **pass**

### Second-pass depth layer (optional, but encouraged when useful)
Use a second pass when:
- a game is close
- conviction is almost there but not fully there
- the user asks for a deeper read
- you want to stress-test whether the first-pass edge is real

If the user asks for a deeper analysis, explicitly check and report these second-pass items when available:
- extra-base-hit / damage profile
- stranded runners / conversion profile
- how runs are being created
- scoring distribution by inning or game state
- whether the final score may be flattering or misleading

You may also include any other game-shape detail that helps explain how a team actually wins.

This is a reinforcement layer, not a replacement for the core first pass.
Core first-pass factors still come first:
- current form
- starter matchup
- bullpen
- weather/park context
- market price
- full win path

---

## Always Include in Pick Output

- **Recent form check:** default to last 7 games as the baseline; use last 5 only when it materially changes the read
- **SP current form:** last 1-2 starts, not career stats
- **Full win-path check:** how does this team actually win the game through starter, bullpen, offense, and weather/park context?
- **Weather check:** what are the conditions, what does that mean for the game, and if weather is irrelevant, did I say that clearly?
- **Underdog check when relevant:** is this dog just live, or does the matchup actually favor them enough that they should be the pick?
- **Confidence level:** High / Medium / Low
  - High: clear edge in 3+ current-data factors AND price supports the bet
  - Medium: edge in 1-2 factors, or uncertainty in one major input
  - Low: coin flip, very early season, data sparse, or price too close to fair
- **Current price + bettable-to price**
- **If conditional:** exact pregame threshold and/or exact live-watch trigger
- **Market-mapping note:** exact-game exchange match, loose sentiment only, or unavailable
- **Why this number may be wrong:** source of edge in one line
- **Flip risk:** one sentence on why the other side wins

---

## Default Output Shape

Use this as the default response format when the user asks for picks.

```text
Good data. Here's the breakdown:

───

🔵 Pick 1: [AWAY] @ [HOME] → [Side] ([Confidence])

Form:

• [Team]: last 5-7 games, avg runs or scoring trend — quick read
• [Team]: last 5-7 games, avg runs or scoring trend — quick read

SP:
[Pitcher A] vs [Pitcher B]. One or two sentences, plain English. Current-form angle first.

Bullpen check:

• [Team]: clean / mixed / red flags / not fully checked yet
• [Team]: clean / mixed / red flags / not fully checked yet

Market:

• current line or best available price
• if needed: playable to / pass above
• if not playable now: exact watch trigger for a better pregame or live entry

The question:
One short sentence on what actually decides whether this is a bet.

───

🔵 Pick 2: [AWAY] @ [HOME] → [Side] ([Confidence])

[same structure]

───

⛔ Pass: [matchup]

One or two short reasons.
```

### Style notes
- Prefer **1-3 actual picks max**. Do not spray the board.
- If only one pick is truly strong, give one pick and passes.
- If **nothing** is truly strong, give **no picks**. Just list the passes or say there is no play.
- Keep the tone conversational, sharp, and direct.
- Use the exact labels: `Form:`, `SP:`, `Bullpen check:`, `Market:`, `The question:`
- Use bullets (`•`) under Form and Bullpen check when possible for Telegram readability.
- If bullpen was not fully verified, say that directly instead of bluffing.
- If weather was checked and not relevant, say that plainly.
- If price is bad, move the game to `⛔ Pass` even if the side is likely to win.
- It is allowed to say a side is playable only to a specific number, or to say pass now / watch live if down early, but the trigger must be explicit.
- When doing a second pass, it is fine to add one short extra note on game-shape context, but do not let the answer sprawl.

### Example of the target shape

```text
Good data. Here's the breakdown:

───

🔵 Pick 1: ARI @ PHI → Diamondbacks (+117) (Medium-High)

Form:

• ARI: live dog profile, real starter edge spot
• PHI: bullpen game setup, less stable path through 9 innings

SP:
Zac Gallen vs Zach Pop. Arizona has the real starter. Philly is piecing it together. That is the core edge.

Bullpen check:

• ARI: acceptable if Gallen gives length
• PHI: automatic concern because they are asking for more outs from the pen

Market:

• PHI -132 / ARI plus money range
• +117 is a playable dog number

The question:
Can Gallen control the game long enough to force Philly's bullpen depth to matter?

───

🔵 Pick 2: HOU @ SEA → Mariners (Medium)

Form:

• SEA: offense may be waking up
• HOU: volatile scoring profile, not trustworthy inning to inning

SP:
Logan Gilbert vs Cody Bolton. Clear Seattle starter edge.

Bullpen check:

• SEA: not fully checked yet
• HOU: not fully checked yet

Market:

• SEA -175
• right side, worse number

The question:
Is the starter edge big enough to justify paying the tax?

───

⛔ Pass: COL @ SD

Padres are the right side. -219 is stupid.
```

---

## Conditional Pick Rules

Use a conditional pick only when the handicap is real but the entry price matters.

Allowed:
- playable pregame only to a stated number
- pass now, but watch for a better pregame number
- pass now, but watch live if an early deficit improves the price into a stated range

Not allowed:
- vague watch language
- fake precision without a real threshold
- treating a live note as an official pick when no actual trigger was given

A live-watch recommendation should still answer:
- what team I would want
- what game state likely creates the better number
- what approximate number or range makes it playable
- whether this is still only a watch, not an official pregame pick

## Pass Rules

Pass when:
- the current number is worse than the bettable-to price
- the case depends on reputation instead of current evidence
- the team is scoring fewer than 3 runs/game recently and you're laying big juice
- the matchup is close and you cannot clearly explain where the edge comes from
- the number moved and you are chasing steam late
- market data conflicts sharply with your case and you cannot explain why
- confidence is below a real-conviction threshold, even if you lean one side
- you would be making the pick mainly to have action or keep the record moving

**No pick is better than a bad pick.**
**No confidence, no pick.**

---

## Market Check (All Sports)

Use the sportsbook line as the primary pricing input.

If you use a market check beyond the sportsbook line, prefer an exact-game exchange match first. Kalshi or other exchange views are supplementary only and should never override the actual game handicap by themselves.

If no clean same-game contract exists, say so and move on. Do not force fake precision from futures, series markets, or vaguely related contracts.

If market confidence diverges significantly from your analysis, note it and explain why.

---

## Post-Game Reflection Loop (Mandatory for Every Settled Pick)

After every pick settles — win or loss — run this loop and update `.picks/INDEX.md` + `.picks/REFLECTIONS.md`.

**Loss closure rule:** a loss is not considered fully processed when the index is updated. A loss is closed only after the verified reflection is written to `.picks/REFLECTIONS.md`.

### Questions to answer:
1. **What was my stated edge thesis?**
2. **What was the full pregame win path for both teams?**
3. **What actually decided the game?** (starter, bullpen, offense, variance)
4. **Was the data available to catch what went wrong — did I look?**
5. **Did I lean on reputation, price, or one narrow angle instead of the full picture?**
6. **Was this a bad bet or a bad result?** (A -194 loss on a cold offense is a bad bet. A +123 loss on a fluky inning may be fine process.)
7. **What rubric change, if any, prevents this mistake again?**

### On losses specifically:
- Identify the single most important thing missed
- Was it a data gap (didn't check) or a weighting error (checked but misjudged)?
- Update the pick notes in INDEX.md with the key lesson
- If the same mistake appears twice → promote it to a permanent rule in this skill

### Result-settlement update rule
Whenever a pick moves from `Pending` to `W` or `L`, update `.picks/INDEX.md` so these stay correct together:
- the row result
- the running tally
- the current streaks

Streak rules:
- official streaks count only `Official` picks
- live streaks count only `Live` picks
- compute streaks from the dated rows using the most recent uninterrupted sequence for that pick type
- do not leave streaks stale after settling a result

### Loss reflection procedure (required)
When reflecting on a loss, do not rely on chat recollection alone. Walk through these steps in order:
1. Pull the actual final score and inning-by-inning scoring flow
2. Pull the box score and review team hits/runs/errors
3. Review the starter line for the picked team
4. Review the bullpen lines for the picked team
5. Identify exactly when the game swung and who allowed the damage
6. Compare that to the stated edge thesis from the original pick
7. Decide whether the miss was:
   - offensive read wrong
   - starter read wrong
   - bullpen/run-prevention read wrong
   - price/market discipline wrong
   - or mostly variance despite a sound read
8. Write the reflection from the verified game data, then extract the durable process lesson

If box score / play-by-play review changes the initial impression, trust the verified game data over memory.

### On wins:
- Did the reasoning hold up, or did we get lucky at a bad number?
- Beating the close on a winner = good process.
- Winning despite bad reasoning = still bad process.

### Pattern log:
Over time, track recurring failure modes in `.picks/PROCESS.md`. Current known patterns:
- **Reputation bias:** picking famous teams/pitchers on name value, not current evidence
- **Cold offense + heavy juice:** laying -170+ on a team scoring <3 runs/game is almost always wrong
- **Career stats vs specific opponent:** useful directional signal, not a standalone edge, discount for current form
- **Early season noise:** records and ERA through first 10 games are unreliable, weight recent game-by-game form instead

---

## Review Discipline

Separate these questions after every pick:
- Did the bet win?
- Did the reasoning hold up?
- Did we beat or miss the market/closing number?

Track process quality independently of short-term results. A 3-9 record with improving process is better than a 6-6 record with no learning.

## Picks Files

Use `.picks/` as the source of truth for betting workflow:
- `.picks/PROCESS.md` — current process + hard rules + recurring failure patterns
- `.picks/REFLECTIONS.md` — post-game review log
- `.picks/INDEX.md` — running pick history and W/L record

## Consistency Rule

If the chat analysis, a memory note, or a stray topic summary conflicts with `.picks/INDEX.md`, verify and then update `.picks/INDEX.md` so the official card and official record stay aligned.

Do not infer official picks from broad slate analysis after the fact. Log only the picks that were actually locked as the card.
