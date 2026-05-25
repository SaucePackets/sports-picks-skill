# sports-picks-skill

A generic sports-picks skill bundle for Hermes.

It provides one portable workflow for official sports picks: gather sport data, compare market prices, decide pick vs pass, and keep a clean `.picks/` ledger. It is built for discipline, not action-chasing.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/SaucePackets/sports-picks-skill/main/scripts/install-hermes.sh | bash
```

Then start a new Hermes session and ask:

```text
Use sports-picks. Give me tonight's MLB card, and pass anything thin.
```

## What gets installed

The installer copies this bundle into Hermes:

- `sports-picks` — official pick workflow and ledger rules.
- `mlb-data`, `nfl-data`, `nba-data`, `nhl-data` — sport data helpers.
- `polymarket`, `kalshi` — prediction-market helpers.
- `sports-news` — sports news helper.
- `weather` — weather helper for outdoor-game context.

## Repo structure

```text
sports-picks-skill/
├── README.md
├── GETTING-STARTED.md
├── docs/
│   ├── install-hermes.md
│   └── install-openclaw.md
├── scripts/
│   └── install-hermes.sh
├── .picks/
│   ├── PROCESS.md
│   └── REFLECTIONS.md
└── skills/
    ├── sports-picks/
    │   ├── SKILL.md
    │   ├── references/
    │   └── scripts/
    ├── mlb-data/
    ├── nfl-data/
    ├── nba-data/
    ├── nhl-data/
    ├── polymarket/
    ├── kalshi/
    ├── sports-news/
    └── weather/
```

## Key files

- `skills/sports-picks/SKILL.md` — main skill contract.
- `skills/sports-picks/references/runtime.md` — short working checklist.
- `skills/sports-picks/references/pick-process-lanes.md` — slate scan, quick card, full handicap, thesis card, postgame lanes.
- `skills/sports-picks/references/thesis-card-template.md` — official-pick thesis receipt.
- `skills/sports-picks/references/postgame-attribution.md` — process-vs-result settlement labels.
- `skills/sports-picks/references/process.md` — settlement, reflection, ledger maintenance.
- `skills/sports-picks/references/mlb.md` — MLB-specific pick rules.
- `.picks/PROCESS.md` — reusable lessons and process rules.
- `.picks/REFLECTIONS.md` — fresh-start reflection ledger template.

## Runtime state

This repo includes only templates and reusable process files. Live pick history, receipts, watchlists, and execution schedules are runtime state and should stay out of public commits.

## Manual loading

Any agent that can read Markdown can use the repo:

1. Load `skills/sports-picks/SKILL.md`.
2. Load `skills/sports-picks/references/runtime.md`.
3. Load `skills/sports-picks/references/pick-process-lanes.md`.
4. Load the relevant sport reference, such as `skills/sports-picks/references/mlb.md`.
5. For official picks, use `skills/sports-picks/references/thesis-card-template.md`.
6. For settlement, use `skills/sports-picks/references/postgame-attribution.md` plus `skills/sports-picks/references/process.md`.
7. Use `.picks/` as the local ledger root.
