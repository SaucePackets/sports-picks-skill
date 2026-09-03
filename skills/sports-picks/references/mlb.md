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

ESPN team schedule parsing note:
- `competitor.score` may be a nested object like `{"value": 4.0, "displayValue": "4"}`, not a raw string/int.
- If recent-form output shows `n: 0` for active-season teams, check this first before assuming schedule data is missing.
- Use `competitions[0].status.type.completed` for final games, and parse score via `score.value` when present.

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
- In ESPN summary `boxscore.players[].statistics[]`, baseball batting/pitching categories may have `name: None`; identify pitching rows by `labels[0] == "IP"`, not by `name == "pitching"`.
- `header` from the ESPN summary endpoint may not include a top-level `name`; use the scoreboard event name or `header.competitions[0].competitors` to label the matchup.
- ESPN athlete gamelog endpoint (`site.web.api.espn.com/apis/common/v3/sports/baseball/mlb/athletes/{player_id}/gamelog?season=YYYY&category=pitching`) stores stat labels in top-level `labels` / `names`; `seasonTypes[0].categories` is a list of month splits, each with `events[]`. Do not parse `categories` as a dict. Join each month event's `eventId` back to top-level `events[eventId]` for date/opponent.

Example pitcher gamelog extraction pattern:
```python
data = requests.get(gamelog_url, params={"season": season, "category": "pitching"}).json()
labels = data.get("labels", [])
rows = []
for cat in data.get("seasonTypes", [{}])[0].get("categories", []):
    for event_row in cat.get("events", []):
        event_id = str(event_row.get("eventId"))
        event_meta = data.get("events", {}).get(event_id, {})
        stats = dict(zip(labels, event_row.get("stats", [])))
        rows.append({"date": event_meta.get("gameDate"), "opponent": (event_meta.get("opponent") or {}).get("abbreviation"), "stats": stats})
```

Injury endpoint note:
- ESPN injury endpoint may return top-level `injuries[]`, not `teams[]`. Match by team `displayName` if abbreviations are missing, then read nested team `injuries[]`.

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
- Reset triggers include: a losing streak just ended, a key bat returned, the lineup shape materially changed, the market is moving toward the supposedly cold team, or the team has already produced a reset game / multiple competitive offensive outputs inside the current series.
- If a reset trigger exists, the fade needs another real support layer behind it: hot bats on my side, elite/stable starter floor, or clearly cleaner bullpen/run-prevention support.
- Do not fade yesterday's version of a team if the current series shape suggests the offense may already be waking up.
- Do not double-dip a cold-offense fade across the same series once reset signs appear; rebuild the handicap from scratch.
- **Same-series recheck after a loss is mandatory** before backing the same side against the same opponent within 48 hours. The new starter edge must be independent of the failed pillar, and the opponent's counter-signal from the prior game must be addressed directly. If the new case is just the same broad team-over-team thesis with different pitcher names, cap at lean/pass.

### Starting Pitchers (weight current form, not reputation)
- The slate scanner provides season FIP and K-BB% per starter. When FIP and ERA diverge by >0.75, trust the FIP direction: ERA carries defense/sequencing luck at half-season samples. K-BB% is the most stable skill signal — cite it in every starter comparison.
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
- For road dogs, do not treat recent ER alone as proof of a stable starter floor. Stress-test command, walk risk, pitch efficiency, and swing-and-miss. Against high-ceiling lineups, traffic can become one crooked inning fast.
- A walk-rate fade is not clean by itself. Confirm the candidate offense has current traffic/punish support — recent OBP, walk rate, hard contact, or power — and that the opponent is not also in strong form. Otherwise cap at lean/pass.

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

Close-game survival rule:
- apply this hardest when the projected game is close late: favorite script, one-to-two-run margin, no overwhelming offensive/starter edge, and an opponent that can stay within one swing
- if the likely win path is a one-to-two-run game, identify the 7th-10th inning path before locking the pick
- a starter giving 6-7 good innings is not enough if the bridge/closer path is injured, taxed, or role-uncertain
- missing/taxed leverage arms + close favorite script is a hard-gate question first
- if the side needs late bullpen protection and the offensive/starter edge is not overwhelming, pass instead of assuming the late innings hold
- if the opponent's bullpen can stabilize after its starter exits, do not keep treating the game as a pure starter mismatch
- do not over-apply this to every bullpen uncertainty; with a clear multi-run offensive/starter edge and a passed gate, bullpen risk is a confidence cap/modifier, not an automatic pass

