# MLB drought diagnostic — 2026-08-11 to 2026-08-31 (21 days)

Executed picks in window: **0**. Priced candidates: 2. Watchlist near-misses: 12.

## Days by class

| class | days |
| --- | --- |
| `no_slate_artifact` | 5 |
| `slate_empty` | 10 |
| `watchlist_only` | 5 |
| `candidates_rejected` | 1 |
| `candidates_executed` | 0 |

## Where priced candidates stopped

| stop | count |
| --- | --- |
| `executed` | 0 |
| `review_gate_rejected` | 2 |
| `approved_not_executed` | 0 |
| `unknown` | 0 |

## Day by day

| date | class | games | cands | watch |
| --- | --- | --- | --- | --- |
| 2026-08-11 | `watchlist_only` | 15 | 0 | 2 |
| 2026-08-12 | `no_slate_artifact` | — | 0 | 0 |
| 2026-08-13 | `no_slate_artifact` | — | 0 | 0 |
| 2026-08-14 | `no_slate_artifact` | — | 0 | 0 |
| 2026-08-15 | `slate_empty` | 15 | 0 | 0 |
| 2026-08-16 | `watchlist_only` | 15 | 0 | 3 |
| 2026-08-17 | `slate_empty` | 11 | 0 | 0 |
| 2026-08-18 | `watchlist_only` | 15 | 0 | 2 |
| 2026-08-19 | `no_slate_artifact` | — | 0 | 0 |
| 2026-08-20 | `no_slate_artifact` | — | 0 | 0 |
| 2026-08-21 | `watchlist_only` | 15 | 0 | 1 |
| 2026-08-22 | `slate_empty` | 15 | 0 | 0 |
| 2026-08-23 | `slate_empty` | 15 | 0 | 0 |
| 2026-08-24 | `watchlist_only` | 10 | 0 | 1 |
| 2026-08-25 | `slate_empty` | 15 | 0 | 0 |
| 2026-08-26 | `slate_empty` | 15 | 0 | 0 |
| 2026-08-27 | `slate_empty` | 7 | 0 | 0 |
| 2026-08-28 | `slate_empty` | 15 | 0 | 0 |
| 2026-08-29 | `slate_empty` | 17 | 0 | 0 |
| 2026-08-30 | `candidates_rejected` | 14 | 2 | 3 |
| 2026-08-31 | `slate_empty` | — | 0 | 0 |

## Data gaps
- **no_cached_mlb_schedule** — no denominator for these days: the scan's output cannot be read against the slate that was actually available. ['2026-08-12', '2026-08-13', '2026-08-14', '2026-08-19', '2026-08-20', '2026-08-31']
- **no_slate_artifact** — no schedule JSON and no writeup. The corpus cannot distinguish 'the job did not run' from 'the job ran and wrote nothing'; that needs cron/journal state, which is outside this report's inputs. ['2026-08-12', '2026-08-13', '2026-08-14', '2026-08-19', '2026-08-20']
- **invalid_watchlist_status** — status is outside the validator's accepted set, so no transition can act on the entry again. [{'date': '2026-08-11', 'id': 'LW-20260811-MIL-SD', 'status': 'recheck_complete'}, {'date': '2026-08-11', 'id': 'LW-20260811-HOU-SF', 'status': 'recheck_complete'}]

Reconciliation: ok
