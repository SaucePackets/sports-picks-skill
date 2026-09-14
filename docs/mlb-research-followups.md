# MLB research follow-ups

## Behavior

The existing 15-minute MLB review entrypoint runs its normal review first, then
`mlb_research_queue.run_cycle` and `mlb_research_producer.dispatch_ready`, then
refreshes its receipt and decision audit. Soccer and the execution poller do not
enter this lane. The deployment manifest includes both new modules.

Research state is separate from executable candidates and the lineup watchlist:
`.picks/research/YYYY-MM-DD/queue.json`. The full scan roster is the denominator.
The source must have a matching coverage hash and reconciled identities. Missing
or legacy unverified scans are reported explicitly and never used for collection;
malformed or contradicted coverage fails the cycle. An existing candidate or
watchlist entry owns its game and suppresses research for that identity.

Each game retains its initial source row, scan digest, immutable attempt objects,
missing fields, next retry, deadline, and disposition. Queue updates use an atomic
replace under a nonblocking per-day lock. Retries are claimed durably before I/O;
a crash consumes its attempt rather than resetting the budget. A repeated scan
cannot reset retry counts. Changed identities/start times are blocked for producer
review. A removed game is explicitly invalidated. Previous-day unfinished work
expires without reading its old source or issuing network calls.

## Operational limits (not betting thresholds)

- Open research at scheduled start minus three hours.
- Retry no more often than every 15 minutes; at most 12 attempts per game.
- Refresh at most four due games per cycle, ordered by retry time and deadline.
- Each refresh has a 60-second request-start budget, at most 12 source requests,
  and single-shot HTTP calls with at most 10-second network timeout. These are
  transport/request limits, not a guarantee that every OS operation terminates
  within precisely 60 wall-clock seconds.
- Stop research at scheduled start minus 15 minutes. A late response cannot become
  ready. The provider must still identify the same game, teams, start, and Preview
  state. Postponements/start changes require a producer rescan/review.
- At most two producer attempts per ready game. Each scan/agent subprocess has
  a timeout capped at 300 seconds and further bounded by half the available
  deadline minus 30 seconds. Check the deadline again before landing.

These initial operational settings are deliberately explicit so operational
coverage can be measured. They do not change the edge floor, bankroll limits,
confidence gates, model admission, or execution authorization.

## Refresh and producer ownership

Refresh only missing source fields, plus starter statistics when the current
probable starter changes. Reuse existing scanner methods for form, bullpen,
starter statistics and injuries. Refresh missing offense with a bounded Savant CSV request instead of relabeling its daily cache. ESPN price/injury lookups must match
its event ID, start, and both team abbreviations; MLB numeric IDs are never used
as ESPN IDs. Complete batting orders use the existing source corroboration
contract. Retained fields keep their original observations; refreshed field
metadata is separate. Unresolvable identity/collection gaps remain missing.

Normalized responses and local retrieval times are operational diagnostics.
They are not original HTTP-byte receipts, independently trusted timestamp
proof, or inputs automatically admitted to a prospective model evaluation.

`ready_for_producer` means data fields recovered, not that a pick qualifies.
The same cycle consumes ready packets through the bounded producer:

1. The orchestrator runs the existing full Stage 2 scanner with a new retained
   nonce, preserving stdout/stderr in the run directory. A full scan is required
   here to satisfy the existing full-roster writer contract; it is not repeated
   on every missing-lineup poll.
2. The orchestrator creates the draft through `mlb_slate_writer.skeleton`.
3. The agent reads the current sport/runtime references, policy, ledger, scan,
   and packet; fills only that draft; and retrieves current prices/card evidence.
   It may not call the writer, mutate canonical records, approve, or execute.
4. The orchestrator invokes `mlb_slate_writer.land` itself. A model response saying
   “done” cannot substitute for a filled draft or a valid nonce. Existing cards,
   retained reads, schema checks, and candidate admission stay writer-owned.
5. The postflight receipt must pass. Supported decisions are recorded in queue
   state; unresolved inputs return to pending or exhaust their retry budget.
   New proposals await the normal reviewer on a later cycle. Execution stays
   subject to its existing separately paused poller and policy.

Per-run draft, agent logs, invocation, and receipt remain under
`.picks/research/YYYY-MM-DD/producer/<nonce>/`. Failures retain their artifacts.
A failed scan can leave the normal Stage 2 artifact newer than the last landed
schedule, just as an interrupted regular producer can today; receipts flag this
rather than label it a successful slate. Concurrent ordinary producers remain
subject to the writer's existing nonce and optimistic schedule-change checks.

The decision audit retains `incomplete_without_linked_recheck` (the original
executable-watchlist question) and separately adds research status, retry,
deadline, and `incomplete_without_tracked_followup`. Tracked research is not proof
that the research succeeded; inspect its status and attempts.

## Validation and activation

The full candidate suite passes 1,813 tests and 714 subtests, with one optional
sports_skills integration skipped. Behavioral coverage includes retry timing,
budgets, missing/corrupt source, expiry across days, occupied cards, identity
mismatches, retained timestamps, correlated ESPN lookup, real writer landing,
wrong nonces, empty drafts, self-approval, failure retention, and terminal state.

An isolated server queue initialized ten pending games without premature requests.
A separate direct refresh obtained one live MLB feed: Cleveland's home lineup
had become confirmed while the away lineup remained unconfirmed. The retained
row observation timestamp stayed unchanged. This is a collection smoke test,
not a live agent/producer acceptance test. New code is not deployed before merge.

After merge, use the merged deployment driver, verify all 28 managed files, and
observe one natural cycle through recovered inputs, producer draft, writer land,
receipt, audit, and later review. Verify a supported pass as carefully as a new
candidate. Keep execution paused. Track missing-source expiry, producer failures,
and percentage of games with a supported pregame decision before claiming the
research gap is closed in production.
