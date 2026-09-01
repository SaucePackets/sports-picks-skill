# Replaying the gate over 2026-08-11..08-31

`scripts/vig_slate_gate_replay.py` reads the slate documents a window produced,
reconstructs the four numbers the MLB gate consumes, runs the gate's own
arithmetic over them, and grades the hypothetical selections against the cached
finals. It places no order, changes no gate, writes no file, reads no live
state, and never fetches.

It exists because every corpus this repo has studied takes a **priced candidate**
as its unit, and this window produced two of them. "Would a different rail have
bet anything" had no population to ask of. The inputs were not missing — they
were in the prose, unrecorded.

## Reproducing the run

```
python scripts/vig_slate_gate_replay.py \
    --picks-dir ~/projects/sports-picks-runtime/.picks \
    --also-picks-dir ~/projects/sports-picks-skill/.picks \
    --since 2026-08-11 --until 2026-08-31
```

Both roots matter: 2026-08-20's slate exists only in the second one, and
2026-08-22 has a **different** document in each. `--json` emits the full record
including every unparsed block and every `unavailable` field's reason and raw
prose.

The rails are constructed from repo constants (`--edge-floor`,
`--max-bets-per-day`), not read from the live `risk_limits.json`. A report whose
meaning changes when someone edits live state is not a reproducible artifact —
the same reasoning `vig_historical_audit` states for reading the floor as a
constant. The values used are recorded in the output.

## What the numbers mean, and what they do not

**Coverage is a first-class column, not a footnote.** The denominator is the
cached MLB schedule, because the count of blocks a run happened to write
measures that run's verbosity. Days with no cached schedule report a null
denominator rather than falling back to the block count, which would read as
100% exactly where coverage is least known.

**Every field carries a provenance.** `reconstructed` (de-vigged from the
two-sided American line by `mlb_stage2_scan.devig`), `recorded` (stated and
side-labelled in the prose), `inferred_order` (stated but NOT side-labelled, so
the away/home assignment rests on a writing convention), `unavailable` (with a
reason, and the raw prose retained). Only the first two count as faithful.

The line is read from the block body and, where the body has none, from the
block **title** — 2026-08-26 writes it there and nowhere else
(`— KC +136 / TOR -146:`). The title form requires both club tokens, because
the body form's `DK` prefix is what licenses falling back on written order and
a title has no such anchor; so it can only ever add a side-resolved reading.

**Outcomes are reported per fidelity and never combined.** A selection whose
price orientation rests on a writing convention is weaker evidence than one
whose sides were labelled, so `graded`, `wins`, `losses` and `units` exist only
inside `by_fidelity` — there is no combined key in the report or the render. In
this window that is not a formality: both 2026-08-26 selections, including the
only win, carry `inferred_order` on their Polymarket ask.

Where a block writes a side-labelled line **and** a pair in bare order that
agrees with it side for side, the block's writing convention was measured
rather than assumed, and the game is flagged `written_order_corroborated`. It
stays `inferred_order` regardless: agreement on the fair pair is evidence about
the ask pair's orientation, not proof of it, and the two were written by
different steps.

**Prices are asks.** No Polymarket quote receipt exists anywhere in this window;
the three in `receipts/polymarket` all belong to the last executed pick, on
2026-08-10. The traded price is genuinely unavailable and is not modelled. Two-
sided ask sums run at a median of 1.005, which bounds how much a mid could have
improved either side, and that sum is reported rather than averaged away.

**The two populations never blend.** `market_only` is the configuration the
deployment gate forces when no model is deployed — raw equals `dk_fair_prob`,
haircut zero — so the conservative edge is exactly `dk_fair - ask`.
`recorded_handicap` needs the prose to have stated both a win probability and an
uncertainty haircut. A stated handicap with no stated haircut is **not**
completed with zero: zero is the fallback's own value, and borrowing it would
relabel a handicapped game as a market-only one.

**Clearing the floor is not being bet.** The gate also caps the day, so
`enforce_daily_candidate_limit` is applied per card. On 2026-08-22 five games
clear the floor and the cap allows two. The cap is **2 per card**; the window
total of four is two cards each filling it.

