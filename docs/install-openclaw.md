# Install on OpenClaw

Use this when the target runtime is **OpenClaw**.

## Goal
Install the same shared core skill bundle used by Hermes while keeping OpenClaw-specific setup steps separate.

## Requirements

```bash
pip install sports-skills
```

If needed:

```bash
python3.12 -m pip install sports-skills
```

Also make sure `curl` is available.

## Install
From the root of the recipient's OpenClaw workspace:

```bash
cp -R /path/to/sports-picks-skill/skills/* ./skills/
cp -R /path/to/sports-picks-skill/.picks ./.picks
```

## Reload
Start a fresh session or restart the gateway/session so the skills are reindexed.

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
- This uses the same core betting logic and `.picks/` structure as the Hermes install.
- OpenClaw-specific differences should stay in this doc, not in a separate fork of the skill.
- Sportsbook line stays primary. Exchange checks stay supplementary unless they map cleanly to the exact game.
