# Getting started

This package starts clean.

## First step

Install the package:

```bash
curl -fsSL https://raw.githubusercontent.com/SaucePackets/sports-picks-skill/main/scripts/install-hermes.sh | bash
```

Then start a fresh Hermes session.

## First prompt ideas

- "Use sports-picks. Make an MLB pick for tonight."
- "Use sports-picks. Give me one official pick and pass the rest."
- "Use sports-picks. Analyze this slate and only log picks with real conviction."
- "Use sports-picks. Give me the deeper pass on the official card."

## Important behavior

The `sports-picks` skill is designed to:

- make official picks only when conviction is real
- pass when the edge is weak or the price is bad
- use current sport data, not team reputation
- track official picks in a local `.picks/INDEX.md` or runtime-specific ledger
- review settled picks in `.picks/REFLECTIONS.md`
- promote recurring lessons into `.picks/PROCESS.md`

## What not to do

Bad workflow:

- spray the slate
- force dog/value sections by default
- log broad leans as official picks
- skip postgame reflection after a loss

Good workflow:

- keep the official card tight
- say pass when conviction is not there
- explain why the side made the card
- update the ledger only when the card is locked
- reflect on losses from verified game data, not memory
