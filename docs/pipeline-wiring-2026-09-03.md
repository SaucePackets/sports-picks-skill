# The promotion path was wired to a transcription

## What happened on 2026-09-03

Cron `75e72e2dc5be` (the 15-minute MLB card/lineup review gate) exited 1 twice
and then succeeded, and the recorder check failed at the end of the day. Both
are the same event seen from two sides: the review gate promoted a lineup
watchlist entry onto the card, and the rest of the pipeline did not fully
recognise the thing it wrote.

### 22:46 and 23:01 — routing normalization failed closed

```
routing normalization failed closed: candidate
polymarket_slug:aec-mlb-tb-tex-2026-09-03|side:Tampa Bay Rays was not a
targeted candidate or watchlist promotion
```

`normalize_review_routing` accepted a newly approved candidate with no
pre-review twin only when `candidate["watchlist_id"]` named a watchlist entry
this review had marked `promoted`. Recovered from Vig's session store:

- **22:46 / 23:01** (session `20260903_224505_cd77a1`) — the child set
  `"status": "promoted"` and wrote a complete `promoted_candidate` object
  carrying `"watchlist_id": "LW20260903-TB-001"`, then appended the candidate
  to `candidates[]` **with no `watchlist_id` key at all**. `None` was not in
  the promoted set, so the whole cycle was refused.
- **23:16** (session `20260903_231507_57beea`) — same reviewer, same entry,
  same prompt. This time the `candidates[]` element carried the id. Passed.

Nothing raced and nothing was ambiguous. The promotion is written twice, by
hand, in two places — once as `lineup_watchlist[].promoted_candidate` and once
as a `candidates[]` element — and whether the cycle survived depended on one
string being copied into both by an LLM.

The evidence the check needed was already in the file. The promoted entry's own
`promoted_candidate` carries the market slug and the side, and the gate already
derives one side of the pair from the other twenty lines further down — keyed
on the same missing field.

Two things made this expensive to diagnose. The message said *which* candidate
but not which half of the disjunction was absent. And `_restore_pre_review_state`
overwrote the reviewed schedule with the pre-review copy, which is correct — a
refused review must not stay live where the execution poller reads it — but it
was also the only copy. Nothing under `.picks/` recorded that a review had been
refused at all, so reconstructing what the reviewer wrote meant reading an
agent session database on the VPS.

### 23:30 — two candidates, one game_read

```
game_reads gap: 1 defect(s) ... 1 game_reads entries say 'candidate' but the
schedule carries 2 candidates
```

The morning slate owns `game_reads` and writes one read per scheduled game,
once. When the review gate promotes a watchlist entry the game moves into
`candidates[]` and its read still said `lineup_watchlist`.

The worse half is what the identity did next: it is restored when the candidate
*leaves* the card, not when the record is fixed. A wrong per-game record goes
quiet on its own while still claiming we passed a game we carded — and those
reads are the input to the model evaluation lane.

## What changed

**A promotion is recognised by corroboration, not by transcription.**
`resolve_promotion` pairs a newly approved candidate with the promoted entry
whose `promoted_candidate` addresses the same bet — same market slug or event
id, same side — and stamps `watchlist_id` from the entry it matched. The rail
stays closed: a candidate no entry corroborates is still refused, and a
candidate two entries corroborate is refused as ambiguous.

`promotion_address_agreement` returns three answers, not two. `agree` needs at
least one field present on both sides and equal with nothing disagreeing;
`disagree` is a positive contradiction; `unknown` is an absence of evidence and
never rounds to either. A `watchlist_id` the reviewer *did* write is honoured
but may not be contradicted by the entry it names — without that check a
mis-stamp launders itself, because normalization overwrites the named entry's
`promoted_candidate` with whatever candidate claimed it and the downstream
equality check then compares the forgery against itself.

The side is part of the address. A slug match alone would call the other side
of the same market a corroborated promotion.

**The refusal names the missing half, and the refused review is kept.**
`persist_refused_review` writes the child's own output — snapshotted before
normalization touches it — to `.picks/refused/<day>-<sport>-<stamp>.json`
before the restore destroys it. It never raises and never changes the verdict:
a refusal that could not be archived is still a refusal, and it says so out
loud rather than pretending the artifact exists.

