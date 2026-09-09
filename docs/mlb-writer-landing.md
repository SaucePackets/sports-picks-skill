# MLB writer landing and retries

`mlb_slate_writer.py --land` preserves an existing schedule's non-derived header, candidates,
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

Non-derived headers and retained JSON objects keep their original spelling and bytes, including
whitespace inside objects, escaped strings, and numeric notation. Separators in
changed arrays may be reformatted. An unchanged landing does not call the write
primitive, so repeated no-ops preserve the complete file and succeed. Unoccupied
reads may be refreshed from a complete validated draft. The derived
`slate_denominator` refreshes from the accepted scan, including its roster,
`scan_sha256`, and artifact `fetched_at_utc`; other header metadata stays intact.
The shared validator still checks game identities against the current scan.

The denominator's `read_bindings` maps each game id to a canonical JSON read digest
and the scan digest accepted when that read landed. Occupied reads retain their
original bindings; legacy reads without bindings remain unknown. These bindings
are separate from retained read objects, so refreshing the denominator does not
rewrite evidence or downstream-owned cards.

The receipt reports `read_scan_bindings` as game-id lists: `current_scan`,
`earlier_scan`, and `unknown`. Missing/malformed bindings or changed read contents
are unknown. Earlier bindings are retained claims, not verification of archived
scan bytes. Like `writer_provenance`, this is diagnostic and does not affect the
verdict or any gate. The schedule hash identifies the resulting merged file.

`acquisition_freshness` is explicitly `not_established`: accepting a draft against
a current scan does not prove its evidence was newly acquired. The denominator's
`fetched_at_utc` is the scan file modification time, not the acquisition time of
retained reads or upstream sources. Source completeness remains a separate receipt
field; it must not be interpreted as reacquisition of all retained reads.

A final read-back refuses a schedule changed during validation. Replacement remains
atomic, but this is not a cross-process transaction with the reviewer or executor:
a noncooperating write after that final comparison is outside this check. No runtime,
review, execution, betting policy, or deployment changes are part of this writer fix.
