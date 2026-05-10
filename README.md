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

## Current proven use

Current production use and refinement has been focused on **MLB game picks**.

The repo still includes the broader sports bundle and supporting skills for NFL, NBA, and NHL, but the most tested official-pick workflow today is MLB.

## MLB pick style

This workflow is not built for blind favorite picks or generic "best bet" dumping.

The current MLB style is:
- side / moneyline focused
- recent form first
- starter matchup matters, but not in isolation
- bullpen edge matters, especially when it changes the late-game path
- who is actually hitting right now matters more than name value
- price discipline matters, always
- live-bet angles are valid when the pregame side is right but the number is not

In practice, that means the workflow tries to answer:
- who is hitting right now?
- who has the cleaner starter path?
- which bullpen looks more trustworthy?
- is the current number still bettable?
- is this a real official pick, or just the side I like more?

## Output expectation

The package is built around three rules:
- default to a **tight official card** with only real-conviction picks
- use a **deeper pass** only when the user wants the full case
- distinguish between a true pass and a side that may be right but is not official-card clean at the current number

## Polymarket execution

Polymarket live trading is guarded, not autonomous chaos.

The repo includes:
- `skills/sports-picks/references/polymarket-trading.md`
- `skills/sports-picks/scripts/polymarket_us_guard.py`

Default behavior is dry-run proposal only. Live orders require Polymarket US API credentials in the runtime environment, explicit Jerry approval, a matching proposal token, and a saved receipt. Credentials never belong in this repo.

## Repo layout

```text
sports-picks-skill/
├── README.md
├── GETTING-STARTED.md
├── DEV_NOTES.md
├── docs/
│   ├── install-hermes.md
│   └── install-openclaw.md
├── .picks/
│   ├── INDEX.md
│   ├── PROCESS.md
│   └── REFLECTIONS.md
└── skills/
    ├── sports-picks/
    │   ├── SKILL.md
    │   ├── scripts/
    │   │   └── polymarket_us_guard.py
    │   └── references/
    │       ├── runtime.md
    │       ├── process.md
    │       ├── polymarket-trading.md
    │       ├── mlb.md
    │       ├── nfl.md
    │       ├── nba.md
    │       └── nhl.md
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
- `sports-picks/SKILL.md` is the front door: when to use it, hard gate, verification rules, and default output rules.
- `sports-picks/references/runtime.md` is the short working checklist for picks.
- `sports-picks/references/process.md` covers settlement, reflections, and record maintenance.
- The other skills support data gathering and market/weather checks.
- Platform differences should live in docs, not in duplicate skill forks.