Important:
- do **not** pretend we know the exact closer decision unless directly verified
- do **not** let bullpen proxy override a much stronger starter/price/form read by itself
- if bullpen data is incomplete, say so explicitly instead of inventing certainty

### Lineup
- Actual batting-order quality (from depth chart + injury report)
- Handedness / platoon context if relevant
- Cold bats are real — check if key hitters are struggling

#### Unconfirmed-lineup watchlist recheck

Do not discard a strong near-miss when every hard gate passes except confirmed
batting lineups. Persist it to the schedule's `lineup_watchlist` with
`blocked_only_by: ["lineups_unconfirmed"]`, all original gate results, first
pitch, a target recheck at first pitch minus 75 minutes, the observed price,
and the bettable-to threshold. Any second blocker means ordinary PASS, not a
watchlist entry. While `starter_pending_promotions_enabled` is false in the
shared policy, the watchlist is restricted to `lineups_unconfirmed` only —
a pending `starter_unannounced` entry fails validation outright.

Get `first_pitch_utc` right, and check it before you move on. You write both it
and the recheck target derived from it, and nothing in the repo can tell that
either is wrong — they are only checked for being parseable timestamps. Because
the recheck window is computed from first pitch, a single mistyped date produces
an entry that is valid, never selected for recheck, never quarantined, and
silent to the overdue-recheck warning: invisible to every running job. The
review gate now prints a notice when an entry's first pitch does not fall on the
schedule day it was written onto — first pitch must land inside that Chicago
calendar day, with no tolerance either side. That is the one cross-check
available, because the day comes from the gate's clock, not from you. It catches
a one-day slip onto a morning game, which is the likeliest date error there is
and the one that would otherwise disappear. Treat that notice as a
transcription error to fix, not as noise.

Record the morning probability components on every watchlist entry as a
`slate_probability` object carrying `dk_fair_prob`, `raw_probability`,
`uncertainty_haircut`, `conservative_probability`, `current_ask`,
`projected_edge_at_current_ask`, and `model_version` (the same trail as a
schedule candidate). The recheck validator compares these against the
refreshed values; an entry without them cannot be promoted under standing
authorization.

Both watchlist price fields use signed American odds as JSON numbers:
`"original_price": 119` and `"bettable_to_price": 105` (or negative numbers
such as `-120`). Values such as `"MIN +119 at DraftKings"` and `"+105"` are
invalid. Keep source and timestamp prose in the slate or `thesis`, not in the
numeric fields.

**The schedule is landed by code.** Do not write
`.picks/execute/<date>-schedule.json` yourself. Run
`python3 scripts/mlb_slate_writer.py --skeleton --day <date>` after the Stage 2
scan, fill in the draft it writes to `.picks/tmp/<date>-slate-draft.json`, and
land it with `python3 scripts/mlb_slate_writer.py --land <draft> --day <date>`.
The writer derives `slate_denominator` from the scan roster — a draft that
carries its own is refused — runs both validators
(`mlb_game_reads.validate_with_denominator` and
`mlb_lineup_watchlist.validate_watchlist`, the same functions the scheduled gate
uses) and writes **nothing** unless they come back clean, so a refused landing
leaves any previous schedule byte-identical. A nonzero exit lists every defect
at once; fix the draft and land again.

The writer refuses to overwrite a schedule whose candidates already carry
`vig_approved`, `vig_notes`, `execution_status` or `executed`, or whose
watchlist entries have been rechecked. There is no flag to override that: those
decisions exist nowhere else. Re-landing an untouched schedule is ordinary and
allowed.

It refuses the same four fields in the **draft**, for the mirror-image reason:
`vig_approved` and `vig_notes` are the reviewer's to write and
`execution_status` and `executed` are the executor's, and a candidate the
producer has already marked approved is not queued for review — it goes to the
executor unreviewed. Leave all four exactly as the template above has them
(`null`, `null`, `null`, `false`); those values land fine, and it is only a
non-null ruling or a truthy `executed` that is refused. `execution_mode:
"standing_authorized"` is not a decision and is expected on every card.

