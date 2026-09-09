# NFL assessment and final-card contract

The scanner collects context. The finalizer accepts a structured handicap,
checks it against context and supplemental evidence, and builds the report and
manual proposed schedule together. It does not estimate win probabilities,
authenticate provider responses, decide subjective football questions, or place
bets. Those evidence/trust limits apply to a successful verification receipt too.

## Supported path

Use `scripts/nfl_weekly_prompt.txt` as the reviewed prompt template. The live
cron must explicitly be installed with that template in a separate deployment;
this repository change does not activate it. Downstream readers must use the
run bundle and invoke `--verify`; legacy dated artifacts are not valid evidence
of this new contract. A cron process exiting normally does not prove completion.

1. Capture the full scanner JSON into a run-specific staging file. Rows carry
   `context_version: 1`, `collector_only: true`, `collected_at`, team IDs and
   `analysis_tasks`. They have no `assessment`, `candidates`, or
   `official_pick_allowed` decision fields. Legacy scanner output is refused.
2. Generate an incomplete draft from every scanned event:
   `python3 scripts/nfl_card_writer.py --root ROOT --scan SCAN --skeleton --run-id RUN --date YYYY-MM-DD`
3. Complete the draft using the schema below, then run `--diagnose` with
   `--root ROOT --scan SCAN --draft DRAFT`. Incomplete analysis exits 2 and
   explicitly withholds the completed card. No final files are written.
4. Run the same inputs with `--write`, then independently run
   `python3 scripts/nfl_card_writer.py --root ROOT --verify --run-id RUN`.
   Both must succeed before calling the slate complete. Read the human report
   from that exact bundle, not a remembered checkout or dated artifact.

The only canonical output is `ROOT/.picks/nfl/runs/RUN/`, an immutable bundle
containing `scan.json`, `assessment.json`, `schedule.json`, `slate.md`, and
`receipt.json`. All five are staged and the directory is renamed into place;
existing runs are never overwritten. New evidence needs a new run ID. This
avoids partial multi-file replacement and preserves the card that was reviewed.
No legacy schedule, latest-action file, runtime config or executor is changed.

Verification checks the explicit root/run, file content, event coverage, then
recomputes decisions and report from the stored inputs. A hash alone does not
prove validity. Missing finalizer output, tampered schedule/report, old receipt,
started game or stale evidence fails. Copying a bundle to a different root does
not validate it. A prompt can still be bypassed; only a consumer that actually
runs this verifier has the acceptance boundary.

## Draft schema

The envelope has exactly `version: 1`, `run_id`, current Central `date`, and
`assessments`. Run IDs are 1–80 ASCII letters/digits/underscores/hyphens.
One assessment per scan event is required for a completed card. Unknown or
duplicate events are rejected; missing events remain incomplete.

Each assessment contains:

- `event_id`, `side` (`away` or `home`), `confidence` (`Medium` or `High`),
  positive finite `unit_size`, and independently justified `win_probability`.
- `thesis` and `market_explanation` (required when own probability differs
  from two-sided de-vig fair by more than 0.04).
- `sections`: nonempty analyst reasoning for `form`, `qb`, `trenches_defense`,
  `spot`, `market`, and `the_question`.
- `gates`: every name in `nfl_card_writer.GATES`, each with `status`
  (`pass`, `fail`, `unknown`), `reason`, and `evidence_refs`. Here pass always
  means the condition is cleared, including inverse-named danger gates.
- `evidence`: supplemental records described below.

An unknown gate, missing section, unattempted required acquisition, partial
collector row or missing game assessment makes the whole card incomplete.
Fully evaluated hard failures can produce a complete PASS, with their reasons.
Neither a missing source nor `offseason_adjustment_required` should be copied
blindly into an analyst failure: perform the inquiry and record what happened.
The finalizer cannot judge whether subjective prose represents good analysis;
independent review is still needed.

Output decisions and execution/reviewer fields are forbidden in the draft.
`status: awaiting_jerry`, `execution_mode: manual`, `executed: false`, null
manual-bet/review decisions, and review-needed flags are generated only for
validated proposed candidates. This is no execution or staking authorization.

