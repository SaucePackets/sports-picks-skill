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

## Main files
- `skills/sports-picks/` — core pick workflow
- `.picks/INDEX.md` — official record
- `.picks/PROCESS.md` — rules and lessons
- `.picks/REFLECTIONS.md` — post-game reviews
