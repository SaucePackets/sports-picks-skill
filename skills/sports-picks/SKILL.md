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
- Polymarket live trading is allowed only through `references/polymarket-trading.md`, `scripts/polymarket_us_sdk_bet.py`, and legacy `scripts/polymarket_us_guard.py`; proposals are dry-run by default and live orders require explicit Jerry approval plus a matching approval token.

---

## Live Trading Boundary

Sports analysis may produce an official pick. It does not automatically produce a bet.

Command semantics for Jerry:
- `deep analysis` / `deeper pass`: produce the full deep-analysis writeup only. No lock, no bet.
- `lock official picks only`: save verified official picks to the canonical ledger/Console. No Polymarket proposals or live orders.
- `lock and propose bets`: save verified official picks, then create dry-run Polymarket proposals with approval tokens. No live orders.
- `lock and place authorized MLB bets`: save verified official MLB picks, then place capped Polymarket US orders only if every standing-authorization gate passes.

For Polymarket execution:
- load `references/polymarket-trading.md` first
- for MLB official locks, also load `references/mlb-polymarket-auto-bets.md`
- for Polymarket US sports moneylines, load `references/polymarket-us-sports-moneyline.md` and use `scripts/polymarket_us_sdk_bet.py`; trust authenticated preview metadata, not slug/YES-NO guesses
- exact game/outcome mapping is mandatory
- create a dry-run proposal before any live order
- show market slug, side, action, price, quantity, max exposure, BBO, and approval token
- live MLB betting requires the explicit phrase `lock and place authorized MLB bets`; vague `lock picks` does **not** authorize live orders
- MLB official locks have standing authorization within the caps in `references/mlb-polymarket-auto-bets.md` only after that explicit phrase
- non-MLB bets, exits, cancels, modifications, market orders, props, parlays, and anything outside caps still require explicit Jerry approval in the current chat/session
- save a receipt under `.picks/receipts/polymarket/`
- placed-bet watchers and passed-price watchlists must check live MLB game state before suggesting entries/exits; price movement alone is not enough
- daily season automation should be staged: proposed card first, then lock-only, then dry-run proposals, then explicit standing-authorized entries, then watch/exit proposals, then auto-exits only after separate approval

No explicit authorization phrase, no bet. No receipt, no success claim.

---

## Runtime Lock Gate

Default to **PASS** until every hard gate clears.

Treat the official-pick decision as a state machine, not advice:

```text
official_pick_allowed = true

if starter_floor fails: official_pick_allowed = false
if opposing_starter_shutdown_path fails: official_pick_allowed = false
if my_bullpen_close_game_survival fails: official_pick_allowed = false
if my_bullpen_is_disaster_recently: official_pick_allowed = false
if game_shape_is_two_bad_bullpens_chaos: official_pick_allowed = false
if cold_fade_reset fails: official_pick_allowed = false
if price_discipline fails: official_pick_allowed = false
if real_winner_conviction fails: official_pick_allowed = false
if thesis_is_opponent_fade_or_price_more_than_my_team: official_pick_allowed = false

if official_pick_allowed == false:
  output PASS
```

Hard meaning:
- Failed hard gate means **PASS**.
- Not Medium.
- Not value.
- Not conditional.
- Not "still playable."
- No pick because the price is cute.
- No pick because the opponent is injured, cold, or using a weak opener if my side also has a major run-prevention flaw.
- A recent bullpen disaster is a hard veto unless the starter is likely to go deep and the edge is overwhelming; otherwise PASS.
- Two bad bullpens means chaos, not confidence. Do not upgrade chaos into an official pick.

Medium confidence is allowed only when every hard gate passes but the edge is not elite.
It must never mean one gate failed but the lean survived.

Analysis can find a lean. Hard gates decide the official pick. Failed gate converts lean → **PASS**.

Run this lock gate immediately before outputting any official card.

---

## Core Betting Principle

**Official picks only. Confidence first. Winner first. Price matters, but price alone does not create an official pick. Form first, reputation never.**

Official-pick confidence rule:
- first decide who I actually think wins the game most often
- then check whether the current price is still worth paying
- never promote a side to an official pick mainly because the number is attractive
- if I like the price more than I like the team to win, it is a pass

A team can be the better price and still fail the official-pick bar.
Official picks are for sides I genuinely believe win, with price acting as a filter, not the whole reason for the pick.

