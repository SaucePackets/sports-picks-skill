# Install on Hermes

Use the installer from the repo root:

```bash
curl -fsSL https://raw.githubusercontent.com/SaucePackets/sports-picks-skill/main/scripts/install-hermes.sh | bash
```

Then start a fresh Hermes session so the skills are reindexed.

## What the installer does

- Copies every directory under `skills/` into `${HERMES_HOME:-$HOME/.hermes}/skills/sports/`.
- Copies the fresh `.picks/` templates into `${HERMES_HOME:-$HOME/.hermes}/sports-picks/.picks/` if they do not already exist.
- Leaves existing runtime ledgers, receipts, and reflections untouched.

## Optional dependencies

Some helper scripts use external packages or CLIs depending on the runtime:

```bash
pip install sports-skills polymarket-us
```

These are not required just to install the Markdown skills.

## Smoke test

After install and a fresh Hermes session:

```text
Use sports-picks. Show the runtime checklist.
```

For data helpers, test whichever sports package you installed, for example:

```bash
sports-skills mlb get_scoreboard
python ~/.hermes/skills/sports/sports-picks/scripts/polymarket_us_sdk_bet.py health
```