You can still validate a landed schedule by hand with
`python3 scripts/mlb_lineup_watchlist.py <schedule> --validate` and
`python3 scripts/mlb_game_reads.py <schedule> --validate`. Then run
`python3 scripts/mlb_slate_receipt.py --write`. The game-reads validator finds
the denominator by convention at `.picks/tmp/stage2-<date>.json`, which
`mlb_stage2_scan.py` writes on every run; its absence is an error, not a skipped
check, so neither command needs `--denominator`. **Slate success is the
receipt's verdict, not your own assessment of the run** — report
`recorder_failed` as a failure even when the prose reads well and the card is
legitimately empty.

### Recording the refusals

The second validator covers the per-game refusal record described in
`SKILL.md`. The numbers it wants are the ones the writeup is already computing
in order to say "the ask is already below the DK fair prior": `dk_fair_prob`
comes straight from `mlb_stage2_scan`'s `away_fair` / `home_fair`,
`polymarket_ask` and `net_edge` from the price discipline step, and
`refusing_rails` from the gate that actually stopped it.

It also wants the handicap itself on every game: `raw_probability`,
`uncertainty_haircut`, `conservative_probability` and `model_version` — the
same trail an approved candidate carries, recorded for the games you refuse as
well as the one or two you take. `conservative_probability` must equal
`raw_probability - uncertainty_haircut` on both sides, exactly as it does for a
candidate, and the haircut is one non-negative number for the read; **zero is
legal**, because a market-only read charges no buffer.

**Record all four or excuse all four.** A partial trail is refused, because
every field is what makes another one checkable: an excused
`uncertainty_haircut` leaves nothing to subtract, so `conservative_probability`
could disagree with `raw_probability` by any amount and still validate, and a
`raw_probability` with no `model_version` can be counted but never evaluated —
the deployment gate segments rows by that string. A game that was never
handicapped excuses the whole trail in `unavailable`; a game that was
handicapped owes the whole trail.

**The edge is arithmetic, not prose.** `net_edge` must equal
`conservative_probability - polymarket_ask` on each side, within 0.001 — the
same rule `conservative_probability = raw_probability - uncertainty_haircut`
already follows one field over. Record it and you owe both operands: an edge
with nothing to subtract is a number nobody can check. You may leave `net_edge`
out and say why in `unavailable` — a game you priced and handicapped but never
finished pricing out is a real state and the measurement lane counts it — but
that buys nothing, because every rail below recomputes the edge from the two
operands rather than reading the field.

**A disposition has to agree with the numbers beside it.** Two rules, both
checked by `mlb_game_reads` and both refusing the schedule:

- `candidate` and `lineup_watchlist` require a recorded `polymarket_ask` and
  the whole model trail. You cannot card a game you did not price and
  handicap — the candidate the reviewer receives is built from exactly those
  numbers.
- `price_discipline` may not be named on a game whose own
  `conservative_probability - polymarket_ask` is at or above the deployed
  `min_conservative_edge` on either side. Refusing a price that clears the
  floor is a record that contradicts itself. Refusing it on a *handicapping*
  rail is not: name `starter_floor`, `bullpen_close_game_survival` or whichever
  gate actually stopped it, and the record agrees again.

The floor comes from the deployed policy (`mlb_policy.min_conservative_edge` in
`risk_limits.json`), never from a number written here. When that policy cannot
be loaded, a read naming `price_discipline` is reported as **uncheckable** —
the claim is not accepted on trust.

**Look at the eligibility report before you land.**
`python3 scripts/mlb_eligibility_report.py --draft .picks/tmp/<date>-slate-draft.json`
prints, for every game and both sides, the DK fair prior, the current ask, the
raw and conservative probabilities, the recomputed edge, the executable
`max_polymarket_price` ceiling, the verdict under the deployed floor, the rails
you named, and whether your disposition agrees with your own numbers. It exits
nonzero on a contradiction or a malformed read. It takes `--schedule` on a
landed file and reports identically — the draft view and the landed view are
the same rows.

It reports; it does not decide. It never writes a disposition, promotes a
model, moves the floor, or touches an order. A side reported `eligible` is not
an instruction to bet it — the handicapping gates are yours to apply and a
`pass` naming one of them is a complete and correct record.