**The promoted game's read moves onto the card in the same step.**
`mlb_game_reads.record_promotion_as_candidate` re-labels the read from
`lineup_watchlist` to `candidate`, matched on `game_pk` when the entry carries
one (the only key that separates a doubleheader) and on `event_id` otherwise.
It fails closed: no read, more than one read, or a read saying the card refused
the game each block the promotion.

The re-label is a label change and nothing else, because both dispositions are
in `ACCEPTING_DISPOSITIONS` and impose identical requirements on the read. That
equivalence is pinned by a test rather than asserted here, so a future
candidate-only requirement reds instead of silently making the recorder write
invalid reads.

**The reconciliation identity names its own populations.** `candidates` and
`lineup_watchlist` are not disjoint: a promoted entry stays on the watchlist as
its audit trail while the game owns a `candidates[]` element. Counted in both,
the identity was unsatisfiable — whichever disposition the promoted game's read
carried, one half was wrong. The watchlist half now counts un-promoted entries.
`passed` is still a deferral (the recheck dropped the game; it never reached the
card), and a malformed entry stays in the deferred population rather than
letting the count shrink around junk.

**`vig_runtime_verify` checks ownership and the deployed script copies.**

Two things it could not see before:

- Every cron job's `profile`, compared against the profile its jobs file
  belongs to — read off the `.../profiles/<name>/cron/jobs.json` path, never a
  constant here. On 2026-09-03 every job pointed at the deploy-managed runtime
  and **five of nine carried `profile: null`**, including the MLB evening slate
  that writes the same schedule the morning job writes. `origin` is a warning:
  its absence is a lost audit trail, not a runtime divergence.
- The deployed profile copies against the manifest and against the runtime
  checkout. `deploy-runtime.sh` seeds each stage from the live directory so
  unmanaged files survive every deploy — which means an unmanaged copy of a
  repo script is frozen at the day it was made and no deploy will ever update
  it. `mlb_slate_receipt.py` had been sitting in the Vig profile scripts
  directory since 2026-09-01 in exactly that state, missing the
  `policy_disposition_errors` rail the runtime copy had gained: a receipt that
  would call a slate clean where the gate calls it defective. It is now in
  `PROFILE_MANIFEST`.

The manifest is parsed out of `deploy-runtime.sh` rather than restated in
Python. A test asserts the two readers of that array read the same array.

## Not changed here, and why

**The evening slate prompt still carries the retired execution policy.** Cron
`27087cc00dfa` ends its report template with `Vig review pending. No automatic
execution.` The morning job `c9452052719c` was corrected and carries the
explicit routing-language block saying MLB Polymarket moneyline runs under
standing authorization and that `manual-only` / `no automatic execution` /
`awaiting Jerry` must never be written. The evening pass writes into the *same*
schedule and reports the opposite policy; it is also missing the bug-file
age-out paragraph the morning prompt got. That edit and the profile-scripts
sync are **live state**, staged as a reviewable procedure rather than applied,
until this branch passes review.

## A write nobody claims

`.picks/execute/2026-09-03-schedule.json` was rewritten at **01:25:06Z** on
2026-09-04: the promoted Tampa Bay candidate and the whole `lineup_watchlist`
were gone, and Tampa's read had become `disposition: pass`,
`refusing_rails: [lineups_unconfirmed]`. `latest-action.md` was rewritten in
the same second.

That is not the pipeline healing itself. No cron job ran at that second (the
execution poller fired at 01:24:13 and 01:26:13, both silent), there were zero
ssh logins between 01:15 and 01:32, and the write coincides to the second with
a terminal tool call from a local Vig gateway session whose output was
`validation_errors= [] / candidate_count= 1 watchlist_count= 0`. Nothing was
executed — there has been no order receipt since 2026-08-11.

Rebecca did not make that call. It is recorded here as an **unattributed
investigation artifact** with no owner identified. The post-rewrite file is
therefore not used as a regression fixture anywhere in this branch; every
fixture is built from the pre-rewrite receipts quoted above, which are the
reviewer's own output. The provenance gap itself is the finding: live pick
state can be rewritten by something outside the pipeline with no record on the
box that it happened, and `.picks/` has no audit trail that would have named
the writer.
