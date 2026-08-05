# NFL Pick Workflow

## Core NFL Lens

The NFL is not MLB. There is no starter-vs-starter shortcut, samples are tiny
(17 games), and week-to-week variance is enormous. Any given Sunday is a data
fact, not a cliche: double-digit favorites lose outright every season.

Treat each game as five layers — in this order:
1. **Current team form** (last 4-5 games — points for/against, point differential trend)
2. **QB situation** (who actually starts, health, and the drop-off to the backup)
3. **Injury/inactives picture** (OL/DL clusters, secondary vs passing strength, practice designations)
4. **Situational edges** (rest, short week, travel, bye, divisional, weather)
5. **Market price**

Short version:
- Who is actually playing quarterback, and at what health?
- Which team is playing better football right now?
- Do the injuries break either team's win path?
- Does the schedule spot favor one side?
- Does the current number still make sense after all of that?

A team can be the better roster and still be a bad bet at the price — especially
on a short week, on the road, or with a compromised offensive line.

---

## NFL Runtime Lock Gate

Default to **PASS** until every hard gate clears. Same semantics as the main
SKILL.md Runtime Lock Gate: this is a state machine, not advice. A failed hard
gate converts any lean to PASS — not Medium, not value, not conditional.

```text
official_pick_allowed = true

if qb_status_gate fails: official_pick_allowed = false
if backup_qb_dropoff_unpriced: official_pick_allowed = false
if injury_cluster_gate fails: official_pick_allowed = false
if my_defense_late_game_survival fails: official_pick_allowed = false
if rest_travel_gate fails: official_pick_allowed = false
if weather_gate fails: official_pick_allowed = false
if week_to_week_overreaction_gate fails: official_pick_allowed = false
if price_discipline fails: official_pick_allowed = false
if real_winner_conviction fails: official_pick_allowed = false
if thesis_is_opponent_fade_or_price_more_than_my_team: official_pick_allowed = false

if official_pick_allowed == false:
  output PASS
```

Gate definitions:

- **qb_status_gate** — My side's starting QB is confirmed (practice reports,
  official designations). A Questionable QB whose absence would break the
  handicap is a failed gate until confirmed — or an inactives-watchlist entry
  if that is the only blocker. Never assume a QB plays because he is famous.
- **backup_qb_dropoff_unpriced** — If either team starts a backup QB, the
  handicap must be rebuilt around that backup's actual profile, not the team's
  season numbers (the starter produced those). If my case still quotes
  starter-era stats, the gate fails. Backup-QB lines are also where the market
  overreacts both ways — a backup alone is not a fade thesis either.
- **injury_cluster_gate** — One star out is a data point; a cluster is a broken
  win path. Two or more starting OL out against a real pass rush, a top corner
  duo out against an elite passing offense, or both starting edge rushers out
  against a clean-pocket QB — each fails the gate unless the rest of the case
  is overwhelming and the price already reflects it.
- **my_defense_late_game_survival** — The NFL analog of bullpen survival. If
  the likely script is a one-score fourth quarter, my side's defense must be
  able to get a stop and my offense must be able to kill a clock on the ground.
  A pick that only wins a shootout is chaos, not confidence. Two bad defenses
  means variance — do not upgrade chaos into an official pick.
- **rest_travel_gate** — Short-week road games, cross-country travel into early
  kickoff windows (West Coast team at noon ET / body-clock 9am), and teams
  coming off international games get a hard look. A thin edge does not survive
  a bad schedule spot; a failed spot check on my side is a failed gate.
- **weather_gate** — Outdoor game with sustained wind ≥ 15 mph, heavy
  rain/snow, or extreme cold: the thesis must explicitly survive the weather.
  Wind kills passing efficiency and field goals; if my edge is a passing
  offense in 20 mph gusts, the gate fails. Domes: state it and move on.
- **week_to_week_overreaction_gate** — The case cannot rest mainly on last
  week's single result. A blowout is the classic regression spot in both
  directions. If removing last week's game from the form read kills the
  thesis, the gate fails.
