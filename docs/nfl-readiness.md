# NFL scanner readiness evidence

`python3 scripts/nfl_stage2_scan.py --season 2026 --week 1` remains a
read-only context collector. Each row now includes `blockers`, `assessment:
"PASS"`, `official_pick_allowed: false`, and `candidates: []`. These are the
collector's readiness result, not a replacement for a separately completed
NFL handicap. It cannot authorize a pick, infer an exchange price, or execute.

## Player evidence

ESPN's season-specific team roster supplies athlete IDs, positions, team IDs,
roster status and injury details. The collector retains every reported injury;
it no longer silently truncates to ten players. `away_injuries`/`home_injuries`
remain lists for compatibility; the matching `*_injury_evidence` objects expose
collection status, source URL and retrieval time. Injury `source_timestamp`
is the provider's injury date; retrieval time is not injury publication time.
An empty injury list does not establish official clearance.

Each `*_qb` object contains roster, depth chart, event-summary confirmation
availability and an explicit official-inactives collection state. Depth rank
and roster `Active` status **never confirm a game-day starter**. The roster
join records athlete-ID/position agreement; mismatches remain visible.
ESPN's pregame summary currently lacks official starting-QB confirmation.
After a successful summary request this is `unavailable`, not evidence that
no quarterback is available to play. Official inactives are `not_retrieved`:
this scanner has no official inactives feed. Practice reports and official
team confirmation must still be obtained by the analysis workflow.

Status meanings:

- `retrieved`: the expected collection was returned; not a passed football gate.
- `unavailable`: valid upstream response lacks required evidence.
- `collector_failure`: transport, schema, identity or season check failed.
- `not_retrieved`: this collector intentionally has not acquired that input.
- `not_applicable`: ESPN explicitly marks the venue indoor.

Diagnostics classify these as `upstream_missing`, `collector_failure`, or
`hard_gate`. Full handicap/lock-gate review always remains required. Missing
exchange context is explicitly `not_retrieved`, with null `ask` and `net_edge`.

## Stadium weather

The collector searches Nominatim using the event venue's exact name, city,
state and country. It accepts one exact name/city match, retains coordinates,
source and OpenStreetMap attribution, and refuses missing/ambiguous matches.
There is no silent city-coordinate fallback. Requests are serial, rate limited
and cached within a scan; run this as bounded slate collection, not a bulk or
high-frequency geocoder. Provider renames and address differences can still
require manual venue verification. No static team-to-stadium assumption is
made, so neutral-site games use their actual listed venue.

Open-Meteo is queried for five UTC hourly samples: the hour containing kickoff
through four hours later. The row records temperature in Fahrenheit, sustained
wind/gusts in mph, precipitation and snow in inches. Missing hours, non-finite
values and unexpected units/timezones cannot become retrieved weather.
`retrieved_at` is the forecast acquisition time. `forecast_issued_at` remains
null because this endpoint does not supply model issuance time. A forecast
must be refreshed at analysis/lock time; retained rows are not a freshness gate.
An indoor flag is retained as the source's claim; retractable-roof status still
needs a game-day check. Retrieved weather requires thesis review, including
sustained wind at least 15 mph; it never clears that gate automatically.

## Early-season form

Raw W/L/PF/PA/PD fields remain descriptive historical totals. Regular-season
Weeks 1–4 with fewer than three current-season games backfill from the prior
season. Known preseason games, wrong-season records, and future games are
excluded. Counts include only games with usable scores.

Prior games receive a **0.5 evidence weight**, current games 1.0, in the
`weighted_*` totals and `effective_n`. This is a conservative implementation
choice for the reference's “discount hard” rule, not a fitted model or an
estimated win probability. `discounted_pd_per_game` divides weighted PD by raw
valid game count: dividing by effective sample size would cancel the discount
in a prior-only Week 1 sample. Analysis must use the discounted contribution,
not raw historical PD as current form. The form label explicitly flags the
fallback; offseason roster/coaching adjustment remains a blocker and Week 1
confidence is capped at Medium. No fallback runs in Week 5+, preseason, or
postseason. These summaries do not enforce the downstream analyst's reasoning.

## Verification and provider references

Run the complete repository suite with `python -m pytest -q` after installing
pytest and both `scripts/requirements-shadow.txt` and
`skills/sports-picks/scripts/requirements-exec.txt`. `tests/test_nfl_readiness.py`
uses offline boundary fixtures; read-only provider smoke checks are separate.

- [ESPN roster](https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/17/roster?season=2026)
- [ESPN depth chart](https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/17/depthcharts?season=2026)
- [ESPN game summary](https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event=401872656)
- [Nominatim search API](https://nominatim.org/release-docs/latest/api/Search/)
- [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/)
- [Open-Meteo forecast API](https://open-meteo.com/en/docs)