**Get the sides the right way round.** The reads carry `away` and `home` from
the scan, and the outcome is joined from the MLB StatsAPI final. A transposed
read produces a row that scores one club's handicap against the other club's
result — clean-looking, counted, and invisible afterwards — so the dataset
builder refuses a read whose sides are crossed relative to the final.

The schedule validator asks the same question one step earlier, at the schedule
itself. A read is matched to its scheduled game by `game_pk`, and the matched
pair then has to agree: the `event_id` must be the one the denominator (and the
scan behind it) carries, and the two clubs must not be the other way round.
Copying a read from the game above it and editing only the `game_pk` used to
pass — most easily on a doubleheader, whose two games share a date and both
clubs — and it produced well-formed numbers about a different game. The club
names are checked only for *crossing*, never for spelling, so writing `ATL`
where the scan wrote `Atlanta Braves` is fine; putting the away club in the
`home` slot is not — **as long as both records name the clubs the same way**.

That last clause is a declared limit, not an oversight. Crossing is decided by
comparing the two names, so a read that both renames and crosses — `away:
"NYM"`, `home: "ATL"` against a scan saying `Atlanta Braves` at `New York
Mets` — matches on neither side and is reported by nothing. Resolving one
vocabulary onto the other is not something this rail does. What keeps the limit
narrow is `--skeleton`: the stub it writes already carries the scan's own
`away`, `home` and `event_id`, so a crossing made while filling that stub in is
caught. Retyping the club names by hand is what removes the rail.

The slate date is canonicalised before it is persisted: exactly `YYYY-MM-DD`, a
real day on the calendar, whitespace stripped. The date is the record's
address, so a value that validates in one spelling and is written in another
files the schedule where the receipt and the gate will not look. `--day
2026-9-1` and `--day 2026-02-30` are usage errors, not landings.

This is what makes the model gate answerable. `mlb_probability_model.py
dataset` has only ever been able to read `picks.json` — settled, executed
picks — so a model could not deploy without out-of-sample evidence, evidence
came only from bets, and bets required a deployed model. A handicap on a game
we passed is still a testable pre-pitch prediction, and it is the less biased
one: a pick exists only where the model liked itself enough to clear the edge
floor. `scripts/mlb_model_eval_dataset.py` turns these reads plus finals into
the rows the existing evaluator already consumes.

The rail vocabulary is the six handicapping gates
(`starter_floor`, `opposing_starter_shutdown_path`,
`bullpen_close_game_survival`, `cold_fade_reset`, `price_discipline`,
`real_winner_conviction`), the two deferrable blockers (`lineups_unconfirmed`,
`starter_unannounced`), and six structural rails for a game that could not be
priced, was closed out by a volume rail, or whose required inputs never
arrived (`no_dk_price`, `no_polymarket_market`, `game_already_started`,
`park_environment_cap`, `daily_volume_cap`, `incomplete_input_data`). Name
every rail that refused the game, not just the first.

`incomplete_input_data` means a required input never arrived — a missing
offense row, an absent probable starter, a stat lookup that returned nothing —
so the game could not be handicapped on its merits. It is the rail to name
when the refusal is about the inputs rather than about the game: do not reach
for the nearest handicapping gate, which records a judgement that was never
made.

Recording is not a gate change. A read never causes a bet and never blocks one;
it records the decision the existing gates already made.

The conditional review gate runs frequently enough to select the entry 60-90
minutes before first pitch. At recheck, refresh ALL material baseball inputs —
not only lineups, injuries, and price: starter role and expected innings,
bullpen/leverage-arm availability, lineup quality from the confirmed orders,
park/weather, and the live ask. Rerun every original gate. Promote only if all
gates still hold and the price remains acceptable; otherwise log `status:
"passed"` plus the decisive reason.

A standing-authorized promotion must satisfy the refresh contract — a recheck
that merely asserts "all original gates hold" is rejected by the validator:

- `recheck.probability`: the full refreshed trail (`dk_fair_prob`,
  `raw_probability`, `uncertainty_haircut`, `conservative_probability`,
  `current_ask`, `projected_edge_at_current_ask`, `model_version`). The
  promoted candidate's probability fields must equal it — never route the
  morning numbers.
