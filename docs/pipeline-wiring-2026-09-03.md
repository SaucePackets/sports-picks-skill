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
least one **market** field — `polymarket_slug`, `market_slug`, `event_id` —
present on both sides and equal, with nothing disagreeing; `disagree` is a
positive contradiction; `unknown` is an absence of evidence and never rounds to
either. A `watchlist_id` the reviewer *did* write is honoured
but may not be contradicted by the entry it names — without that check a
mis-stamp launders itself, because normalization overwrites the named entry's
`promoted_candidate` with whatever candidate claimed it and the downstream
equality check then compares the forgery against itself.

The side is part of the address, but only as a veto. A slug match alone would
call the other side of the same market a corroborated promotion, so a
disagreeing `side` contradicts. It cannot supply corroboration on its own: a
side is a club name, the same club plays every day, and an entry whose
`promoted_candidate` carries only `side` addresses no market at all. Letting
that agree would corroborate a candidate for a **different market**, and the
failure would be silent — the promotion is then recorded against the watchlist
entry's `game_pk`, so the wrong game's read is relabelled while the
reconciliation identity still balances. An under-specified `promoted_candidate`
is the same transcription failure as a missing `watchlist_id` and gets the same
answer.

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
execution.` The morning job `c9452052719c` carries the explicit routing-language
block saying MLB Polymarket moneyline runs under standing authorization and that
`manual-only` / `no automatic execution` / `awaiting Jerry` must never be
written. The evening pass writes into the *same* schedule and reports the
opposite policy; it is also missing the bug-file age-out paragraph the morning
prompt got. That edit and the profile-scripts sync are **live state**, staged as
the reversible procedure below rather than applied, until this branch passes
review.

## Staged live-edit procedure

Nothing in this section has been run. Both edits touch state outside the repo —
a cron job's prompt and the deployed profile script copies — so they are written
out here in full, with the exact bytes on both sides and a rollback, and applied
only after this branch is gated. Every hash below was read off the deploy host
(ssh alias `saucepackets`, under the production user's `$HOME`) on 2026-09-04;
re-read the before-hash at apply time and **abort if it has moved**, because a
prompt that changed underneath this document is a different prompt than the one
reviewed.

### Edit 1 — evening slate prompt `27087cc00dfa`

State at authoring time, from `~/.hermes/profiles/vig/cron/jobs.json`:

| | bytes | sha256 of the `prompt` field |
|---|---|---|
| before | 16535 | `dbae855c2e15de931739481e9174ee16d002459d972ff6b2526dded834c8fca4` |
| after | 17693 | `decdf0e1e4b7f27ff34bc897f162b5ccfe75c8baac98754abb8efff64e2fcf4e` |

Two replacements, each occurring exactly once in the before-text.

**1a. The report template's closing line.** Current text, verbatim:

```
[Discipline line. Total proposed exposure. Vig review pending. No automatic execution.]
```

Replacement — the morning job's corresponding line plus its routing block,
copied verbatim from `c9452052719c` so the two passes state one policy:

```
[Discipline line. Total proposed exposure. Then state the routing ACCURATELY.]

ROUTING LANGUAGE — do not get this wrong, a report that misdescribes its own
automation is worse than no report. MLB Polymarket moneyline runs under
STANDING AUTHORIZATION: a card, or a watchlist entry promoted at recheck,
passes the Vig review gate and is then executed automatically by the
execution poller. Nobody approves it by hand. NEVER write "manual-only",
"no automatic execution", "awaiting Jerry", or "Vig review required" for
these. The ONLY exception is an entry that literally carries
manual_only=true, which you must not invent. If the card is empty, say the
card is empty and why — do not describe a human approval step that does not
exist.
```

**1b. The bug-file age-out.** The evening task 0 is a byte-exact prefix of the
morning task 0 — the morning gained 509 characters the evening never got. The
edit appends exactly that suffix to the evening's `0. Read ...` line:

```
 AGE-OUT: an entry you could not act on is only worth carrying while it is still true. Drop any entry older than 7 days, and drop any deploy_failed entry whose fix is now live — the runtime tracks origin/main, so a merged PR from weeks ago is deployed. Report an entry at most once; "keep only what I could not act on" without this becomes a permanently unactionable entry re-announced in every slate, which is what happened with a July PR#27 deploy failure from 2026-07-27 until it was cleared on 2026-08-24.
