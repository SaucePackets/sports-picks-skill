# MLB Pick Workflow

## Core MLB Lens

Baseball is not just starter vs starter. And it is not just record vs record.

Treat each game as five layers — in this order:
1. **Current team form** (last 5-7 games — run scoring, wins, trends)
2. **Starter quality + current form** (last 1-2 starts, not career ERA)
3. **Bullpen quality + availability**
4. **Lineup quality + actual context** (injuries, cold bats, platoon)
5. **Market price**

Short version:
- Who is hitting right now?
- Who has the better starter today?
- Which bullpen looks cleaner?
- Does the current number still make sense?

A favorite can be the most likely winner and still be a bad bet if the price is too expensive — especially when their offense is cold.

---

## Data Pull Order (Follow This Exactly)

### Step 1 — Current form (ALWAYS first)
Pull the last 7 results for both teams from the scoreboard as the default baseline:
- How many runs are they scoring per game recently?
- How many are they allowing?
- Are they winning? Losing streaks?
- Is the offense active or cold?

Use the last 5 only when it materially changes the thesis:
- the team has clearly heated up or gone cold inside the last week
- one blowup game is distorting the 7-game view
- you need a freshness check on whether the current run environment is accelerating or fading

Do not force a separate "temperature" section if it does not change the read.

**If a team is averaging <3 runs/game over the last 5 games, do not lay heavy juice on them. Full stop.**

```python
# ESPN scoreboard — pull last 7 days
url = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard'
# Then for team-specific recent results:
url = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{team_id}/schedule?season=2026'
```

### Step 2 — Depth charts (roster truth)
```bash
# ESPN depth chart API
url = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{tid}/depthchart'
```
Never name players from memory — rosters change every offseason.

### Step 3 — Probable starters + recent SP form
```python
# ESPN game summary for probables
url = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={game_id}'
# → header.competitions[0].competitors[].probables[0].athlete.displayName
```
Do NOT assume depth chart #1 = today's starter. Always fetch the `probables` field.

Hermes/ESPN data note:
- for detailed player-level starter and bullpen review, prefer the raw ESPN summary endpoint directly (`site.api.espn.com/.../summary?event=`)
- in current Hermes runs, `sports-skills mlb get_game_summary` can be lossy for some boxscore player data, while raw ESPN `boxscore.players` often contains the full pitcher lines needed for deeper analysis
- when the CLI summary and raw ESPN summary disagree, trust the raw ESPN summary for pitcher-level game logs
- Bullpen workload and starter last-start extraction should come from `boxscore.players`, not just `boxscore.teams` totals.

**For each starter, check:**
- Last 1-2 starts (runs allowed, innings, command)
- ERA is noisy early season — actual recent outings matter more
- Career stats vs opponent: directional only, discount for current form

### Step 4 — Team stats (current season)
```python
url = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/teams/{tid}/statistics'
```
If <10 games, pull 2025 + 2024 as baseline and flag it explicitly: *"Early season — using prior year baseline."*

### Step 5 — Injury report
```python
url = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries'
```
Flag missing SPs, closers, key relievers, core lineup bats.

### Step 6 — Live sportsbook odds (ESPN/DraftKings)
```python
# ESPN game summary includes DraftKings odds in pickcenter
url = 'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?event={game_id}'
# → pickcenter[0].moneyline.away.close.odds (e.g., '+159')
# → pickcenter[0].moneyline.home.close.odds (e.g., '-194')
# → pickcenter[0].details (e.g., 'LAD -194')
```
This is the PRIMARY price source. Use this for all edge calculations.

### Step 7 — Markets matching layer
Use `openclaw-imports/markets` when you need to:
- match the ESPN event to available exchange contracts
- compare sportsbook odds against exchange probabilities
- quickly see whether there is even a clean market for this game

If `markets` returns no clean match, do not force one. Move on.

### Step 8 — Kalshi (supplementary only)
Use `openclaw-imports/kalshi` only as a supplementary exchange check.

**Note:** if Kalshi does not surface a clean same-game market, do not use futures or unrelated contracts as a substitute. Primary line stays ESPN/DraftKings unless the exchange contract clearly matches the exact game.