**Hard output rule:** if conviction is not real, do not make a pick.
If the edge is thin, the number is bad, the data is incomplete, confidence does not clear the bar, or the case is mostly "the dog is live at this number" without a real belief they win, output a pass and nothing else.

**Official pick gate:** before logging any official pick, explicitly check starter floor, opposing-starter shutdown path, bullpen survival, red-bullpen close-game risk, cold-fade reset risk, price discipline, and winner conviction. Any failed gate overrides the lean. Do not downgrade to Medium and keep it. Failed gate means pass.

**Opponent-fade trap:** do not promote a side mainly because the opponent looks awful, injured, cold, or mispriced. The selected side must have its own clean win path. If my side has a shaky starter, ugly walk profile, or bullpen disaster, the answer is PASS even when the opponent is worse.

**Bullpen chaos veto:** if my side's recent bullpen form is terrible, or both bullpens are terrible, that is not late-game survival. It is variance. Pass unless there is a clear starter-length edge and overwhelming separation elsewhere.

Road favorite chalk tightening: when both teams are hot, both bullpens are in good form, and the opponent starter has a credible shutdown path, do not let season profile plus starter floor carry a mid-price road favorite by itself. Require a clear late-game separation, offensive contact edge, or bullpen mismatch before paying roughly -130 or worse.

If new information or a better critique arrives before game start and breaks an official-pick gate, scratch the pick from the card rather than forcing it to stand.

Do not create side categories like "value plays," "leans," or other unofficial buckets unless the user explicitly asks for them. Default to one of two outputs only:
- official pick
- pass

---

## Verify Before You Speak — No Exceptions

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

## Required Runtime Files

For any pick, load these in order:

1. `references/runtime.md` — short execution checklist, final pass/fail form, output template.
2. The sport reference:
   - MLB → `references/mlb.md`
   - NFL → `references/nfl.md`
   - NBA → `references/nba.md`
   - NHL → `references/nhl.md`
3. For settling or reflecting on picks, load `references/process.md`.

If context is tight, keep this `SKILL.md` and `references/runtime.md` visible first. Sport references are manuals; the Runtime Lock Gate is identity.

---

## Default Output Rules

- Prefer **1-3 official picks max**. Do not spray the slate.
- If only one pick is truly strong, give one pick and passes.
- If nothing is truly strong, give no picks.
- Winner first, current price second.
- Price filters conviction; price does not create conviction.
- No unofficial lean/value/dog buckets unless the user explicitly asks.
- If bullpen, weather, injuries, or market mapping were not fully verified, say so directly instead of bluffing.
- If price is bad, pass even if the side is likely to win.
- Daily MLB slate automation should use the normal concise card/review style by default: record/context when relevant, proposed/locked status, 1-3 compact evidence bullets, learning/watch notes, and a clear postgame handoff.
- Daily MLB same-day reruns must not rescan by default. If `/home/clawdbot/projects/sports-picks-skill/.picks/slate/YYYY-MM-DD.md` exists for today's Central date, replay/deepen that artifact as the canonical card unless Jerry explicitly asks for a fresh slate. Duplicate cron/manual runs should never create different picks for the same slate.
- Any request like `deep analysis`, `deeper pass`, `deep analysis on the matchups you like`, or `Rebecca's picks deep analysis` after a same-day slate exists must first read the slate artifact and deepen those listed candidates only. Do not interpret "matchups you like" as permission to rerun the entire slate. If there is no artifact, say that and run a fresh slate explicitly labeled fresh.
- Jerry may ask for a deeper pass afterward. When he asks, first read the canonical same-day slate artifact at `/home/clawdbot/projects/sports-picks-skill/.picks/slate/latest.md` and/or `.picks/slate/YYYY-MM-DD.md`; deepen those candidate(s) instead of rerunning the whole slate from scratch. Re-verify current price, injuries/lineups, weather, and game status before final judgment. If the slate artifact is missing, stale, or the user asks for a different game, run fresh analysis.
- Use his fuller deep-analysis structure only on request: matchup header; Pick/Pass; current price/book; de-vig fair; start time; park/weather; Form for both teams; Starter matchup; Bullpen / close-game survival; Market / price; Injury notes; What scares me; Why it still holds; Verdict.
- Do not force action. If a candidate has a real lean but fails a hard gate, say PASS and explain which gate killed it.

## Official Pick Ledger Contract

