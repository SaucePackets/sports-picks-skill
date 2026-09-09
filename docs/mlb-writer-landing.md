# MLB writer landing and retries

`mlb_slate_writer.py --land` preserves an existing schedule's header, candidates,
and watchlist entries. This includes reviewed/executed candidates and promoted
watchlist audit records. Pending entries are also retained: production can append
new games, but changing or removing an existing card belongs to its downstream
owner.

Each incoming card must identify a game by `game_pk` or `event_id`. When both are
supplied, each must resolve to the same scan-backed read. Missing, ambiguous,
contradictory, and duplicate identities fail closed. Doubleheaders remain distinct
by game identity, even when the clubs are identical. A matching existing game is
occupied across both card arrays; the writer skips the incoming card and retains
the existing game read so a promotion cannot revert to a deferral.

Draft dates, read coverage, read contents, watchlist contents, and producer decision
restrictions still validate before merging. Card counts reconcile on the merged
record because an evening draft may omit existing cards. The existing and merged
schedules must both pass the shared validators. The merge also checks card-to-read
game identities. Malformed existing schedules cannot be erased or reset by landing.

Successful CLI output includes `landed: true` and four counts:

- `new_candidates`: candidate entries appended (or created on first landing).
- `new_watchlist_entries`: watchlist entries appended (or created).
- `skipped_occupied_entries`: incoming entries omitted because their game is occupied.
- `retained_occupied_entries`: existing candidate and watchlist entries retained.
  A promoted watchlist audit row and its candidate count as two entries.

Headers and retained JSON objects keep their original spelling and bytes, including
whitespace inside objects, escaped strings, and numeric notation. Separators in
changed arrays may be reformatted. An unchanged landing does not call the write
primitive, so repeated no-ops preserve the complete file and succeed. Unoccupied
reads may be refreshed from a complete validated draft; existing header metadata,
including denominator provenance, remains intact and is checked against the current
scan roster by the shared validator.

A final read-back refuses a schedule changed during validation. Replacement remains
atomic, but this is not a cross-process transaction with the reviewer or executor:
a noncooperating write after that final comparison is outside this check. No runtime,
review, execution, betting policy, or deployment changes are part of this writer fix.
