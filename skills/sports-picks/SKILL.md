---
name: sports-picks
description: >
  Data-driven official game picks for MLB, NFL, NBA, and NHL. Pull team form, injuries, starting pitcher/goalie matchups, and current market prices to decide whether there is a real pick or a pass. Use sportsbook lines as the main pricing signal and treat market odds as a sanity check for whether a side is too expensive or mispriced. The goal is learning to analyze data and keep score only on official picks with real conviction, not generating extra categories like value plays.

  Use when: user asks "who do you have winning", "make a pick", "who should I bet on", "who's favored", wants an official pick record, or any game prediction question for MLB, NFL, NBA, or NHL.
  Don't use when: user only wants raw odds (use kalshi/markets directly), live scores (use sport-specific data skill), or news (use sports-news).
---

# Sports Picks

Produce data-backed game picks. Always pull multiple data layers — never guess from memory.

Hermes compatibility note:
- This repo can be installed in Hermes or OpenClaw.
- Keep one canonical `.picks/` directory for the installed workflow and use it as the source of truth.
- In Hermes, prefer the imported sport-specific skills for ESPN-backed data and treat the sportsbook line as the primary price source.
- Kalshi / Polymarket / other exchange checks are supplementary only unless they map cleanly to the exact game.

---

## Core Betting Principle

**Official picks only. Confidence first. Winner first. Price matters, but price alone does not create an official pick. Form first, reputation never.**

Official-pick confidence rule:
- first decide who I actually think wins the game most often
- then check whether the current price is still worth paying
- never promote a side to an official pick mainly because the number is attractive
- if I like the price more than I like the team to win, it is a pass

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
4. **Pull recent scoreboard** — use the last 7 games as the baseline for both teams (run scoring trend, form). Check the last 5 only if it materially changes the thesis.
5. **Pull depth charts** — current roster truth, not memory
6. **Confirm probable starters** via ESPN game summary `probables` field
7. Pull current season stats; if thin, pull prior season(s) as baseline
8. Pull injury report
   - Treat injury feeds cautiously. If the feed is noisy, outdated, or unclear, say so and do not let dirty injury data fake conviction.
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
   - do not pad analysis with weather if it does not change the thesis; use it as a game-shape modifier only when it materially affects scoring environment or variance

   Starter-floor rule:
   - do not just compare the last 1-2 starts; ask whether each starter has a stable enough floor to survive the first 4-5 innings without breaking the game open
   - if backing the team with the weaker starter, and the opposing starter has the clearly stronger current-season profile, do not make it official unless the team-form edge is overwhelming and the side can still win often enough if the opposing starter performs to profile
 
   Road-dog rule:
   - do not make a road underdog official just because the recent-form differential looks cleaner on paper
   - if the opponent has both the cleaner starter-floor edge and a strong home profile, the dog needs a real offensive matchup edge or a clearly mispriced number
   - if the favorite can plausibly control the first 5-6 innings and has been materially stronger at home, pass the dog unless the matchup gives a concrete reason the market may have the teams ordered wrong
 
   Run-prevention-path rule:

   - check whether the opponent has a credible multi-arm early-to-middle innings run-prevention path (bulk reliever, piggyback, short-start bridge, rested leverage arms)
   - if they can credibly turn the game into a low-scoring grinder, lower confidence unless the offensive edge is big enough to survive that shape
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
- **Weather check:** what are the conditions, and if weather is irrelevant, did I say that clearly?
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

Default to a tighter official-card format. By default, do not surface value angles or dog angles. Name only the teams I actually feel confident win, then explain briefly why they made the card.

Preferred shape:
- opening line like `Yeah. A few stick out.`
- `Official card right now` followed by 1-3 ML picks max
- `Why they stick out` with short numbered breakdowns using actual recent-form numbers, a quick weather/park note, current line, and 2-3 bullets on why the side made the card
- `Passes / close calls` with short reasons
- `Real card` repeated at the end, plus a simple ranking if useful

Favorite or dog does not matter. Confidence does.

When the user wants a deeper official-card analysis, use this shape:
- `Yeah. Here’s the deeper pass.`
- `Official card` with ML picks and confidence labels
- one short line on any notable pass near the top if relevant
- numbered game sections with these labels in order:
  - `Form`
  - `Starter matchup`
  - `Bullpen`
  - `Weather`
  - `What held up on second pass`
  - `Verdict`
- end with `Final official card`

Deep-analysis rule: still keep it to official picks only. Passes stay separate and concise. The point is to explain why the side made the card, not to turn every game into a writeup.

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
- Prefer **1-3 official picks max**. Do not spray the slate.
- Do **not** create default value-pick or dog-pick sections. Favorite or dog does not matter; confidence does.
- If only one pick is truly strong, give one pick and passes.
- If **nothing** is truly strong, give **no picks**. Just list the passes or say there is no play.
- Keep the tone conversational, sharp, and direct.
- Default official-card style for Jerry: lead with a short `Official card right now` section listing only the confident picks, then a `Why they stick out` section, then `Passes / close calls`, and finish with a short `Real card` or ranking only if useful.
- In that default style, do **not** force the longer template labels (`Form:`, `SP:`, `Bullpen check:`, `Market:`, `The question:`) unless Jerry asks for a deeper breakdown.
- Use short bullets (`•`) for the data points that actually matter.
- The standard longer template is still available for deeper analysis, but the default should feel like a trimmed betting card, not a report.
- If bullpen was not fully verified, say that directly instead of bluffing.
- If weather was checked and not relevant, say that plainly.
- If price is bad, move the game to `⛔ Pass` even if the side is likely to win.
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

If Kalshi or another exchange market view is available, use it only as a supplementary sanity check on public/book thinking and whether the number may be mispriced or too expensive. If market confidence diverges significantly from your analysis, note it and explain why.

---

## Post-Game Reflection Loop (Mandatory for Every Settled Pick)

After every pick settles — win or loss — run this loop and update `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/INDEX.md` + `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/REFLECTIONS.md`.

**Loss closure rule:** a loss is not considered fully processed when the index is updated. A loss is closed only after the verified reflection is written to `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/REFLECTIONS.md`.

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
Whenever a pick moves from `Pending` to `W` or `L`, update `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/INDEX.md` so these stay correct together:
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
- Pull the box score anyway if the win was one-run, extra innings, or otherwise tighter than the pregame framing implied.
- Check whether the opponent's actual run-prevention path, game-state scoring flow, or weather-driven game shape made the game materially narrower than expected.
- Beating the close on a winner = good process.
- Winning despite bad reasoning = still bad process.

### Pattern log:
Over time, track recurring failure modes in `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/PROCESS.md`. Current known patterns:
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

Use `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/` as the source of truth for betting workflow:
- `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/PROCESS.md` — current process + hard rules + recurring failure patterns
- `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/REFLECTIONS.md` — post-game review log
- `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/INDEX.md` — running pick history and W/L record

## Consistency Rule

If the chat analysis, a memory note, or a stray topic summary conflicts with `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/INDEX.md`, verify and then update `/home/clawdbot/.hermes/skills/openclaw-imports/sports-picks/.picks/INDEX.md` so the official card and official record stay aligned.

Do not infer official picks from broad slate analysis after the fact. Log only the picks that were actually locked as the card.