- refreshed `baseball_evidence` and `execution_checks` on the promoted
  candidate.
- `recheck.material_changes`: a list of material input changes (empty list when
  nothing material changed). If changes are recorded but
  `conservative_probability` is unchanged, add
  `recheck.probability_unchanged_justification`.
- `recheck.probability_change_reasons`: a written reason keyed by field for
  every probability component (`dk_fair_prob`, `raw_probability`,
  `uncertainty_haircut`, `conservative_probability`) that changed from
  `slate_probability`.
- Lineup confirmation clears the lineup gate but adds ZERO win probability by
  itself. Any `conservative_probability` increase over the morning slate
  requires `recheck.quantified_upgrade` = `{component, delta, evidence}` with
  `delta` equal to the increase.

When local MLB standing authorization is enabled, promotions use
`execution_mode: "standing_authorized"`, `execution_status: "pending"`, an
explicit `max_polymarket_price`, and `executed: false`. Never create a one-shot
execution cron, proposal token, or order in the reviewer; the recurring poller
owns the guarded execution attempt.

### Park / Weather
Treat weather as a real handicap input, not an afterthought.

The scanner provides `park.run_factor` (100 = neutral) per game. Cite it on every card candidate. A flagged extreme hitter park (>=105, e.g. Coors) caps confidence at Medium per the park rule; <=96 strengthens pitcher-side reads.

**An unavailable park factor is not a stop.** `park.run_factor` is null and `park.data_status` is `unavailable` whenever the venue is off the scanner's fixed table — a neutral-site game, a new or renamed ballpark. That is a data outage, and it routes the same way the price and lineup outages do: price the game with the uncertainty priced in, do not discard it. Concretely, take **no** `park_home_context` adjustment (there is no data to support one) and charge the `unknown_park_environment` haircut with the outage as its written evidence. Both together are a hard validator failure. Never substitute a neutral 100 for a missing factor — that is a park read claimed on no data.

Be clear about which half of that a machine checks. The validator **does** reject the haircut and a `park_home_context` adjustment together. It does **not** know the park was unavailable: it never sees `park.data_status`, so a candidate that stays silent about an unknown park validates cleanly and, with no haircut, clears on exactly the same terms a known-park game would. Charging the haircut on an outage is a rule you follow, not a rule that is enforced. Team `away_offense`/`home_offense` carry season wOBA and xwOBA: wOBA trailing xwOBA by >=0.010 means the offense is UNDERperforming its contact quality (fade cold-offense narratives); the reverse means it is running hot (discount recent scoring).

Always check it for MLB.
Use the dedicated `weather` skill path first when weather is needed for analysis. If `wttr.in` hangs or gets blocked, do not retry the same curl command; use Open-Meteo JSON directly with venue/city coordinates for temperature, wind speed/direction, and precipitation.
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

De-vig before any edge claim:
- DK single-side implied probability includes roughly 2 points of vig. Never compare it directly to a Polymarket ask and call the difference an edge.
- Compute both sides' implied probabilities from the two DK moneylines, then `fair = imp_side / (imp_side + imp_opp)`.
- State your own `raw_probability` (decimal) from the full handicap, with every adjustment recorded as an explicit component against the de-vigged DK fair prior. If it differs from the de-vigged fair by more than 0.04, the component adjustments must explain the delta.
- Apply the documented `uncertainty_haircut` (small samples, opener/bulk uncertainty, contact/HR dependence, unavailable leverage relievers, unconfirmed lineups, conflicting signals, unknown park run environment) to get `conservative_probability` — the ONLY probability used for edge and execution. The haircut is a model-uncertainty buffer, NEVER a venue fee.
- Conservative edge = `conservative_probability - current_ask`. Polymarket US charges ZERO trading fees (confirmed 0 bps on every executed receipt) — do NOT subtract a phantom fee. Cardable requires conservative edge >= the shared policy floor `min_conservative_edge` (currently 0.05, from the `vig-mlb-selection-policy-v1` block in `~/.hermes/vig/state/risk_limits.json`).
- Equivalently — and this is the guardrail the execution poller actually enforces — the real executable ask must be at or under your price ceiling `max_polymarket_price = conservative_probability - min_conservative_edge`. That ceiling (not any fee) is the single source of truth: judge price on the true cost to buy, and any real fee is fine as long as the all-in price stays at or under the ceiling. Execution is an IOC limit placed AT the ceiling, so the book can only ever fill at or under your number.
- Record `dk_fair_prob`, `raw_probability`, `uncertainty_haircut`, `conservative_probability`, `current_ask`, `projected_edge_at_current_ask`, and `model_version` on every schedule candidate and ledger row — these feed the monthly calibration report (`scripts/vig_calibration_report.py`). Recompute `projected_edge_at_current_ask = conservative_probability - current_ask` at every recheck and at fill; the morning `net_edge` is never the executed edge, and a candidate with missing or stale probability fields is ineligible.
- Volume rails: at most `max_mlb_official_bets_per_day` (currently 2) official MLB bets per day — when more candidates qualify, rank by live conservative edge and keep only the top two. During probation, at most `max_small_bets_per_day_probation` (currently 1) Small bet per day. Starter-pending (`starter_unannounced`) watchlist entries AND promotions are disabled while `starter_pending_promotions_enabled` is false — the live watchlist is lineup-only.

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

