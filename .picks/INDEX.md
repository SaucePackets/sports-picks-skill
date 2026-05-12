# Lucy Picks — Index

Running record of official sports picks.
Primary live picks record lives in Sovereign Console `/chat/picks`; this file mirrors locally-audited sports-picks workflow rows.

## Official Card Lock
- Log only official picks I actually feel confident making.
- Log the card immediately once it is locked.
- Do not backfill official picks later from vague chat, broad slate analysis, or memory.
- If chat and this file disagree, verify and fix this file so it remains an audit trail.

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
| 2026-05-12 | Official | MLB | Tampa Bay Rays at Toronto Blue Jays | Tampa Bay Rays ML | Medium | -115 | -130 / 0.56 PM | Pending | — | — | Console pick `3790fcfb-f4df-4c8c-ab36-6db46068ce36`; Polymarket proposal `ae648b4162d43135`, no live order. |
| 2026-05-10 | Official | MLB | Atlanta Braves at Los Angeles Dodgers | Los Angeles Dodgers ML | Medium | -136 | -145 | L | — | — | Braves won 7-2. Elder delivered the shutdown path; Dodgers offense stalled. Console pick `bfd43fc9-b50f-4641-bf1c-7a4a3236da31` settled 2026-05-12. |

## Running Tally
- W: 0
- L: 1
- Pending: 1

## Current Streaks
- Official streak: L1
- Live streak: 0

## Notes
- If closing-line data is unavailable, leave `Close Line` and `Beat Close` as `—`.
- `Bettable To` should be filled in for all new picks going forward.
- Review picks on process, not only result.
- `Pick Type` must be explicit for every row: `Official` or `Live`.
- Official streaks count only `Official` picks. Live streaks should be treated separately.
- Compute streaks by sorting rows by `Date` and following the most recent uninterrupted sequence within each `Pick Type`.
- `Scratched` means a pregame critique or new information broke the official-pick gate before start. Exclude scratched picks from W/L/Pending tally and streaks, but keep the row as an audit trail if it was already logged.

_Last updated: 2026-05-12_
