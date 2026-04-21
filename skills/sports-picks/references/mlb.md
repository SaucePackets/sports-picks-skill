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

Hermes / ESPN note:
- for detailed pitcher and bullpen review, prefer the raw ESPN summary endpoint directly when needed
- wrapper outputs can be lossy for pitcher-level detail
- when the wrapper and raw ESPN boxscore disagree, trust the raw ESPN summary for player pitching lines

**For each starter, check:**
- Last 1-2 starts (runs allowed, innings, command)
- ERA is noisy early season — actual recent outings matter more
- Career stats vs opponent: directional only, discount for current form
- Do not just ask who is better on paper. Ask whether each starter has a stable enough floor to survive the first 4-5 innings without breaking the handicap.

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

### Step 7 — Supplementary market check (optional)
If you want extra pricing context beyond the sportsbook line, prefer a clean same-game exchange check first.

Kalshi or any other exchange/market view is supplementary only.
Do not use exchange data as the primary price source unless the contract clearly maps to the exact game.

If no clean same-game market exists, say so and move on.
Do not force futures, series markets, or vague team contracts into a single-game handicap.

### Step 8 — Current price evaluation
Always state:
- Current line from ESPN/DraftKings
- Bettable-to price or clear pass point
- Whether any supplementary market check was an exact-game match, loose sentiment only, or unavailable

Use fair probability / implied probability math when it genuinely helps.
Do not force fake precision when the cleaner read is simply:
- playable at this number
- good only to a certain threshold
- pass if the line gets more expensive
- pass if outside market context does not cleanly map to the game

---

## What to Weight

### Current Team Form (highest weight)
- Is the offense actually scoring? Check last 5 games.
- Is there a scoring trend (heating up, cooling off, flat)?
- Heavy juice + cold offense = almost always a bad bet, regardless of roster quality
- Hot offense alone is not enough to justify a favorite pick. If the case is built mainly on bats, verify the run-prevention side harder before logging it as official.

### Starting Pitchers (weight current form, not reputation)
- Last 2 starts: runs allowed, innings pitched, walks
- Is the ERA from early starts or is this a late-season sample?
- Check for: injury return, times-through-the-order risk, manager leash tendencies
- Career stats vs specific opponent: useful signal, but discount 30-50% for current form divergence
- **Do not let a famous name override a cold recent trend**
- Always ask: which team is more likely to win the starter portion of the game, and by how much?
- If the underdog has the better starter edge and the offenses are close enough, treat that as a serious signal, not a side note.
- If you are backing a favorite with the weaker or shakier starter, the rest of the run-prevention path must be strong enough to absorb that risk.
- If the favorite's starter has a genuinely fragile floor, team-form edge alone is not enough for an official pick.
- Do not over-index on the listed probable alone if the opponent has a credible multi-arm early-to-middle innings run-prevention path.

### Bullpen
Casual bettors underweight this constantly.

Bullpen is not just a side note. It is part of the team's full win path.

Treat bullpen as a **proxy availability check**, not a claim of perfect certainty.
Use bullpen primarily as a supporting input unless the workload picture is unusually clean and lopsided.

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

Check:
- Hitter-friendly vs pitcher-friendly park context
- Temperature (cold can suppress offense)
- Wind direction and speed
- Wind direction relative to the field when known
- Fly-ball vs ground-ball pitcher fit
- Rain / delay risk that could shorten a starter outing and force earlier bullpen usage
- Whether bad weather is expected to clear before or during game time

When weather is meaningful, do not just list it. Say whether it is:
- not a real factor
- a mild concern
- or a real source of added variance

Always ask whether weather helps or hurts the stated win path for each side.
If the game is in a dome or weather is otherwise irrelevant, say that explicitly.

---

## Market / Price Protocol

Every MLB pick must answer:
- What is the current price?
- What is the worst number we would still take?
- Is this playable now, only playable to a threshold, or not playable unless a live number improves?
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

When a favorite is under consideration, ask this too:
- do I actually like this side at the current number?
- or do I just like the team more than the opponent?

If it is the second one, that is often a pass-now / better-number-later situation, not a clean pregame pick.

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
- conditional official picks are allowed only when the price trigger is explicit
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

When asked about or referencing the current picks record, always read `.picks/INDEX.md` first. Never state W/L record from memory.

---

## Post-Game Reflection (Required)

After every settled pick, log the review in `.picks/REFLECTIONS.md` and keep recurring rules in `.picks/PROCESS.md`.

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
