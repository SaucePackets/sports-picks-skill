# Getting started

This package starts clean.

## What that means
- `.picks/INDEX.md` starts empty
- `.picks/REFLECTIONS.md` starts empty
- `.picks/PROCESS.md` keeps the reusable workflow rules

So a new user gets the system, not someone else's history.

## First step
Install the package using the platform-specific guide first:

- `docs/install-hermes.md`
- `docs/install-openclaw.md`

This file is for **workflow and usage**, not platform setup.

## First validation

After install, run:

```bash
which sports-skills
sports-skills mlb get_scoreboard
sports-skills nba get_scoreboard
sports-skills polymarket get_sports_config
sports-skills kalshi get_sports_config
curl -s "wttr.in/Chicago?format=3"
```

## First prompt ideas
- "Make an MLB pick for tonight"
- "Give me one official pick and pass the rest"
- "Analyze this slate and only log picks with real conviction"
- "Give me the deeper pass on the official card"

## Important behavior
The `sports-picks` skill is designed to:
- make official picks only when conviction is real
- pass when the edge is weak or the price is bad
- use recent form and matchup data, not team reputation
- track official picks in `.picks/INDEX.md`
- review settled picks in `.picks/REFLECTIONS.md`
- promote recurring lessons into `.picks/PROCESS.md`

## Example output shapes

### Fast official-card format

```text
Yeah. A few stick out.

Official card right now

• Cubs ML
• Cleveland ML

That is the cleanest two.

Why they stick out

1. PHI @ CHC → Cubs

• CHC last 7 scoring: 7.57/game
• PHI last 7 allowing: 6.86/game
• PHI last 7 scoring: 3.57/game
• line is only CHC -115

What I like:

• biggest current-form gap on the slate
• Chicago bats are still alive
• Philly is cold and leaking runs

───

Passes / close calls

Braves

• right side maybe
• not official-card clean at the number

Real card

• Cubs ML
• Cleveland ML
```

### Deeper official-card format

```text
Yeah. Here’s the deeper pass.

Official card

• Cubs ML (Medium-High)
• Cleveland ML (Medium)

That’s the real card.

───

1. PHI @ CHC → Cubs (Medium-High)

Form

• PHI last 7 scoring: 3.57/game
• PHI last 7 allowing: 6.86/game
• CHC last 7 scoring: 7.57/game
• CHC last 7 allowing: 4.57/game

Starter matchup

• Aaron Nola vs Colin Rea

Bullpen

• Philly side looks a little more stressed
• Chicago is not spotless, but not clearly worse

Weather

• 49°F, light wind
• not a real chaos signal

What held up on second pass

Nola is the better single arm, but the current full-game gap still points Chicago.

Verdict

Play: Cubs ML
Bettable to: -125
Confidence: Medium-High

───

Final official card

• Cubs ML
• Cleveland ML
```

## What not to do

Bad workflow:
- spray the slate
- force dog/value sections by default
- log broad leans as official picks
- skip postgame reflection after a loss

Good workflow:
- keep the official card tight
- say pass when conviction is not there
- explain *why the side made the card*
- update the ledger the moment the card is locked
- reflect on losses from verified game data, not memory
