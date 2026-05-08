# Sports Picks Runtime Checklist

Use this after loading `SKILL.md` and before loading the sport reference.
This file is the clipboard. `SKILL.md` is the front door.

---

## Runtime Order

1. Identify sport and teams.
2. Load the sport reference.
3. Resolve team IDs and event ID.
4. Pull recent form: last 7 games baseline, last 5 only if it changes the read.
5. Confirm roster/depth chart truth.
6. Confirm starters/goalies/QBs/star availability from live sources.
7. Pull injuries and flag noisy/unclear feeds.
8. Pull primary sportsbook/game line first.
9. Use Kalshi/Polymarket/markets only as supplementary context when it cleanly maps to the exact game.
10. Build full win paths for both teams.
11. Run the final pass/fail gate.
12. Output official picks only.

---

## Final Pass/Fail Gate

Default state:

```text
official_pick_allowed = true
```

Set it false if any hard gate fails:

```text
if starter_floor fails: official_pick_allowed = false
if opposing_starter_shutdown_path fails: official_pick_allowed = false
if my_bullpen_close_game_survival fails: official_pick_allowed = false
if cold_fade_reset fails: official_pick_allowed = false
if price_discipline fails: official_pick_allowed = false
if real_winner_conviction fails: official_pick_allowed = false
```

Decision:

```text
if official_pick_allowed == false:
  output PASS
else:
  official pick may be logged
```

Medium confidence means all hard gates passed but the edge is not elite.
Medium never means a failed gate survived.

---

## Gate Checklist

Before any official card, answer these internally:

- **Starter floor:** can my side survive the first segment without command/traffic/HR damage breaking the game?
- **Opposing starter shutdown path:** what if their starter is good today?
- **My bullpen close-game survival:** if this is close late, who protects innings 7-10 / final possessions / late game state?
- **Cold-fade reset:** am I fading yesterday's version of a team after reset signs appeared?
- **Price discipline:** do I still like the team to win more than I like the number?
- **Real winner conviction:** do I actually believe this team wins most often?

If any answer is weak enough to break the win path, pass.

---

## Always Include When Making a Pick

- Recent form check: last 7 baseline, last 5 only if meaningful.
- Current matchup edge: starter/goalie/QB/star context depending on sport.
- Full win path: how the side wins through early game, late game, offense/defense, and market price.
- Weather/park/rest/injury context when relevant.
- Current price and bettable-to/pass point.
- Market-mapping note: exact-game exchange match, loose sentiment only, or unavailable.
- Why this number may be wrong.
- Flip risk: one sentence on why the other side wins.

---

## Output Template — Default Card

```text
Official card right now
- [Team ML] ([price]) — [confidence]

Why it sticks out
- Form: [last 5-7 game signal]
- Matchup: [starter/goalie/QB/star edge]
- Late-game path: [bullpen/defense/availability]
- Market: [current price, playable to/pass point]
- Gate: all hard gates passed

Passes / close calls
- [Game] — [short reason]

Real card
- [Team ML]
```

Keep it tight. Do not turn every game into a report.

---

## Output Template — Deeper Pass

Use this when the user asks for deeper analysis:

```text
Yeah. Here's the deeper pass.

Official card
- [Team ML] ([price]) — [confidence]

1. [Away] @ [Home] → [Side]

Form
- [Team]: [recent form]
- [Team]: [recent form]

Starter matchup / primary matchup
- [Current-form read]

Bullpen / late-game path
- [Availability and survival read]

Weather / context
- [Relevant or explicitly irrelevant]

What held up on second pass
- [Damage profile, conversion profile, game-shape detail if checked]

Verdict
- [Official pick or pass]
- Gate: [all passed OR failed gate → pass]

Final official card
- [Team ML]
```

---

## Pass Rules

Pass when:

- any hard gate fails
- the current number is worse than the bettable-to price
- the case depends on reputation instead of current evidence
- the team is scoring fewer than 3 runs/game recently and you are laying big juice
- the matchup is close and the edge is not clear
- the number moved and you are chasing steam late
- market data conflicts sharply with your case and you cannot explain why
- confidence is below real-conviction threshold
- the pick is mainly to have action or keep the record moving
- my side's bullpen/late-game path is red in a close-game script without an overwhelming edge elsewhere

No pick is better than a bad pick.
No confidence, no pick.

---

## Props Rule

Props are secondary. Do not surface prop plays unless the user asks for props or the slate analysis explicitly includes them.

Do not make an official prop without:
- exact prop market, line, and odds
- role/workload expectation
- recent form against that line
- opponent tendency that maps directly to the prop
- game-script risk check

If the line moves across a key threshold, re-grade the play.