- **price_discipline** — Same math as MLB: de-vig the two moneylines, state my
  own win probability, and require real net edge (see Market / Price Protocol).
  Road favorites at roughly -150 or worse need dominant current form plus a
  clear QB/defense edge, not just reputation.
- **real_winner_conviction** — I actually believe this team wins the game most
  often. If I like the price more than I like the team, PASS.
- **opponent-fade trap** — Same as MLB: the selected side must have its own
  clean win path. "Their QB is bad" is not a thesis if my side cannot block,
  cover, or score.

Hard NFL-specific calibrators:
- Divisional games are tighter than season profiles suggest — twice-a-year
  familiarity compresses edges. A divisional road favorite needs an extra
  layer of separation before laying a big number.
- Thursday games are lower-quality and higher-variance. Thin edges do not
  clear the bar on Thursday.
- Weeks 1-4 run on prior-season priors that rosters and coaching staffs have
  already broken. See Early Season Protocol.
- Do not pick a side because the opponent is "in turmoil" (media narrative,
  locker-room stories). Narrative is not data.
- Medium confidence still requires every hard gate to pass. It never means one
  gate failed but the lean survived.

Run this lock gate immediately before outputting any official NFL card.

---

## Data Pull Order (Follow This Exactly)

### Step 0 — Slate scan (automation)
For a cron/pipeline run, start from the deterministic scanner:
```bash
python3 scripts/nfl_stage2_scan.py --season 2026 --week 1
```
It emits one JSON row per game: moneylines + de-vigged fair probabilities,
last-5 form (W-L, PF/PA, point differential), rest days and short-week flags,
best-effort injuries, and venue/indoor context. Treat it as slate context;
deeper per-candidate stats still come from the steps below.

### Step 1 — Current form (ALWAYS first)
Pull each team's last 4-5 completed games:
- Points scored and allowed per game, and the trend
- Point differential — 3-2 built on +40 is not the same as 3-2 on -10
- Who they beat: wins over teams with winning records count more

```python
# ESPN team schedule — completed results
url = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule?season=2026'
```
ESPN schedule parsing note (same gotcha as MLB): `competitor.score` may be a
nested object like `{"value": 24.0, "displayValue": "24"}`, not a raw value.
Use `competitions[0].status.type.completed` for finals and read `score.value`
when present.

### Step 2 — Season efficiency stats
```python
url = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/statistics'
```
Or via the nfl-data skill: `sports-skills nfl get_team_stats --team_id=<id>`.

Minimum set per team, both sides of the ball:
- Yards per play (offense and defense) — the most stable single team signal
- Points per game / points allowed per game
- Turnover differential (regresses hard — treat extreme values as fragile)
- Third-down and red-zone conversion, both directions
- Sacks taken vs sacks produced (pass-protection vs pass-rush picture)

### Step 3 — QB confirmation + injury report
```bash
sports-skills nfl get_injuries          # league-wide report with status
```
If `sports-skills` returns an ESPN "Access Denied" (Akamai blocks
`site.api.espn.com` from data-center clients), load the repo shim first —
it mirrors requests to the public `site.web.api.espn.com` host with the
same routes and payload shape:
```python
import scripts.sports_skills_espn_shim  # noqa: F401  (apply before importing sports_skills)
from sports_skills import nfl
injuries = nfl.get_injuries()
```
```python
# ESPN core injuries per team (same $ref pattern as MLB)
url = 'https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/teams/{tid}/injuries'
```
- Wednesday-Friday practice designations tell the real story: DNP-DNP-DNP is
  almost always Out; LP→FP trend usually means Active.
- Official statuses: Out / Doubtful / Questionable. Doubtful effectively means
  out. Questionable QBs and OL are the gate-relevant ones.
- Inactives are published ~90 minutes before kickoff and are the final truth.
- Never name starters from memory — depth charts change weekly.

### Step 4 — Depth charts (backup quality)
```bash
sports-skills nfl get_depth_chart --team_id=<id>
```
When a starter is out, the handicap is the backup's profile, not the absence.