### Postgame evidence & process grade (Phase 5 hardening)

Question 3 is no longer answered by judgment alone. Before writing any reflection,
collect deterministic game-script evidence with
`scripts/mlb_postgame_evidence.py collect` (MLB Stats API `feed/live`): starter and
bulk-pitcher lines, expected-vs-actual role (`starter | opener_bulk | short_start |
unknown`), bullpen usage, offense conversion, and the scoring sequence. The
collector exits non-zero when evidence is insufficient — settlement fails loud, it
never guesses.

Then write a structured `process_grade` on the ledger row and validate it with
`scripts/mlb_postgame_evidence.py grade`:

- `process_grade`: `good_read_bad_variance` (loss) | `good_read_edge_held` (win) |
  `good_read_execution_issue` | `bad_read_starter_role` | `bad_read_starter_quality` |
  `bad_read_bullpen_availability` | `bad_read_offense_conversion` |
  `bad_read_named_risk` | `insufficient_evidence`
- `pillars`: every thesis pillar (`starter_role`, `starter_quality`,
  `bullpen_availability`, `offense_conversion`, `named_risk`) graded
  `held | failed | mixed | unknown` with written evidence citing the collected numbers.

Hard rules the validator enforces:

- Pillar grades the boxscore decides deterministically (`held`/`failed`) cannot be
  overridden by the reviewer.
- A loss is `good_read_bad_variance` ONLY when every pillar is graded `held` with
  evidence — a loss can never be called variance without complete pillar evidence.
- Missing postgame evidence forces `insufficient_evidence`: mark the review pending;
  never default to "I would assign it again".
- Wins are graded too: a win carried by variance still gets its `bad_read_*` grade.

Promote a durable rule to `PROCESS.md` only after a repeated or structural failure;
match-specific detail stays in `REFLECTIONS.md`.

Known recurring failure modes to watch for:
- Cold offense + heavy juice (check run scoring trend FIRST)
- Reputation bias (career ERA, famous roster — discount for current form)
- Career stats vs opponent overstated (good signal, not standalone edge)
- Early season record noise (game-by-game form beats W-L through 10 games)

---

## Baseball Evidence & Execution Checks (Phase 2 hardening)

Every standing-authorized MLB approval/promotion must carry two structured objects;
the deterministic validators in `scripts/mlb_baseball_evidence.py` fail closed on
missing or invalid content. The reviewer prompts, lineup recheck prompt, and
execution poller prompt all carry this contract.

### `baseball_evidence` (the thesis/edge inputs)

- `starter_role`: `starter` | `opener` | `bulk` | `piggyback` | `unknown`. Unknown is a hard failure.
- `expected_ip`: positive numeric innings projection. `expected_pitch_count` optional.
- `starter_sample_ip`, `starter_games_started`: observed track record backing the projection.
- `starter_floor_evidence`: quantified justification (recent starts, K-BB%, FIP).
- `opponent_shutdown_path`: why the opponent is/is not likely to suppress the edge.
- `candidate_failure_path`: the specific scenario that erases the edge.
- `contact_hr_risk`, `bullpen_availability`, `offense_quality`, `lineup_quality`: objects
  with `magnitude` in {none, small, moderate, large} plus short notes.
  `bullpen_availability.leverage_arms_available` must be `true`.