### Step 9 — Current price evaluation
Always state:
- Current line from ESPN/DraftKings
- Bettable-to price or clear pass point
- Whether `markets` found a clean exchange match or not
- Whether Kalshi is exact-game context or just non-matching noise

Use fair probability / implied probability math when it genuinely helps.
Do not force fake precision when the cleaner read is simply:
- playable at this number
- good only to a certain threshold
- pass if the line gets more expensive
- pass if exchange data does not cleanly map to the game

---

## What to Weight

### Current Team Form (highest weight)
- Is the offense actually scoring? Check last 5 games.
- Is there a scoring trend (heating up, cooling off, flat)?
- Heavy juice + cold offense = almost always a bad bet, regardless of roster quality
- Hot offense alone is not enough to justify a favorite pick. If the case is built mainly on bats, verify the run-prevention side harder before logging it as official.
- If the handicap starts with fading a cold offense, ask whether the fade is stale.
- Reset triggers include: a losing streak just ended, a key bat returned, the lineup shape materially changed, or the market is moving toward the supposedly cold team.
- If a reset trigger exists, the fade needs another real support layer behind it: hot bats on my side, elite/stable starter floor, or clearly cleaner bullpen/run-prevention support.
- Do not fade yesterday's version of a team if the current series shape suggests the offense may already be waking up.

### Starting Pitchers (weight current form, not reputation)
- Last 2 starts: runs allowed, innings pitched, walks
- Do not let one ugly recent outing erase a larger team-form edge by itself; ask whether it reflects a real collapse or just one blowup in an otherwise acceptable profile
- Is the listed probable actually expected to carry the game, or is this likely a short-leash / opener / piggyback setup?
- If the opponent's run-prevention path is really a multi-arm game rather than one weak starter, price the whole early-to-middle innings path instead of dismissing them by the listed probable alone
- Is the ERA from early starts or is this a late-season sample?
- Check for: injury return, times-through-the-order risk, manager leash tendencies
- Career stats vs specific opponent: useful signal, but discount 30-50% for current form divergence
- **Do not let a famous name override a cold recent trend**
- But do not dismiss a real starter gap as mere name tax.
- Always ask: which team is more likely to win the starter portion of the game, and by how much?
- If you are backing a team with the weaker starter, the rest of the case must be strong enough to overcome that early-game risk.
- If the opposing starter has a clearly superior current-season profile and your side's starter lacks a stable recent-workload / quality-start shape, do not log it as an official pick unless the team-form edge is overwhelming.
- Treat command volatility as starter-floor risk, not a minor stat-line blemish. If a favorite's starter can lose the zone early and break the handicap in the first trip or two through the order, downgrade to pass unless the bullpen/run-prevention backup is clearly strong.
- If the underdog has the better starter edge and the offenses are close enough, treat that as a serious signal, not a side note.

### Bullpen
Casual bettors underweight this constantly.

Bullpen is not just a side note. It is part of the team's full win path.

Treat bullpen as a **proxy availability check**, not a claim of perfect certainty.
In current Hermes MLB work, bullpen should usually be a supporting input, not the main handicap, unless the workload picture is extremely lopsided and clean.

Goal:
- identify the relievers most likely to matter late
- estimate whether they are fresh, somewhat taxed, or likely limited today
- use that as a supporting factor in the pick, not the whole handicap

Simple method:
- use MLB StatsAPI boxscores for the team's last **3-4 completed games**
- treat the **first listed pitcher** for the team as the starter
- treat all later pitchers as relievers used that game
- for each reliever, track:
  - appearances in last 3-4 games
  - total recent pitches
  - last used date

Simple availability tags:
- **available**
  - 1 recent appearance and under ~25 pitches total
- **maybe limited**
  - 2 appearances in last 3-4 games, or ~25-44 pitches total
- **likely limited**
  - 3 appearances in last 4 games, or 45+ recent pitches total, or back-to-back usage with one heavier outing

How to talk about it:
- it is fine to describe a bullpen as **clean**, **mixed**, or **showing red flags**
- it is fine to say one side's late-inning group looks **cleaner** than the other
- the goal is not to predict the exact manager decision, just to spot likely fatigue and late-inning stability

How to write it in the handicap:
- **Bullpen edge** — opponent's likely late-inning arms look more taxed
- **Bullpen concern** — my side's key recent relievers have heavier recent use
- **Bullpen uncertain** — role/usage picture is too muddy to trust

