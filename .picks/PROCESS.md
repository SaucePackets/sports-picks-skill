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
Use the tighter pick format the user prefers:
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

## Official Pick Gate

Before logging any official pick, run this gate in writing, even if the final user-facing answer stays short:

- **Starter floor:** Can my side's starter survive 4-5 innings without command volatility, walk/traffic risk, weak miss-bat floor, pitch inefficiency, or HR damage breaking the handicap? If no, pass.
- **Opposing-starter shutdown path:** Can the opposing starter realistically suppress my side for 6-8 innings through command, swing-and-miss, weak contact, or workload? If yes and my edge is only medium, pass.
- **Bullpen survival:** If the likely script is close late, do I trust my side's innings 7-10 enough? If no, pass.
- **My-side red-bullpen check:** If my side's bullpen is red/taxed, is my side's edge big enough to avoid a one-to-two-run script and late bullpen dependence? If no, pass.
- **Both-bullpens red check:** If both bullpens are red/taxed, is my side's starter/offense edge strong enough to avoid making bullpen chaos the deciding factor? If no, pass.
- **Cold-fade reset:** If fading a cold offense, has that offense shown reset signs inside the series? If yes, require another strong support layer or pass.
- **Price discipline:** Is the number inside the bettable-to threshold without needing the price to create the pick? If no, pass.
- **Winner conviction:** Do I actually believe this side wins most often, not just that the number is attractive? If no, pass.

Any failed gate overrides the lean. Do not downgrade to Medium and keep it. Failed gate means pass.

## Starter-floor guardrail
If backing a favorite, the listed starter needs a believable floor to survive the first 4-5 innings without breaking the game open.

Do not treat a decent surface ERA as enough by itself.
Check for:
- walk risk / command volatility
- short recent outings or pitch-count stress
- whether the handicap collapses if the starter loses the zone early
- whether the bullpen behind him is strong enough to absorb an early exit

If the starter floor is shaky and the bullpen backup is not clearly strong, pass the favorite.

## Opposing-starter respect rule
Before backing a favorite, stress-test the opposing starter as an active win condition, not just a lesser name across from our guy.

Do not reduce the matchup to "our starter is better" if the opponent's starter has:
- recent quality-start shape
- strong command / low walk risk
- real swing-and-miss or weak-contact form
- enough workload to suppress our offense for 6-8 innings

If the opposing starter can plausibly neutralize our lineup and our side has lineup injuries, bullpen tax, or only a medium team-shape edge, downgrade to pass.

## Two-way starter enforcement check
This is not a new handicap layer; it is an enforcement step for the starter-floor and opposing-starter rules above.

Before locking any MLB official pick, explicitly answer both questions in the analysis:
- What if the opposing starter is good today?
- What if our starter struggles today?

Name the actual failure path, not a generic caveat:
- opposing starter command/swing-and-miss/workload suppresses our offense
- our starter walk risk, traffic, hard contact, short leash, or matchup issue breaks the game open

If either path is realistic enough to erase a medium edge, lower confidence or pass. If the analysis skips these questions, the pick has not cleared the official-pick gate.

## Road-dog guardrail
If backing a road underdog, do not let a generic recent-form edge do all the work when the opponent has both:
- the cleaner starter-floor edge
- a strong home-game profile

In that shape, the dog needs a real offensive matchup advantage or a clearly mispriced number.
If the favorite can control the first 5-6 innings and has been consistently stronger at home, pass the dog.

If the road dog case depends on “cleaner starter floor,” stress-test the starter's command and miss bats, not just recent ER. Against high-ceiling lineups, a low-ERA recent run can still hide walk/traffic risk that turns into one crooked inning.

## Cold-offense fade reset check
Before making an official pick mainly by fading a cold offense, check whether the cold-offense label is stale.

Reset triggers:
- a key bat returned to the lineup
- the team broke a losing streak in the previous game
- the lineup construction materially changed
- the market moved toward the supposedly cold team despite poor recent scoring
- the supposedly cold team has already produced a reset game or multiple competitive offensive outputs inside the current series

If a reset trigger exists, do not make the fade official unless my side also has at least one strong support layer:
- hot offense
- elite/stable starter floor
- clean bullpen/run-prevention backup

Do not fade yesterday's version of a team if the current series shape suggests the offense may already be waking up.
Do not double-dip a cold-offense fade across the same series once reset signs appear; require a fresh handicap, not a repeated label.

This is the picks-specific process file.
Use this instead of general `.learnings/` for betting workflow improvements.

## Core Principle

**Winner conviction first. Current form first. Price filters the pick. Reputation never.**

Do not ask only: who has the best number?
Ask:
- what is each team doing right now?
- where is the actual edge?
- who do I actually believe wins most often?
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
   - Explicitly answer: what if their starter is good today, and what if our starter struggles today?

4. **Bullpen context**
   - Availability / fatigue if known
   - If unknown, say uncertainty exists
   - If the realistic win path is close late, identify who protects innings 7-10
   - Check whether key leverage arms are injured, recently taxed, or role-uncertain

5. **Lineup / injury context**
   - Current roster truth only
   - Key bats out? cold lineup? platoon issue?

6. **Market price**
   - Current line
   - Bettable-to line
   - Pass point
   - Price can veto a pick; it cannot create one by itself

7. **Market sanity check**
   - Sportsbook line is primary
   - Kalshi / Polymarket / exchange markets are supplementary only when they cleanly map to the exact game
   - If exchange data is missing or mismatched, say so explicitly and do not use it as a primary price source

