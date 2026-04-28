# Lucy Picks — Index

Running record of official sports picks.
Primary picks record lives here under `.picks/`.

## Official Card Lock
- Log only official picks I actually feel confident making.
- Log the card immediately once it is locked.
- Do not backfill official picks later from vague chat, broad slate analysis, or memory.
- If chat and this file disagree, verify and fix this file so it remains the source of truth.

## Format
Each pick tracks:
- Date, pick type, sport, matchup
- Pick + confidence level
- Line at pick time
- Bettable-to price
- Result (W/L/Pending/Scratched)
- Closing line (when available)
- Beat close? (Yes/No/—)
- Short edge thesis / notes

## Record
| Date | Pick Type | Sport | Matchup | Pick | Conf | Pick Line | Bettable To | Result | Close Line | Beat Close | Notes |
|------|-----------|-------|---------|------|------|-----------|-------------|--------|------------|------------|-------|

## Running Tally
- W: 0
- L: 0
- Pending: 0

## Current Streaks
- Official streak: 0
- Live streak: 0

## Notes
- If closing-line data is unavailable, leave `Close Line` and `Beat Close` as `—`.
- `Bettable To` should be filled in for all new picks going forward.
- Review picks on process, not only result.
- `Pick Type` must be explicit for every row: `Official` or `Live`.
- Official streaks count only `Official` picks. Live streaks should be treated separately.
- Compute streaks by sorting rows by `Date` and following the most recent uninterrupted sequence within each `Pick Type`.
- `Scratched` means a pregame critique or new information broke the official-pick gate before start. Exclude scratched picks from W/L/Pending tally and streaks, but keep the row as an audit trail if it was already logged.

_Last updated: TEMPLATE_