```

(One leading space; it continues the existing line rather than starting a new
paragraph, which is how it sits in the morning prompt.)

**The two replacements are committed files, not text to retype.** Both blocks
above are reproduced here so a reviewer can read them, but the apply step reads
them from the checkout — this lane exists because a rail was keyed on a string
an LLM copied by hand, and a procedure that asks an operator to re-key 741 bytes
of prompt has the same defect.

| file | bytes | sha256 |
|---|---|---|
| `docs/staged/evening-slate-routing-block.txt` | 741 | `fe77d7f502d9935e5f6d48bd80bd87e15719fc812071b27ab2aee992c419a0a8` |
| `docs/staged/evening-slate-ageout.txt` | 512 | `7359312b712975874753f2f503efb34121ba4ec0f0decadf8843ae0df86d45e5` |

Each file carries a trailing newline that the apply step strips; the byte counts
above include it. Because the apply reads them out of
`~/projects/sports-picks-runtime`, Edit 1 — like Edit 2 — can only run once this
branch is merged and that checkout has been updated.

**Apply.** The transform is mechanical, so it is done by a script that refuses
to proceed on a hash it does not recognise, not by hand-editing 16 KB of prompt:

```bash
ssh saucepackets
cd ~
JOBS=~/.hermes/profiles/vig/cron/jobs.json
TS=$(date -u +%Y%m%dT%H%M%SZ)

# 0. Back up the whole store and the single prompt.
cp -a "$JOBS" ~/backups/vig-cron-jobs.$TS.json
python3 - "$JOBS" > ~/backups/evening-prompt.$TS.txt <<'PY'
import json,sys,hashlib
d=json.load(open(sys.argv[1])); jobs=d.get("jobs",d)
jobs=list(jobs.values()) if isinstance(jobs,dict) else jobs
p=[j for j in jobs if j["id"]=="27087cc00dfa"][0]["prompt"]
assert hashlib.sha256(p.encode()).hexdigest()=="dbae855c2e15de931739481e9174ee16d002459d972ff6b2526dded834c8fca4", "prompt moved since review — STOP"
sys.stdout.write(p)
PY

# 1. Produce the new prompt and assert the reviewed after-hash.
python3 - ~/backups/evening-prompt.$TS.txt > /tmp/evening-new.txt <<'PY'
import sys,os,hashlib
ev=open(sys.argv[1]).read()
old="[Discipline line. Total proposed exposure. Vig review pending. No automatic execution.]"
STAGED=os.path.expanduser("~/projects/sports-picks-runtime/docs/staged/")
new=open(STAGED+"evening-slate-routing-block.txt").read().rstrip("\n")
ageout=open(STAGED+"evening-slate-ageout.txt").read().rstrip("\n")
task0=[l for l in ev.split("\n") if l.startswith("0. Read")][0]
assert ev.count(old)==1 and ev.count(task0)==1
out=ev.replace(task0, task0+ageout).replace(old, new)
assert hashlib.sha256(out.encode()).hexdigest()=="decdf0e1e4b7f27ff34bc897f162b5ccfe75c8baac98754abb8efff64e2fcf4e", "not the reviewed text — STOP"
sys.stdout.write(out)
PY

# 2. Install.
PATH=$HOME/.local/bin:$PATH hermes -p vig cron edit 27087cc00dfa --prompt "$(cat /tmp/evening-new.txt)"

