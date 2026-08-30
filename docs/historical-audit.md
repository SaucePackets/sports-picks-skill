# Historical MLB pick audit

`scripts/vig_historical_audit.py` reads every dated
`.picks/execute/<date>-schedule.json`, reconciles each candidate to the official
MLB final, and reports what the slate proposed, what happened on the field, and
— separately — how much of the current selection contract was present to judge
it by.

It is read-only. It writes nothing except the official-results cache, and only
under `--fetch`. It has no order, execution, or policy-mutation path.

## Running it

```sh
# Offline: reports against whatever official results are already cached.
python scripts/vig_historical_audit.py --picks-dir ~/projects/sports-picks-runtime/.picks

# Populate the results cache first (MLB Stats API, via scripts/http_util.py),
# then report. One request per dated schedule; already-cached dates are skipped.
python scripts/vig_historical_audit.py \
  --picks-dir ~/projects/sports-picks-runtime/.picks \
  --results-dir ~/mlb-audit-results --fetch

# Machine-readable, including every per-candidate record.
python scripts/vig_historical_audit.py --picks-dir ... --json
```

`--picks-dir` defaults to `$SPORTS_PICKS_ROOT/.picks`; `--results-dir` defaults
to `<picks-dir>/audit-results`. `--since` / `--until` bound the date range,
`--edge-floor` overrides the floor for sensitivity analysis, and `--min-sample`
sets the bucket size below which no calibration or ROI claim is made.

## Where the numbers come from

| Quantity | Source | Never |
|---|---|---|
| Official winner and score | `mlb_final_scores.final_scores` over a cached MLB Stats API schedule payload | web search, an LLM, a scraped box score |
| Edge floor | `mlb_runtime_policy.DEFAULT_MIN_CONSERVATIVE_EDGE`, the repo's declared default | the live `risk_limits.json` — today's rail is not the rail that was in force in May |
| Conservative edge | `conservative_probability - current_ask` from the card | a stored `net_edge`, a reconstruction from any other pair of fields |
| Wilson intervals | `vig_calibration_report.wilson_ci` | a second copy of the same arithmetic |

Every reconciled candidate carries its provenance: the cache path, the Stats API
URL, the fetch timestamp, the `gamePk`, how the game was matched, and how the
side was resolved.

## The distinctions the report exists to preserve

**Unevaluable is not below-floor.** The 5-point floor is
`conservative_probability - current_ask`. Almost no historical card carries
either field — they predate the contract in
`mlb_runtime_policy.REQUIRED_EXECUTION_FIELDS`. Those candidates are reported
`unevaluable`, with the missing fields named. Reporting them as `below_floor`
would manufacture a rejection that never happened; reporting them as `cleared`
would manufacture an approval. A stated-probability edge is computed alongside
where possible and labelled **advisory** — it can never become the verdict,
because a model number that never passed through the uncertainty haircut is not
the quantity the floor was set against.

**A no-pick day is a control, not an 0-for-0 day.** A day whose schedule
proposed no candidate is counted, its date is listed, and it enters no accuracy
denominator anywhere. A day that proposed candidates and bet none of them is
*not* a control — collapsing the two would hide every day the gate refused a
play. An unreadable or malformed file is not a control either, however empty it
looks.

**Side correctness is not bet quality.** A pick can be right about the baseball
and unjudgeable as a wager (no price on the card), or wrong about the baseball
while having been well-formed. The report keeps `side_correctness` and
`process` separate and states the population of each.

**Populations are always named.** Side correctness covers every candidate
reconciled to an official Final. Economics covers only candidates marked
executed, and ROI divides by the stake of the candidates that carry a P&L — a
strict subset again, because most settled outcomes live in the ledger rather
than on the card. Two reports over one source that do not name their
populations are indistinguishable from one report being stale.

**Insufficient samples say so.** Calibration buckets and the headline ROI both
carry a `sufficient` flag against `--min-sample`, and small buckets are shown
marked rather than hidden — hiding them would misrepresent coverage.

## What it refuses to guess

- **Prose prices.** `"DK/ESPN CLE -131; Polymarket ask 0.56"` contains two
  numbers meaning different things. Neither is taken; the raw string is kept and
  the price is reported `prose_price_unparsed`. A scraped integer here would
  land silently in a calibration bucket.
- **Doubleheaders.** Two official rows for one team pair, and no `game_pk` on
  the card, is genuinely ambiguous. It fails closed as
  `ambiguous_doubleheader`. With a `game_pk` it resolves exactly.
- **Games that had not finished.** Reported `not_final: <status>`, distinct from
  `no_official_game`. Both stay unreconciled; only one is a defect in the card.
- **Sides naming a team that did not play.** Reported unresolved rather than
  credited to the opponent.

## Schema variants handled

| Variant | Example | What is different |
|---|---|---|
| `current` | `2026-08-30` | object with `sport` + `market_type`; `win_probability`, `dk_fair_prob`, `net_edge`, `polymarket_ask` |
| `legacy_object` | `2026-07-12` | object with `status`/`daily_cap`; fill and settlement fields, no probability contract |
| `legacy_object` | `2026-05-26` | `side` is an abbreviation with `pick_side` carrying the full name; American-odds price only |
| `legacy_object` | `2026-05-30` | prose `price` string alongside `polymarket_price` |
| `legacy_bare_list` | `2026-07-17` | top level is `[]`, not an object |

Side and matchup forms all normalize: full names, bare nicknames
(`"Yankees at Tigers"`), abbreviations, the `"BOS @ LAA"` separator, and the
`"Detroit Tigers ML"` market suffix.

## Known limit of the abbreviation table

Resolution tries full names and nicknames across every side field first, and
only then consults `TEAM_ABBREVIATIONS`. A resolved name is accepted only if it
is one of the two teams the official row names, so a table entry that is wrong
in the ordinary way — stale after a rename, a typo'd city — resolves to a team
that is not playing and the candidate is reported unresolved.

What that cross-check **cannot** catch is an entry that maps an abbreviation to
the *opponent* in some game: that name is in the row, so it resolves, and it
resolves wrongly. Nothing available offline distinguishes it.
`test_the_cross_check_cannot_catch_a_table_entry_that_names_the_opponent`
demonstrates the gap rather than claiming it away. The report prints how many
sides were resolved by abbreviation versus by name, so the exposure is visible
rather than assumed to be zero.