Important:
- do **not** pretend we know the exact closer decision unless directly verified
- do **not** let bullpen proxy override a much stronger starter/price/form read by itself
- if bullpen data is incomplete, say so explicitly instead of inventing certainty

### Lineup
- Actual batting-order quality (from depth chart + injury report)
- Handedness / platoon context if relevant
- Cold bats are real — check if key hitters are struggling

### Park / Weather
Treat weather as a real handicap input, not an afterthought.

Always check it for MLB.
Use the dedicated `weather` skill path first when weather is needed for analysis.
If the game is in a dome or weather is otherwise not relevant, say that explicitly.
Do not skip the step silently.
Do not pad the analysis with weather if it does not materially affect the handicap.

Check:
- Hitter-friendly vs pitcher-friendly park context
- Temperature (cold can suppress offense)
- Wind direction and speed
- Fly-ball vs ground-ball pitcher fit
- Rain / delay risk that could shorten a starter outing and force earlier bullpen usage

Always ask whether weather helps or hurts the stated win path for each side.

---

## Market / Price Protocol

Every MLB pick must answer:
- What is the current price?
- What is the worst number we would still take?
- Why do I actually believe this team wins?
- What is this team's full win path through starter, bullpen, offense, and weather/park context?
- What is the other team's full win path through starter, bullpen, offense, and weather/park context?
- What would make this a pass?

If you cannot answer those cleanly, do not force a pick.

For this workflow, official picks are not just "best bet" abstractions. They are confidence picks on teams I actually believe win. Price still matters, but price alone does not justify an official pick.

When a dog is under consideration, ask this plainly:
- do I actually think the dog is the better side?
- or do I just think the price is attractive?

Only the first case should push toward an official pick.

Do not confuse:
- **most likely winner** with **best bet**
- **good baseball take** with **positive-EV wager**
- **a live or cheap dog** with an official confidence pick

---

## Early Season Protocol (<10 games)

- Pull 2025 + 2024 team stats as primary baseline
- Weight recent game-by-game form more than season record
- Flag explicitly: "Early season — using prior year baseline"
- Downgrade confidence if the case depends on small-sample record noise
- Injury report becomes higher-weight factor

---

## Output Format

Default to the main skill's tighter official-picks format.

Rules:
- only give picks you would actually log as official
- if conviction is not real, output a pass
- no unofficial lean/value buckets unless the user explicitly asks
- prefer 1-3 actual picks max, sometimes zero

Use this structure:

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
• playable to / pass above when relevant

The question:
One short sentence on what actually decides whether this is a bet.

───

⛔ Pass: [matchup]

One or two short reasons.
```

Use a more structured block only when the user asks for deeper price math or a more formal breakdown.

## Second Pass (Optional Depth Layer)

Use a second pass when the first read is close, when conviction is borderline, or when the user wants a deeper explanation.

If the user asks for a deeper analysis, explicitly pull and report these when available:
- extra-base-hit profile
- stranded runners / conversion profile
- how runs were created
- scoring flow / inning distribution
- whether the final score hides or flatters the true game shape

Useful second-pass questions:
- Is this offense creating damage or just empty traffic?
- Is the opponent allowing hard damage or mostly scattered baserunners?
- Are runs coming in one fluky burst, or does the scoring profile support the team quality read?
- Does the final score hide a closer or less competitive game shape?

This second pass is for reinforcement and context. It should not replace the core first-pass inputs of form, starter, bullpen, weather, and price.

---

## Picks Record Protocol

When asked about or referencing the current picks record, always read the installed workflow's `.picks/INDEX.md` first. Never state W/L record from memory.

---

## Post-Game Reflection (Required)

After every settled pick, log the review in the installed workflow's `.picks/REFLECTIONS.md` and keep recurring rules in `.picks/PROCESS.md`.

Reflection prompts:
1. What decided the game?
2. Was the data available to catch it?
3. Bad bet or bad result?
4. What changes going forward?

Known recurring failure modes to watch for:
- Cold offense + heavy juice (check run scoring trend FIRST)
- Reputation bias (career ERA, famous roster — discount for current form)
- Career stats vs opponent overstated (good signal, not standalone edge)
- Early season record noise (game-by-game form beats W-L through 10 games)