# 3. Read back from the store and confirm the after-hash.
python3 - "$JOBS" <<'PY'
import json,sys,hashlib
d=json.load(open(sys.argv[1])); jobs=d.get("jobs",d)
jobs=list(jobs.values()) if isinstance(jobs,dict) else jobs
p=[j for j in jobs if j["id"]=="27087cc00dfa"][0]["prompt"]
print(hashlib.sha256(p.encode()).hexdigest(), len(p))
PY
```

The install is not complete until step 3 prints `decdf0e1…` and `17693`.
`hermes cron edit` rewrites the whole jobs file, so a partial write shows up
here as a hash that matches neither side.

**Roll back.** Same command, the backup as the argument:

```bash
PATH=$HOME/.local/bin:$PATH hermes -p vig cron edit 27087cc00dfa \
  --prompt "$(cat ~/backups/evening-prompt.$TS.txt)"
# then re-run step 3; it must print dbae855c… and 16535
```

If `hermes cron edit` itself is the thing that broke, restore the store whole:
`cp -a ~/backups/vig-cron-jobs.$TS.json "$JOBS"`. Only the `prompt` field of one
job differs between the two copies, so this cannot lose an unrelated edit made
in between — but it also cannot *keep* one, so check the store's mtime first.

**A residual contradiction this edit does NOT fix.** Both prompts still carry
two lines written under the retired policy:

```
- All official candidates require Vig review. Every approval remains manual-only and becomes an awaiting_jerry reminder.
**Review:** Vig check; manual-only
```

The morning job carries these *and* the routing block that forbids exactly those
words, so the correction it got was partial — an earlier version of this document
said the morning job "was corrected", and that was too strong. Bringing the
evening to parity therefore reproduces the contradiction rather than introducing
it. Fixing it is a change to what both jobs are told, with no corrected reference
anywhere to copy from, so it is a judgement call for Rebecca rather than a
mechanical sync, and it is deliberately out of scope here.

### Edit 2 — profile-scripts sync

Read off the VPS on 2026-09-04, comparing
`~/.hermes/profiles/vig/scripts/` against the runtime checkout
`~/projects/sports-picks-runtime` (on `main` at `764fb88`):

- `mlb_slate_receipt.py` — **drifted.** profile `be4d83a63718…`, runtime
  `cbe655914fa5…`. This is the unmanaged copy described above, frozen at
  2026-09-01.
- `mlb_eligibility_report.py` — **in `PROFILE_MANIFEST`, absent from the profile
  scripts dir.**
- `test_vig_review_gate.py` — present in the profile dir, in no manifest.
  Unmanaged and left alone; it is a test file, not an import target of any cron.

Everything else in the manifest matches the runtime byte for byte.

There is no bespoke command for this: the sync **is** `deploy-runtime.sh`, which
already stages, checksums, `py_compile`s, and atomically swaps. Running anything
else by hand would create a second unmanaged copy, which is the defect.

```bash
ssh saucepackets
cd ~/projects/sports-picks-runtime
git rev-parse HEAD                    # must be the merged main containing this branch
scripts/deploy-runtime.sh --dry-run   # expect: would install N manifest files
scripts/deploy-runtime.sh
python3 scripts/vig_runtime_verify.py # checks 5 and 6 must be OK, not just non-FAIL
```

**Roll back.** The installer moves the previous set aside instead of deleting
it, so the rollback is a rename:

```bash
P=~/.hermes/profiles/vig/scripts
ls -d $P.bak-*                        # the newest is the pre-deploy set
mv $P $P.failed-$(date -u +%Y%m%dT%H%M%SZ)
mv $P.bak-<TS> $P
python3 ~/projects/sports-picks-runtime/scripts/vig_runtime_verify.py
```

Ordering matters: **Edit 2 must not run before this branch is merged and the
runtime checkout is updated**, because it deploys whatever `main` holds at that
moment. A deploy is also the only thing that makes a merge take effect — the
merge alone is inert — and per standing instruction a VPS runtime redeploy is
Jerry's call, not something this branch performs.

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
