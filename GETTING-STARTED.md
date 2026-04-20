# Getting started

This package starts clean.

## What that means
- `.picks/INDEX.md` starts empty
- `.picks/REFLECTIONS.md` starts empty
- `.picks/PROCESS.md` keeps the reusable workflow rules

So a new user gets the system, not Jerry's history.

## First-use setup
1. Copy the `skills/` folders into the target OpenClaw workspace `skills/` directory.
2. Copy `.picks/` into the target workspace root.
3. Install `sports-skills`.
4. Make sure `curl` is installed.
5. Start a fresh session or restart so OpenClaw reindexes the skills.

## First validation
Run:

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
- "Who do you have in Knicks vs Celtics?"
- "Give me one official pick and pass the rest"
- "Analyze this slate and only log picks with real conviction"

## Important behavior
The `sports-picks` skill is designed to:
- make official picks only when conviction is real
- pass when the edge is weak or the price is bad
- use recent form and matchup data, not team reputation
- track official picks in `.picks/INDEX.md`
- review settled picks in `.picks/REFLECTIONS.md`

## Example output shape

### Good official-card example

```text
Yeah. Here’s the deeper pass.

Official card

• Cubs ML (Medium-High)
• Cleveland ML (Medium)

That’s the real card.

I am passing Braves today. Right side maybe, but not official-card clean at that number with Bryce Elder.

───

1. PHI @ CHC → Cubs (Medium-High)

Form

• PHI last 7 scoring: 3.57/game
• PHI last 7 allowing: 6.86/game
• CHC last 7 scoring: 7.57/game
• CHC last 7 allowing: 4.57/game

That’s still a massive offensive gap.

Starter matchup

• Aaron Nola vs Colin Rea

This is the ugly part for Chicago.
On pure single-arm quality, Nola is the better pitcher.

So this is not a clean SP-edge play.
It’s a full-game shape play again:

• Cubs bats are hotter
• Phillies recent run prevention has been rough
• Chicago is still getting a short price at home

Bullpen

PHI bullpen

• Tanner Banks: likely limited
• Tim Mayza: likely limited
• Kerkering / Shugart: some recent use

CHC bullpen

• Jacob Webb: likely limited
• a couple other moderate-use arms
• not spotless, but not clearly worse than Philly

So bullpen does not kill Chicago here. If anything, Philly’s side looks a little more stressed.

Weather

• Chicago: 49°F, light wind
• not great hitting weather, but also not a real concern
• no major chaos signal here

What held up on second pass

The key question is:
does Nola’s edge outweigh the current full-game gap?

For me, no.
Not at CHC -115.

Verdict

This still looks like a real official pick.

Play: Cubs ML
Bettable to: -125
Confidence: Medium-High

───

2. HOU @ CLE → Cleveland (Medium)

Form

• HOU last 7 scoring: 4.0/game
• HOU last 7 allowing: 5.57/game
• CLE last 7 scoring: 5.29/game
• CLE last 7 allowing: 4.0/game

Cleveland has the cleaner recent two-way profile.

Starter matchup

• Spencer Arrighetti vs Slade Cecconi

This is not some sexy ace matchup.
But it also does not force me off Cleveland.

If anything, this is the kind of game where I care more about:

• who is playing better baseball
• who has the cleaner run prevention profile
• whether the price is still fair

Bullpen

Houston bullpen

• multiple heavier-use / limited flags
• Kai-Wei Teng, AJ Blubaugh both look pretty worked
• several others in the maybe-limited bucket

Cleveland bullpen

• not untouched
• but cleaner than Houston overall

That matters. Houston’s relief picture looks shakier here.

Weather

• Cleveland: 39°F
• cold weather, so offense could flatten some
• I’d call this a mild concern, not a flip

If scoring gets suppressed, that tends to make bullpen stability and clean run prevention matter more.

What held up on second pass

Cleveland still looks like:

• better recent offense
• better recent run prevention
• better bullpen shape
• still only -122

That’s enough.

Verdict

Not as clean as Cubs, but still official-card worthy.

Play: Cleveland ML
Bettable to: -130
Confidence: Medium

───

Passes

ATL @ WSH → Pass

• Braves are probably the better team
• but -163 with Bryce Elder is where I back off
• I do not want to pay hot-team tax just because they’re rolling

LAD @ COL → Pass

• Dodgers are the side
• number is stupid again

BAL @ KC → Pass

• interesting
• not enough separation

CIN @ TB → Pass

• some appeal
• not enough conviction

Final official card

• Cubs ML
• Cleveland ML
```

### Good short pass example

```text
⛔ Pass: COL @ SD

Padres are probably the right side.
The price is too expensive, the edge is not clean enough, and that is not an official pick.
```

### Pass discipline rule

A pass is not always the same thing.
Sometimes it means:
- there is no real edge
- the price is bad even if the side is probably right
- the side is interesting, but not official-card clean
- the data is incomplete, so confidence cannot clear the bar

Do not blur these together.
Be explicit about **why** it is a pass.

### Bad example

Do **not** do this:
- spray 4 to 8 picks just to fill the slate
- call something an official pick when the case is thin
- use vague language like "I kind of like" or "lean"
- force a favorite when the offense is cold and the price is bad
- treat "probably the right side" as the same thing as an official pick

## Main files
- `skills/sports-picks/` — core pick workflow
- `.picks/INDEX.md` — official record
- `.picks/PROCESS.md` — rules and lessons
- `.picks/REFLECTIONS.md` — post-game reviews
