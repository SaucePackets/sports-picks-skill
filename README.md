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
- `soccer` — soccer/World Cup pick rules and gate criteria.

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
- `skills/sports-picks/references/process.md` — settlement, reflection, ledger maintenance.
- `skills/sports-picks/references/mlb.md` — MLB-specific pick rules.
- `.picks/PROCESS.md` — reusable lessons and process rules.
- `.picks/REFLECTIONS.md` — fresh-start reflection ledger template.

## Runtime state

This repo includes only templates and reusable process files. Live pick history, receipts, watchlists, and execution schedules are runtime state and should stay out of public commits.

### Verify an MLB Vig review handoff

`scripts/vig-review-verify.py` performs a read-only check of a dated MLB review before manual reminders are delivered. It verifies:

- every candidate has a boolean `vig_approved` decision and non-empty `vig_notes`;
- each approved candidate is manual-only, `awaiting_jerry`, unexecuted, and has no execution cron or approval token;
- the canonical `picks.json` has no duplicate active `market_slug` + `side` pair; and
- `.picks/latest-action.md` matches the approval counts and approved exposure/daily cap.

Run it from the sports-picks runtime root:

```bash
python scripts/vig-review-verify.py 2026-07-10
```

The script looks for the pick ledger in `SPORTS_PICKS_LEDGER`, `.picks/picks.json`, `picks.json`, then `~/notes/Sports/picks/picks.json`. Portable or test runs can provide every state file explicitly:

```bash
python scripts/vig-review-verify.py 2026-07-10 \
  --root /path/to/sports-picks-skill \
  --picks-file /path/to/picks.json \
  --latest-action-file /path/to/latest-action.md
```

Exit code `0` means every check passed, `1` means the handoff is inconsistent, and `2` means the date argument is invalid. The verifier never edits schedules, jobs, picks, or latest-action state.

### MLB lineup watchlist rechecks

`scripts/mlb_lineup_watchlist.py` provides deterministic selection and
validation for lineup-dependent near-misses. Pending entries are eligible only
60-90 minutes before first pitch and only when unconfirmed lineups were the
sole original blocker. The scheduled reviewer then refreshes lineups, key
injuries, and price and reruns every original gate. Promotions remain
manual-only `awaiting_jerry` reminders and never create or execute bets.
Watchlist `original_price` and `bettable_to_price` values must be signed
American-odds JSON numbers, not quoted or descriptive strings.

Inspect entries due at a specific instant or validate a schedule:

```bash
python scripts/mlb_lineup_watchlist.py .picks/execute/2026-07-17-schedule.json --now 2026-07-17T21:45:00Z
python scripts/mlb_lineup_watchlist.py .picks/execute/2026-07-17-schedule.json --validate
```

## Manual loading

Any agent that can read Markdown can use the repo:

1. Load `skills/sports-picks/SKILL.md`.
2. Load `skills/sports-picks/references/runtime.md`.
3. Load the relevant sport reference, such as `skills/sports-picks/references/mlb.md`.
4. Use `.picks/` as the local ledger root.