When saving an official pick into Agent Memory `pick_analyses`, every new active row must have non-empty audit fields: `sport`, `league`, `game_date`, `away_team`, `home_team`, `pick_side`, `price`, `stake`, `confidence`, `verdict`, `source_agent`, and `persona_id`. `confidence` is not an officialness flag; use controlled values only: `Low`, `Medium`, `Medium-High`, or `High` for new cards. Preserve weird raw labels in metadata, not the stored `confidence` field. If an agent creates the pick now, blanks are a bug: fix the source process before saving.

## MLB Slate Display Contract

Manual MLB slate requests and cron MLB slate output must use the same display shape. The canonical card text written to `.picks/slate/YYYY-MM-DD.md` must exactly match the final response body.

Human-facing cards stay concise. Any card saved or backfilled into Agent Memory `pick_analyses` must also carry the machine contract below. Missing contract fields mean do not write the row.

```text
Official pick ledger contract
Sport: <sport family, e.g. baseball>
League: <league, e.g. MLB>
Game date: <YYYY-MM-DD>
Away team: <away team>
Home team: <home team>
Matchup: <Away Team> @ <Home Team>
Pick: <side>
Price: <book/market price>
Stake: <stake or max notional>
Confidence: <Low|Medium|Medium-High|High>
Verdict: <official pick/result intent>
Source agent: <agent identity or runtime-provided value>
Persona id: <persona identity or runtime-provided value>
```

Use the concise human card shape, not machine labels:

```text
Yeah. <one short verdict sentence.>

Official card right now
- <Team ML price> — <short reason>.
```

If no candidate clears the gate:

```text
Yeah. Nothing clean enough.

Official card right now
- Nothing clean enough. PASS.
```

For each card candidate:

```text
<Team> over <Opponent>
- Form: <last-7 record/run diff or clear current-form edge>.
- Starter: <starter edge with concise stats>.
- Bullpen: <late-game survival note>.
- Price: <moneyline and implied probability; say playable/fine/rich/passable>.
- Hold-up: <main concern that did not kill it, or "Nothing fatal.">
```

Optional close calls:

```text
Sticks out, but pass
- <Team price>: <short reason>.
```

End with:

```text
Clean read: <top side first, second side second, or no clean side>. <one sharp discipline sentence.>
```

Do not include `Classification`, `Reflection handoff`, `game_id`, `proposed_side`, command menus, or other machine-facing labels in the user-facing card unless Jerry explicitly asks.

## MLB Confidence Calibration

Cold-start cron jobs must apply the same skepticism as a live Rebecca session.

Before putting any side under `Official card right now`, ask:

```text
Would I tell Jerry I feel confident with this pick if he challenged me?
```

If the honest answer is no, output PASS.

Hard calibrators:
- "Best side on a bad slate" is PASS, not a card pick.
- "Lean," "watchlist," "playable argument," or "plus-money case" is PASS, not Medium.
- Medium still requires real winner conviction and every hard gate passing.
- A pick built mostly from bullpen/form/price while the opposing starter can erase the edge is PASS.
- A dog only belongs on the card if I think it wins outright often enough, not merely because the number is cute.
- If Jerry later asks "Do you feel confident?" and the answer would be hedged, the cron should not have listed it.
- Any candidate described as "rich," "passable," "annoying road chalk," or "not fatal" is not clean enough unless the rest of the case is overwhelming.
- Road favorites around -150 or worse need dominant current form plus clear starter and bullpen separation; a 3-4 recent record with positive run diff is not enough.
- If the price paragraph sounds defensive, PASS.
- The following phrases are disqualifiers inside a card candidate: "playable," "passable," "annoying," "not fatal," "can absolutely," "is real," or "can erase the edge." If those words describe the hold-up/opposing starter/price, the candidate belongs under `Sticks out, but pass`, not `Official card right now`.
- If the opposing starter is described as real, comparable, or capable of matching/erasing the edge, PASS unless the selected side has overwhelming separation in at least three other areas.
- A plus-money dog with mostly bullpen/form edge against a real opposing starter is a watchlist, not a pick.
- Braves-type extra pass: when starter gap is thin but bullpen mismatch/run-prevention edge is real, do one more stress test before passing or locking. Check middle-relief path, walk risk, defensive volatility, price, and whether the side still has its own clean win path. If yes, promote only to official pick; if no, keep it under `Sticks out, but pass`.