### Step 5 — Rest / schedule spot
The scanner emits `rest_days` and `short_week` per side. Check:
- Short week (Thursday) vs mini-bye (post-Thursday) vs off full bye
- Travel: cross-country trips, especially West Coast teams in early ET windows
- Sandwich spots: divisional opponent next week, big rival last week

### Step 6 — Weather (outdoor games only)
Use the dedicated `weather` skill path first; if `wttr.in` hangs, use Open-Meteo
JSON with stadium coordinates. Wind ≥ 15 mph sustained matters more than rain.
If the game is in a dome or retractable-closed, say so explicitly — do not skip
the step silently.

### Step 7 — Live sportsbook odds (ESPN/DraftKings)
```python
url = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={game_id}'
# → pickcenter[0] moneylines / spread / total
```
This is the PRIMARY price source. Re-verify at analysis time — NFL lines move
all week on injury news, so the scanner's price is a snapshot, not the lock
price.

### Step 8 — Markets matching layer (exchange, supplementary)
Same rules as MLB: `openclaw-imports/markets` to match the event to exchange
contracts; Kalshi/Polymarket only when the contract cleanly maps to the exact
game. No clean match, no forced comparison.

---

## What to Weight

### Current Team Form (highest weight)
- Last 4-5 games with point differential, not just W-L
- Strength of recent opponents — form built against backup QBs is soft
- One-score records are noisy; a 5-2 team that is 4-0 in one-score games is
  closer to average than its record
- Season-long yards per play is the stability anchor when recent form and
  season profile disagree

### Quarterback (the single biggest player variable)
- Current-season efficiency (yards per attempt, sack rate, turnover rate) over
  reputation — do not let a famous name override a cold trend
- Health: a "playing hurt" QB with a visibly changed profile is a downgrade the
  season stats do not show
- Backup starts: rebuild the whole handicap around the backup's actual starts,
  not preseason folklore
- Pressure interaction: a QB who collapses under pressure against a top pass
  rush is a specific, checkable mismatch (sack rate vs pressure rate)

### Trenches (casual bettors underweight this constantly)
- OL health/continuity vs opposing pass rush is the most common hidden edge
- A great skill group behind a broken OL is a broken offense
- The run-game matchup matters most late with a lead — it is the clock-killing
  path the late-game survival gate asks about

### Defense
- Points and yards per play allowed, adjusted for opponents faced
- Secondary health vs opposing passing strength — corner injuries are quiet
  gate-breakers
- Takeaway rates regress; do not pay for last month's turnover luck

### Situational
- Home edge is real but smaller than the old 3-point cliche — closer to
  ~1.5-2 points league-wide
- Divisional games: tighter, weirder, dog-friendly
- Rest differential: off-bye vs short-week is a real edge; check whether the
  market already moved for it
- Primetime/Thursday: sloppier games; thin edges do not survive

### Inactives-dependent watchlist recheck

The NFL analog of the MLB unconfirmed-lineup watchlist. Do not discard a strong
near-miss when every hard gate passes except an unconfirmed player status that
resolves at inactives time.

Persist it under schedule-level `inactives_watchlist` with:
- `blocked_only_by: ["inactives_unconfirmed"]` (or `["qb_status_unconfirmed"]`)
- `kickoff_utc` and `recheck_due_utc` (kickoff minus 75 minutes — inactives
  drop at kickoff minus 90)
- the complete `original_gate_results`
- `original_price` and `bettable_to_price` as signed American-odds JSON
  numbers (`119`, `-120`) — same contract as MLB: never a team name, book,
  `+` sign, or quoted string in those fields; descriptive context goes in
  `thesis`
- `status: "pending_inactives_recheck"`

Any second blocker means ordinary PASS, not a watchlist entry.

The conditional reviewer checks pending entries in the kickoff-minus-90 window:
refresh inactives, confirm the blocking player status, re-verify the price, and
rerun every original gate. Promote only if all still hold; otherwise set
`status: "passed"` with `recheck_notes`. NFL promotions are **manual-only**
(`awaiting_jerry`): there is no NFL standing execution authorization, and the
MLB standing-authorization path must not be reused for NFL candidates.

---

## Market / Price Protocol