## Evidence records

Each record has a unique `id`, `kind`, matching `event_id`, `kickoff_utc`, and
one or both matching `team_ids`; an HTTPS `source`; actual `observed_at`;
`summary`; `status` (`available` or `unavailable`); and a `data` object.
Unavailable means an attempted source returned no usable evidence and requires
`reason`. Unattempted work is not an unavailable record: leave its gate unknown.
Do not recast a transport failure as proof of upstream absence.

Kinds and minimum data for clearing objective requirements:

| Kind | Required use/data |
|---|---|
| `qb_confirmation` | Selected team only; `source_type` official_team/official_league; athlete_id, position QB, confirmed_start true, status_resolved true. Athlete must match the selected team's retrieved roster. Depth rank or Active alone is insufficient. |
| `player_availability` | One official_team/official_league record per team, relevant_status_resolved true, with report/source reasoning. Official inactives need not be published if the existing gate can already be satisfied from accepted official evidence. |
| `efficiency` | Both teams identified; cite the actual efficiency read supporting defense/late-game judgment. |
| `offseason_review` | Each team using prior-season form must have reviewed coaching/QB/roster changes supporting the early-season judgment. |
| `sportsbook` | Both teams; signed integer away_price/home_price. These refreshed prices, never scanner snapshots, drive report and de-vig math. |
| `exchange` | Both teams; moneyline market_type, exact contract_id, selected side/team_id and ask strictly between 0 and 1. Missing ask stays null; no edge exists without it. |
| `analysis` | Additional sourced analytical support; cannot substitute for the typed evidence above. |

Gate references can point to supplemental IDs or available `scan.*` fields:
away_form/home_form, away_qb/home_qb, away_injury_evidence/home_injury_evidence,
weather, venue, away_rest_days/home_rest_days. References must exist in the same
game. A reference is not, by itself, corroboration; specific gates also check
kind, team, fields and acquisition state. Analyst-provided official-source labels
and content are **not cryptographically authenticated or refetched by this
writer**. Review the linked source before approving a real card.

Freshness checks are conservative acceptance windows for proposed-card input:
6 hours for collector context, weather and nonprice evidence; 15 minutes for
sportsbook/exchange evidence and verification receipts; no future timestamps.
They are not a lock-time freshness guarantee. A later approval requires refreshed
evidence and another verification. Weather checks bind venue ID, kickoff, five
UTC hours, coordinates, units and finite values; weather thesis judgment remains
with the analyst. Rendered collection facts come directly from the scan. Analyst
section text is not semantically checked for every possible contradiction.

## Selection rules

Every existing football gate must pass for a candidate; own probability must
clear matched exchange ask by at least 0.02. The writer calculates this and
checks the market-disagreement explanation. Preseason never yields candidates
or watchlist entries. Week 1 confidence is capped at Medium. Valid discounted
prior-season form is usable, with completed offseason review; it is not a
permanent failure. More than three qualified proposed candidates is refused
rather than silently truncated: the analyst must narrow the card responsibly.

A single failed qb_status_gate with `reason_code: qb_status_unconfirmed`, or
injury_cluster_gate with `reason_code: inactives_unconfirmed`, can enter the
watchlist only when every other gate passes and attempted official evidence
records that unresolved status. A second failure or any unknown excludes it.
Watchlist prices are signed integers; the conservative bettable-to price equals
the reviewed original price; recheck is kickoff minus 75 minutes. Watchlist
entries are not awaiting-Jerry candidates and never enter an execution path.

## Tests

`tests/test_nfl_card_writer.py` supplies synthetic evidence only. It proves
positive reachability, every gate failure/unknown, missing assessments, legacy
zero-card refusal, team/athlete/source consistency, Week 1, price freshness/math,
weather consistency, watchlist conditions, and bundle/readback failures. No
synthetic test is evidence of a current pick. Run the full repository suite in
addition to focused tests; baseline failures remain separately reported.
