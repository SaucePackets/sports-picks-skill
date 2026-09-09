# MLB acquisition completeness

The September 9 retained Stage 2 scan contains 15 games, no lineup records,
one missing Arizona offense row, and two games without two-sided ESPN prices.
Those are coverage failures/absences, not evidence of profitable missed bets.
The exact source fixture is retained in
`tests/fixtures/mlb-data-completeness/stage2-2026-09-09.json`.
It contains public baseball context and no betting orders or ledger records.

Code inspection found three concrete mechanisms:

- `mlb_stage2_scan.py` normalized the consumer's abbreviation in one direction,
  but stored Savant offense under the unnormalized source key. An `AZ` source
  row could not be found by an `ARI` consumer. The regression tests recreate
  that lookup in both directions, including cached data. The original fixture
  alone does not prove what was in the source cache at acquisition time.
- The collector had no lineup acquisition call. Thus this scan cannot establish
  that the picks job obtained confirmed lineups elsewhere.
- Injury failures returned `[]` and successful responses were truncated to ten
  entries. Other per-game exceptions could discard independently available
  fields and leave a matched MLB game to be emitted again as unmatched.

## Resulting contract

`mlb_data_completeness.canonical_team` is the single alias resolver used on
both sides of the schedule join and offense lookup. Unknown aliases and
conflicting source aliases fail closed. Sources are not silently renamed to
an unrelated team. Savant CSV requests retry transient network errors, 429s,
and 5xxs at most three times; permanent HTTP errors propagate. JSON requests
retain the existing shared HTTP retry/mirror behavior.

Each matched game fetches the official MLB
`https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live` after collecting
slower statistics and injuries. A September 9 read-only inspection of game
824064 verified the concrete `gameData.teams`, `gameData.datetime`,
`gameData.status`, and `liveData.boxscore.teams` shape used by the parser.
The parser requires a Preview/Scheduled or Preview/Pre-Game observation before
first pitch, matching game/team IDs and start time, and nine unique player IDs
with names, parent-team IDs, starting slot codes 100–900, and explicit
`isSubstitute: false`. `confirmed` means a corroborated published pregame
order; the feed does not provide a separate confirmation boolean. It does not
promise there will be no subsequent scratch.

Coverage requires retrieval within 30 minutes, no future timestamp, and a
still-pregame observation. The 30-minute constant is an acquisition freshness
limit, not a betting threshold. Re-fetch stale evidence; never re-stamp it.

Independent failed fields remain null with `source_failures`; successes survive.
A failed injury lookup is distinct from a successful empty injury list. A
truncated injury response is an explicit failure, not partial success.
Required source fields and lineup errors are listed per game. Unknown park
factors retain the existing documented uncertainty treatment and are not made
a new hard gate. Coverage does not imply a complete weather, news, leverage-arm,
or model analysis; those existing review requirements remain separate.

The collector writes a coverage sidecar bound to the exact scan bytes. Its
schedule count comes from MLB enumeration, checked against `totalGames` when
present. Status counts sum to all emitted rows. Duplicate IDs and unmatched
extra ESPN rows prevent reconciliation. Missing MLB schedule data has an
unknown denominator, never an honest zero. Non-reconciliation or an output
write failure gives the scanner a nonzero exit. Writer skeleton and landing
reject versioned scans without a valid reconciled coverage receipt, including
an empty scan caused by an outage.

Source readiness is partitioned into `incomplete_input_data`, `not_priced`, and
`ready_for_evaluation`. Simultaneous missing inputs and prices use the first
status while separately counting unpriced games. The scanner never invents a
`pass`. Game reads may explicitly record disposition `incomplete_input_data`
with its required named rail. When landing versioned scans, source fields are
rechecked against the read identity and current time; `pass` and `candidate`
require complete source coverage, while the existing lineup-only watchlist
exception remains allowed. No price/edge thresholds, selection policy files,
review authorization, or order execution code changes.

The slate receipt reports current source coverage alongside its existing
record-completeness verdict. An old unversioned scan remains readable, but
cannot establish verified lineup coverage or a verified acquisition denominator.
Version detection is compatibility handling, not tamper-proof admission: an
actor replacing both a scan and its sidecar could fabricate evidence. No such
security claim is made. Freshness evaluated later can expire; coverage is an
as-of observation, not a permanent property of a recorded game.

## Validation and rollout

Tests cover alias direction and unknowns, conflicting aliases, cache and retry
behavior, empty/duplicate/wrong-team/wrong-game/substitute lineup data, time
boundaries, retained independent fields, source outages, duplicate counts,
receipt byte binding, writer call-site enforcement, and the historical fixture.
Run focused tests plus the full repository suite with declared dependencies.
The exact main base is `ad9b8d93f5b21203aa6865f90bcc4db4a5809cbe`.

Repository review is separate from deployment. The manifest includes the new
module. After Jerry merges and authorizes deployment, verify the manifest and
run the actual picks workflow, checking coverage receipts and source freshness.
No cron, runtime checkout, or live policy was changed during implementation.

Validation on this branch's implementation: focused tests 245 passed, 62
subtests passed. Full suite 1,637 passed, 1 skipped, 703 subtests passed, with
four failures also reproduced at the exact base (1,598 passed, 1 skipped,
702 subtests passed): one existing execution lock fixture failure and three
existing standing-authorization/watchlist fixture failures. These runs used
Python 3.14.7 with the declared execution/shadow dependencies.
A read-only official-feed check at 2026-09-09T20:10:54Z parsed both Arizona and
Kansas City published orders as confirmed with nine corroborated players each.
This is acquisition-parser evidence, not a deployed cron or end-to-end picks
workflow result.
