# Install on Hermes

Use this when the target runtime is **Hermes**.

## Goal
Install one shared core skill bundle and keep a single canonical `.picks/` directory so the workflow stays consistent across sessions.

## Requirements

```bash
pip install sports-skills
```

If needed:

```bash
python3.12 -m pip install sports-skills
```

Also make sure `curl` is available.

## Recommended layout

Keep the core skill content together:

```text
~/.hermes/skills/<category>/sports-picks/
~/.hermes/skills/<category>/sports-picks/.picks/
```

If you prefer a workspace-level `.picks/`, that is fine too — just keep **one** canonical path and make sure your workflow uses it consistently.

## Install

Copy the repo skill folders into the Hermes skill tree and place `.picks/` in the agreed canonical location.

Example:

```bash
mkdir -p ~/.hermes/skills/custom
cp -R /path/to/sports-picks-skill/skills/* ~/.hermes/skills/custom/
cp -R /path/to/sports-picks-skill/.picks ~/.hermes/skills/custom/sports-picks/.picks
```

Adjust the destination path if your Hermes setup uses a different category layout.

## Reload
Reload Hermes or start a fresh session so the skill is reindexed.

## Validation

```bash
which sports-skills
sports-skills mlb get_scoreboard
sports-skills nba get_scoreboard
sports-skills polymarket get_sports_config
sports-skills kalshi get_sports_config
curl -s "wttr.in/Chicago?format=3"
```

## Notes
- The skill logic stays shared with OpenClaw.
- Hermes/OpenClaw differences should live in install docs, not in separate skill forks.
- Sportsbook line stays primary. Exchange checks stay supplementary unless they map cleanly to the exact game.
