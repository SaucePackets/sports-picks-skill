# sports-picks

Fresh-start OpenClaw skill bundle for data-driven sports picks.

This repo is meant to be copied into another OpenClaw workspace so a new agent can start clean with the `sports-picks` workflow, supporting data skills, and empty pick-tracking files.

## What this includes

### Skills
- `sports-picks`
- `mlb-data`
- `nfl-data`
- `nba-data`
- `nhl-data`
- `weather`
- `polymarket`
- `kalshi`
- `sports-news`

### Fresh-start state
- `.picks/PROCESS.md`
- `.picks/INDEX.md`
- `.picks/REFLECTIONS.md`

## Fresh slate

Yes. This repo starts as a **fresh slate**.

It includes:
- a clean `INDEX.md`
- a clean `REFLECTIONS.md`
- the reusable process rules in `PROCESS.md`

It does **not** include Jerry's real pick history, running record, or personal reflections.

## What the recipient needs

### OpenClaw workspace
The recipient should already have an OpenClaw workspace with a `skills/` directory.

### Dependencies
Several included skills expect the `sports-skills` CLI.

Install it with:

```bash
pip install sports-skills
```

If the default Python is too old, use Python 3.10+ explicitly:

```bash
python3.12 -m pip install sports-skills
```

The `weather` skill also expects `curl` to be available.

## Install

From the root of the recipient's OpenClaw workspace:

```bash
cp -R /path/to/sports-picks/skills/* ./skills/
cp -R /path/to/sports-picks/.picks ./.picks
```

Then start a fresh session, or restart the gateway/session, so OpenClaw reindexes the skills.

## Quick validation

Run a few checks after install:

```bash
which sports-skills
sports-skills mlb get_scoreboard
sports-skills nba get_scoreboard
sports-skills polymarket get_sports_config
sports-skills kalshi get_sports_config
curl -s "wttr.in/Chicago?format=3"
```

## How to use it

Start with `GETTING-STARTED.md` if the recipient wants the shortest path from install to first pick.

Once installed, the agent can:
- use `sports-picks` for official pick analysis
- use the sport-specific skills for scores, team data, injuries, and schedules
- use `.picks/INDEX.md` to log official cards
- use `.picks/REFLECTIONS.md` for post-game review
- use `.picks/PROCESS.md` as the source of process rules and lessons

### Expected workflow
1. Ask for a game pick or slate breakdown.
2. Let `sports-picks` pull the supporting data.
3. Log only real official picks in `.picks/INDEX.md`.
4. After a result settles, review it in `.picks/REFLECTIONS.md`.
5. Promote recurring lessons into `.picks/PROCESS.md`.

### Output expectation
The package includes examples that reinforce three rules:
- structure picks in a clear official-card format
- make official picks only when conviction is real
- distinguish between a true no-edge pass and a side that may be right but is not official-card clean at the current number

See `GETTING-STARTED.md` for a full official-card example, pass examples, and a bad-example contrast.

## Repo layout

```text
sports-picks/
├── README.md
├── .picks/
│   ├── INDEX.md
│   ├── PROCESS.md
│   └── REFLECTIONS.md
└── skills/
    ├── sports-picks/
    ├── mlb-data/
    ├── nfl-data/
    ├── nba-data/
    ├── nhl-data/
    ├── weather/
    ├── polymarket/
    ├── kalshi/
    └── sports-news/
```

## Notes
- `sports-picks` is the main workflow skill.
- The other skills support data gathering and market/weather checks.
- This repo is for OpenClaw, not Hermes.
