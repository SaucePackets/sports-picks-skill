# sports-picks-skill

Data-driven sports-picks skill bundle for **Hermes** and **OpenClaw**.

This repo keeps one shared core workflow for official sports picks, supporting data skills, and a clean `.picks/` ledger. Platform-specific setup lives in install docs so the betting logic stays in one place.

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
- reusable process rules in `PROCESS.md`

It does **not** include anyone's live pick history, running record, or personal reflections.

## Install docs

Use the platform-specific install guide that matches the runtime:

- `docs/install-hermes.md`
- `docs/install-openclaw.md`

## Shared workflow rules

Once installed, the agent should:
1. use `sports-picks` for official pick analysis
2. log only real official picks in `.picks/INDEX.md`
3. review settled picks in `.picks/REFLECTIONS.md`
4. promote recurring lessons into `.picks/PROCESS.md`

## Output expectation

The package is built around three rules:
- default to a **tight official card** with only real-conviction picks
- use a **deeper pass** only when the user wants the full case
- distinguish between a true pass and a side that may be right but is not official-card clean at the current number

## Repo layout

```text
sports-picks-skill/
├── README.md
├── GETTING-STARTED.md
├── docs/
│   ├── install-hermes.md
│   └── install-openclaw.md
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
- Platform differences should live in docs, not in duplicate skill forks.