**A comparison between documents needs two documents.** 08-11 and 08-15..08-18
exist byte-identically in both `.picks` roots, and a copy of a file is not a
second opinion about a price, so copies are excluded from the cross-document
metric and the exclusion is counted. A matchup a *single* document prices twice
is a doubleheader — two games, not one game with two prices — so it is excluded
and named rather than compared. That refusal is why 2026-08-17's St. Louis at
Cincinnati no longer appears as a document conflict: both prices are in the
same file, and they are DH1 and DH2.

## Findings

### The recorded-handicap population is empty, and the reason is the record

38 blocks state a win probability. **Zero** state an uncertainty haircut. So no
game in the window can be replayed under our own handicap without inventing the
buffer, and the report says so rather than reporting a rate over an invented
one. This is a fact about what the runs wrote down, not about the model — and it
is exactly the gap `game_reads` (PR #74) closes going forward.

### Market-only, the floor is unreachable, and the measurement corroborates

Best available edge per game across 131 evaluable games: median **+0.001**, p90
**+0.025**, max **+0.088**. The drought diagnostic reached the same quantity from
the structured schedule records rather than from prose and got median +0.001,
p90 +0.030, max +0.085 over 79 games. Two independent readings of one book
agreeing to within half a point is the strongest evidence available that this
report's prose parsing is not systematically wrong.

Seven games clear the 0.05 floor and the per-card cap keeps four. Their record
is reported per fidelity and is never added up:

| fidelity | evaluable | clearing | kept by the cap | graded | units |
|---|---|---|---|---|---|
| faithful | 51 | 5 | 2 | 0-2 | **-2.00** |
| inferred_order | 80 | 2 | 2 | 1-1 | **+0.02** |

The split is the finding, not the presentation. Both `inferred_order`
selections are the two 2026-08-26 games, and one of them is the window's only
win — so a combined "1-3" would describe a record that is **0-2** where the
prices were side-labelled in the text. Both of those blocks do carry
`written_order_corroborated`: 08-26 writes a side-labelled DraftKings line in
the block title and its bare-order fair pair agrees with it, so the convention
was checked in-block rather than assumed. That is evidence, not proof, and it
does not move them into the faithful half. Neither half is a result anyone
should act on, for the reason below.

### Every clearing game comes from one price capture, and a second capture of
### the same slate disagrees with it

Five of the seven clearing games are on 2026-08-22, which has two documents: a
10:30 CT card in `sports-picks-skill` covering fifteen games, and a 12:59 CT card
in `sports-picks-runtime` covering thirteen. They price the same games and their
Polymarket asks differ by up to **9.5 points** — and the later card's asks
disagree with the DraftKings line recorded beside them in the same document,
while the earlier card's match their own.

Under the earlier capture, **zero** games clear the floor. Under the later one,
five do. A report that preferred one root would have stated one of those two
answers with no way for a reader to tell which document they were reading. That
is why both copies of every date are carried, and it is the single most
important caveat on the numbers above.

Which capture is right is **not decidable from this repo**. It needs a quote
receipt, and this window has none. The remaining three disagreements are all on
08-27, between that day's morning and evening cards, and are half-point drift
that looks like the book moving normally.

### The 12:59 card saw those prices and refused anyway

The 2026-08-22 runtime document records five games whose market-only arithmetic
clears the 5-point floor, and its own summary says "no read clears the
conservative card gate." That is the mirror image of the 2026-08-30 observation
already recorded in `docs/model-evaluation.md`, where the gate discarded the
slate's handicap and set our probability equal to DraftKings'. Here the
market-only arithmetic clears and the slate refuses on conviction. Both point at
the same open question — the slate and the gate do not agree about what a
probability is — and resolving it either way is a behaviour change, out of scope
for a read-only replay.

## What this does not license

The replay is evidence about throughput, not permission to move a rail. Lowering
the floor to 0.03 on this window buys games priced by a capture that a second
capture of the same slate contradicts, graded 0-2 over the two bets whose
prices were side-labelled and 1-1 over the two that rest on written order.
Nothing here
supports a gate change, and the recorded-handicap population — the one that
would actually test our model — does not exist yet. It starts existing with the
first `game_reads` slate.