- `likely_leverage_arms`: which rested arms back the late-inning path.
- `environment`: park factor, temperature, weather notes.
- `named_risks`: list of `{name, status: resolved|unresolved, evidence}`. Any
  unresolved named risk is a hard failure.
- `primary_thesis_pillar`: `true` only when the starter path is the main edge.
  If true: `expected_ip >= 5` and `starter_games_started >= 6`.
- `support_layers`: list of `{pillar, magnitude}`. If `primary_thesis_pillar` is true
  and `contact_hr_risk` is moderate or large, a separate `large` support layer is required.
- `probability_delta_explanation`: required when `raw_probability` exceeds
  `dk_fair_prob` by more than 0.04.

Opener/bulk/piggyback roles also require a non-empty `bulk_path_plan`.

### `execution_checks` (tradeability confirmation)

All fields required; booleans must be exactly `true`:

- `exact_event_slug_side_mapping`
- `supported_price` (number 0–1), `price_timestamp` (ISO8601 UTC)
- `current_ask_inside_ceiling`
- `liquidity` (object, e.g. `book_state`, `fillable_notional_usd`)
- `bankroll_and_daily_cap_ok`
- `lineup_confirmation`
- `injury_scratch_refresh`
- `receipt_dedup_ready`

Execution checks never increase probability; baseball evidence never loosens
price discipline. Both are re-checked at routing, at the execution gate, and at
the final lock before any order.

---

## Probability Components & Model Deployment (Phase 3 hardening)

Every standing-authorized MLB approval/promotion must carry a structured
`probability_components` object; the deterministic validator in
`scripts/mlb_probability_model.py` fails closed when the numbers do not
reconcile. De-vigged DraftKings fair probability is the market prior; prose can
never substitute for component arithmetic.

### `probability_components`

- `adjustments`: list of `{component, delta, evidence}`. Deltas must sum to
  `raw_probability - dk_fair_prob` (tolerance 0.001). Allowed components:
  `starter_run_prevention`, `starter_expected_innings`, `k_bb_contact_profile`,
  `opponent_starter_bulk_path`, `lineup_offense_quality`,
  `bullpen_quality_availability`, `park_home_context`, `injury_lineup_deltas`,
  `recent_form`. Single-component bound ±0.15. `recent_form` is a low-weight
  supporting input (|delta| <= 0.02, never the largest component).
- `haircuts`: list of `{component, amount, evidence}` with positive amounts
  summing to `uncertainty_haircut`. Allowed components: `small_sample`,
  `opener_bulk_uncertainty`, `contact_hr_risk`, `leverage_relievers_unavailable`,
  `lineup_unconfirmed_or_weakened`, `conflicting_signals`,
  `unknown_park_environment`. The last one may not accompany a
  `park_home_context` adjustment — the park run environment is either known or
  it is not, and claiming both is a buffer that nets to nothing.
- `conservative_probability` must equal `raw_probability - uncertainty_haircut`
  — the only probability used for edge and execution.
- Every component requires written pre-pitch evidence; postgame fields inside a
  component are a hard failure (no leakage).
- Market-only fallback (`model_version` `vig-mlb-market-v1`): empty adjustments
  and haircuts with `raw_probability == dk_fair_prob` and
  `uncertainty_haircut == 0`.

### Model evaluation and the deployment gate

`scripts/mlb_probability_model.py` also owns the model lifecycle:

- `dataset`: builds the historical evaluation dataset from settled ledger rows —
  pre-pitch probabilities plus the official outcome only, skipped rows reported
  loudly.
- `evaluate`: time-ordered walk-forward evaluation (never a random split) —
  Brier score, log loss, calibration slope/intercept, reliability buckets —
  always against the DK-fair market baseline on the same rows.
- `gate`: the versioned deployment gate, fail-closed. A model version may deploy
  only when the predeclared margins in the `mlb_model_deployment_policy` block of
  `risk_limits.json` (schema `vig-mlb-model-deployment-policy-v1`) are met:
  minimum evaluation window, out-of-sample calibration no worse than the market
  baseline, no predictive score regressing, and at least one predictive score
  improving by its predeclared margin. Anything else exits non-zero and the
  market-only fallback stays active.