---

## Hard Pass Rules

Pass when:
- current price is worse than bettable-to price
- the pick exists mainly because the number is attractive, not because I believe the side wins most often
- case depends mostly on reputation, not evidence
- team is averaging **<3 runs/game over last 5** and you're laying heavy juice
- edge is weak and you cannot explain it clearly
- market sharply disagrees and you cannot explain why
- you are chasing a moved number late
- my side's bullpen is tagged red and the realistic win path is a one-to-two-run game, unless my side has a clear multi-run offensive/starter edge
- both bullpens are tagged red and the realistic win path is close late, unless my side has an even stronger starter/offense edge that can realistically avoid bullpen dependence
- the opposing starter has a credible shutdown/workload path and my favorite case relies mostly on recent team form or a medium offensive edge

**No pick is better than a bad pick.**

## Scratched Pick Rule
If a better critique or new information breaks an official-pick gate before game start, scratch the pick instead of defending stale analysis.

Ledger handling:
- If the pick was discussed but not logged yet, do not add it.
- If the pick is already logged, change `Result` to `Scratched`, add the reason in `Notes`, and exclude it from W/L/Pending tally and streaks.
- Do not relabel a scratched pick as a pass after the fact; keep the audit trail honest.

---

## Weighting Rules

These weights apply only after the Official Pick Gate is passed.
A failed gate is not a lower-weight concern; it is a pass.

### Highest weight
- winner conviction from current evidence
- recent run scoring / run prevention
- current starter form and floor
- opposing-starter shutdown risk

### Veto / filter weight
- current price: it can veto a pick, but it cannot create one by itself
- bettable-to threshold / pass point

### Supporting weight
- bullpen context, unless it triggers a hard-gate failure
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
6. Is there any useful second-pass lesson to reuse later?

Do not automatically turn every post-game analysis into a new hard rule.
Use it as a learning layer unless a pattern clearly repeats.

---

## Props rule — line first, role second, matchup third

Props are secondary to the main game-picks workflow. Do not surface prop plays unless the user asks for props or the slate analysis explicitly includes them.

Do not make an official prop without a verified line and price.

Before recommending a prop, verify:
- the exact prop market, line, and odds
- the player role/workload expectation
- recent form against that line, not just generic talent
- opponent tendency that directly maps to the prop
- whether one game script kills the bet

For pitcher props:
- strikeouts need opponent K tendency + pitcher workload + pitch-count leash
- earned-runs/runs allowed props need opponent damage profile, not just pitcher ERA
- hits allowed props need opponent contact/hit volume and pitcher traffic profile

If the line moves across a key threshold, re-grade the play. Example: pitcher Ks over 5.5 and over 6.5 are different bets, not the same opinion.

---

## First Hermes-native slate lessons — 2026-04-19

### CHC lesson — right winner, tighter game than expected
- The Cubs pick was directionally right, but the game finished **2-1 in 10 innings**, not as a clean separation.
- What held: Mets offense was still weak enough that Chicago could survive with only modest run support.
- What we under-modeled: the Mets had a more credible **multi-arm run-prevention path** than the listed probable alone suggested.
- Durable lesson: when the opponent is likely to patch together a real early-to-middle innings pitching path, do not handicap the game as if only the listed starter matters.
- Confidence lesson: weather or a credible piggyback / bullpen path can compress scoring and tighten variance even when the side is right.

### ARI lesson — bad bet from underweighting the starter gap
- Arizona lost **10-4** and the game was effectively dead after **Ryne Nelson allowed 8 ER in 0.1 IP**.
- The miss was not missing data. The miss was weighting.
- Kevin Gausman was not just a "name tax" spot — the starter gap was materially real.
- Durable lesson: if backing the weaker starter, the rest of the case must be overwhelming. Team-form edge alone is not enough when the opposing starter has a clearly stronger floor.

### ATL lesson — winner-first framing held up
- Atlanta won **4-2** at near pick'em.
- Grant Holmes was not dominant, but he was good enough; Painter was respected by the market more than the full-game matchup justified.
- What held: better team shape, better broader run-prevention profile, and a fair enough number.
- Durable lesson: do not let a flashier opposing starter erase a cleaner full-game team edge when the price is still near even.

### Cross-slate carry-forward rules
- Ask whether the side can still win often enough **if the opposing starter performs to profile**.
- Treat weather as a game-shape modifier: sometimes it does not flip the side, but it lowers margin for error.
- Separate **one bad recent outing** from a truly fragile starter profile.
- Distinguish between the listed probable and the opponent's real run-prevention path through the first 5-6 innings.

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

**Close-game survival check:** Apply this hardest when the projected script is close late: favorite in a one-to-two-run game, no overwhelming offensive/starter edge, and the opponent can keep it within one swing. In that shape, explicitly answer who protects innings 7-10. A starter giving 6-7 good innings is not enough if the bridge/closer path is injured, taxed, or role-uncertain.

**Rule:** Bullpen unknown + starter-dependent pick = cap at Low confidence. Low confidence picks only go at clear plus-money prices.

**Rule:** Missing/taxed leverage arms + close favorite script is a hard-gate question first. If the offensive or starter edge is not overwhelming enough to avoid bullpen dependence, pass instead of assuming the late innings hold. If the gate still passes because the side has a clear multi-run offensive/starter edge, bullpen risk becomes a confidence cap/modifier rather than an automatic pass.

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