Identical math to MLB — de-vig before any edge claim:
- Compute both sides' implied probabilities from the two moneylines, then
  `fair = imp_side / (imp_side + imp_opp)`. Never quote single-side implied as
  "fair" — it carries the vig.
- State your own `win_probability` (decimal) from the full handicap. If it
  differs from de-vigged fair by more than 0.04, the thesis must say what the
  market is missing — and the NFL market misses less than MLB's, because there
  is one game a week and armies of pricing attention on each.
- Net edge = `win_probability - exchange_ask - 0.024` (fees) when an exchange
  market cleanly matches. Cardable requires net edge >= 0.02.
- Record `win_probability`, `dk_fair_prob`, and `net_edge` on every schedule
  candidate and ledger row — they feed the monthly calibration report.

Every NFL pick must answer:
- What is the current price, and the worst number we would still take?
- Why do I actually believe this team wins?
- What is each team's full win path through QB, trenches, defense, and the
  schedule/weather spot?
- Which single injury or inactives decision would flip this, and is it settled?
- What would make this a pass?

Moneyline first. Spreads and totals are separate disciplines — do not log them
as official picks unless the user explicitly asks for spread/total work.

---

## Early Season Protocol (Weeks 1-4)

- Prior-season stats are the baseline, but discount hard: coaching changes,
  QB changes, OL turnover, and scheme installs break priors every September.
- Flag explicitly: "Early season — prior-season baseline, discounted."
- Weight offseason ground truth (confirmed QB/OL changes) over projection
  narratives (hype, camp reports).
- Week 1 is the highest-variance slate of the year. Cap confidence at Medium
  for Week 1 regardless of edge, and prefer zero-pick cards.
- By Week 5, current-season data leads and prior season becomes context.

## Preseason Protocol (August)

Preseason games are **not pickable**. Starters play snaps by coaching whim,
outcomes are noise, and no gate can clear honestly. During preseason the
pipeline runs in shakedown mode only:
- exercise the scanner, injuries, odds, and settlement plumbing end to end
  (`--seasontype 1` on the scanner)
- produce cards that say PASS with the data attached
- never log a preseason game as an official pick or route one to execution

---

## Output Format

Default to the main skill's tighter official-picks format: 1-3 picks max,
often zero. Winner first, price second. No unofficial lean/value buckets.

```text
Good data. Here's the breakdown:

───

🔵 Pick 1: [AWAY] @ [HOME] → [Side] ([Confidence])

Form:

• [Team]: last 4-5 games, PF/PA and point-diff trend — quick read
• [Team]: last 4-5 games, PF/PA and point-diff trend — quick read

QB:
[QB A] vs [QB B]. One or two sentences, current form and health first.

Trenches/defense check:

• [Team]: OL health vs opposing rush — clean / concern / red flags
• [Team]: defense late-game survival read

Spot:

• rest/travel/divisional/weather notes that actually matter, or "clean spot"

Market:

• current line; playable to / pass above
• exchange match found or not

The question:
One short sentence on what actually decides whether this is a bet.

───

⛔ Pass: [matchup]

One or two short reasons.
```

Ledger rows use the Official Pick Ledger Contract from SKILL.md with
`Sport: football`, `League: NFL`.

---

## Picks Record Protocol

Same as MLB: read the installed workflow's `.picks/INDEX.md` before stating any
record. Never from memory.

## Post-Game Reflection (Required)

After every settled pick, log the review in `.picks/REFLECTIONS.md` and keep
recurring rules in `.picks/PROCESS.md`. Settlement data comes from
`scripts/nfl_final_scores.py`, not LLM web-fetching.

Reflection prompts:
1. What decided the game?
2. Was the data available to catch it? (Was it in the Friday injury report?)
3. Bad bet or bad result?
4. What changes going forward?

Known NFL failure modes to watch for:
- Paying starter-era prices for backup-QB teams (and the inverse: auto-fading
  competent backups)
- Overreacting to last week's blowout in either direction
- Laying big road numbers in divisional games
- Ignoring OL injury clusters because the skill players are healthy
- Trusting turnover-differential form to continue
- Passing-offense theses in high wind
