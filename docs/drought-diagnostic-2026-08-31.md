# MLB drought diagnostic — 2026-08-11 to 2026-08-31 (21 days)

Executed picks in window: **0**. Priced candidates: 2. Watchlist near-misses: 12.

## Days by class

| class | days |
| --- | --- |
| `job_never_fired` | 1 |
| `scan_ran_artifact_unwritten` | 3 |
| `no_slate_artifact` | 0 |
| `slate_empty` | 11 |
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

| date | class | games | cands | watch | files in `sports-picks-runtime` | files in `sports-picks-skill` |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-08-11 | `watchlist_only` | 15 | 0 | 2 | `execute/2026-08-11-schedule.json`, `slate/2026-08-11.md`, `audit-results/2026-08-11.json` | `execute/2026-08-11-schedule.json`, `slate/2026-08-11.md` |
| 2026-08-12 | `scan_ran_artifact_unwritten` | — | 0 | 0 | — | — |
| 2026-08-13 | `scan_ran_artifact_unwritten` | — | 0 | 0 | — | — |
| 2026-08-14 | `scan_ran_artifact_unwritten` | — | 0 | 0 | — | — |
| 2026-08-15 | `slate_empty` | 15 | 0 | 0 | `execute/2026-08-15-schedule.json`, `slate/2026-08-15.md`, `audit-results/2026-08-15.json` | `execute/2026-08-15-schedule.json`, `slate/2026-08-15.md` |
| 2026-08-16 | `watchlist_only` | 15 | 0 | 3 | `execute/2026-08-16-schedule.json`, `slate/2026-08-16.md`, `audit-results/2026-08-16.json` | `execute/2026-08-16-schedule.json`, `slate/2026-08-16.md` |
| 2026-08-17 | `slate_empty` | 11 | 0 | 0 | `execute/2026-08-17-schedule.json`, `slate/2026-08-17.md`, `audit-results/2026-08-17.json` | `execute/2026-08-17-schedule.json`, `slate/2026-08-17.md` |
| 2026-08-18 | `watchlist_only` | 15 | 0 | 2 | `execute/2026-08-18-schedule.json`, `slate/2026-08-18.md`, `audit-results/2026-08-18.json` | `execute/2026-08-18-schedule.json`, `slate/2026-08-18.md` |
| 2026-08-19 | `job_never_fired` | — | 0 | 0 | — | — |
| 2026-08-20 | `slate_empty` | — | 0 | 0 | — | `execute/2026-08-20-schedule.json`, `slate/2026-08-20.md` |
| 2026-08-21 | `watchlist_only` | 15 | 0 | 1 | `execute/2026-08-21-schedule.json`, `slate/2026-08-21.md`, `audit-results/2026-08-21.json` | — |
| 2026-08-22 | `slate_empty` | 15 | 0 | 0 | `execute/2026-08-22-schedule.json`, `slate/2026-08-22.md`, `audit-results/2026-08-22.json` | `execute/2026-08-22-schedule.json`, `slate/2026-08-22.md` |
| 2026-08-23 | `slate_empty` | 15 | 0 | 0 | `execute/2026-08-23-schedule.json`, `slate/2026-08-23-evening.md`, `slate/2026-08-23.md`, `audit-results/2026-08-23.json` | — |
| 2026-08-24 | `watchlist_only` | 10 | 0 | 1 | `execute/2026-08-24-schedule.json`, `slate/2026-08-24.md`, `audit-results/2026-08-24.json` | — |
| 2026-08-25 | `slate_empty` | 15 | 0 | 0 | `execute/2026-08-25-schedule.json`, `slate/2026-08-25-evening.md`, `slate/2026-08-25.md`, `audit-results/2026-08-25.json` | — |
| 2026-08-26 | `slate_empty` | 15 | 0 | 0 | `execute/2026-08-26-schedule.json`, `slate/2026-08-26-evening.md`, `slate/2026-08-26.md`, `audit-results/2026-08-26.json` | — |
| 2026-08-27 | `slate_empty` | 7 | 0 | 0 | `execute/2026-08-27-schedule.json`, `slate/2026-08-27-evening.md`, `slate/2026-08-27.md`, `audit-results/2026-08-27.json` | — |
| 2026-08-28 | `slate_empty` | 15 | 0 | 0 | `execute/2026-08-28-schedule.json`, `slate/2026-08-28-evening.md`, `slate/2026-08-28.md`, `audit-results/2026-08-28.json` | — |
| 2026-08-29 | `slate_empty` | 17 | 0 | 0 | `execute/2026-08-29-schedule.json`, `slate/2026-08-29-evening.md`, `slate/2026-08-29.md`, `audit-results/2026-08-29.json` | — |
| 2026-08-30 | `candidates_rejected` | 14 | 2 | 3 | `execute/2026-08-30-schedule.json`, `slate/2026-08-30-evening.md`, `slate/2026-08-30.md`, `audit-results/2026-08-30.json` | — |
| 2026-08-31 | `slate_empty` | — | 0 | 0 | `execute/2026-08-31-schedule.json`, `slate/2026-08-31.md` | — |

## Run evidence — days the corpus cannot explain by itself

Quoted verbatim because the sources behind them rotate and cannot be re-derived later. Consulted only for a date with no file in any root.

### 2026-08-12 — `scan_ran_artifact_unwritten`

The job fired on schedule, ran for nine minutes, exhausted its tool-iteration budget, and delivered its analysis 0.19s after the turn ended. The delivered report says in its own words that it never reached the write. Nothing about the market or the slate is implicated.
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-12 15:30:57,475 INFO cron.scheduler: Running job 'Vig — MLB Daily Slate (10:30am CT)' (ID: c9452052719c)`
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-12 15:39:55,705 INFO [cron_c9452052719c_20260812_103057] agent.conversation_loop: Turn ended: reason=max_iterations_reached(30/30) model=deepseek-v4-flash api_calls=30/30 budget=30/30 tool_turns=30 last_msg_role=assistant response_len=8365 session=cron_c9452052719c_20260812_103057`
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-12 15:39:55,895 INFO cron.scheduler: Job 'c9452052719c': delivered to buzz:72db9b5f-3e70-4add-978d-5caaeab48a82 via live adapter`
- `buzz message 7b589043d76dd10c, delivered 2026-08-12T15:39:55Z` — `One honesty flag: this cron hit its tool-iteration cap before I could persist the schedule JSON / slate file to `.picks/`, so this report is the record; nothing was executed and nothing is pending execution, so the operational risk from the missing artifacts is nil`

### 2026-08-13 — `scan_ran_artifact_unwritten`

Same shape as 08-12: fired on schedule, hit the 30/30 iteration cap, delivered 0.4s later, and the delivered report names the unwritten files and the skipped validator explicitly.
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-13 15:30:17,991 INFO cron.scheduler: Running job 'Vig — MLB Daily Slate (10:30am CT)' (ID: c9452052719c)`
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-13 15:44:06,551 INFO [20260813_153938_fc3ecb] agent.conversation_loop: Turn ended: reason=max_iterations_reached(30/30) model=deepseek-v4-pro api_calls=30/30 budget=30/30 tool_turns=13 last_msg_role=assistant response_len=3117 session=20260813_153938_fc3ecb`
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-13 15:44:06,958 INFO cron.scheduler: Job 'c9452052719c': delivered to buzz:72db9b5f-3e70-4add-978d-5caaeab48a82 via live adapter`
- `buzz message d08a6a73f89fe2b9, delivered 2026-08-13T15:44:06Z` — `the full slate scan and per-game analysis are complete, but the tool loop hit its iteration cap before the `schedule.json` write and `mlb_lineup_watchlist.py --validate` step could run. Files for today's slate are **not** written to disk; validator was not run.`

### 2026-08-14 — `scan_ran_artifact_unwritten`

Third instance of the same failure on three consecutive days. Fired, capped at 30/30, delivered 0.39s later, and the report names both artifact paths it could not persist.
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-14 15:30:41,308 INFO cron.scheduler: Running job 'Vig — MLB Daily Slate (10:30am CT)' (ID: c9452052719c)`
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-14 15:46:50,129 INFO [20260814_153938_b42762] agent.conversation_loop: Turn ended: reason=max_iterations_reached(30/30) model=deepseek-v4-pro api_calls=30/30 budget=30/30 tool_turns=15 last_msg_role=assistant response_len=6431 session=20260814_153938_b42762`
- `~/.hermes/profiles/vig/logs/agent.log.3` — `2026-08-14 15:46:50,519 INFO cron.scheduler: Job 'c9452052719c': delivered to buzz:72db9b5f-3e70-4add-978d-5caaeab48a82 via live adapter`
- `buzz message 0c724662dce8ae3c, delivered 2026-08-14T15:46:50Z` — `this run reached its execution budget before I could persist `.picks/slate/2026-08-14.md`, `.picks/execute/2026-08-14-schedule.json`, and run the `mlb_lineup_watchlist.py --validate` step. The read below is complete and honest, but the slate artifacts are **NOT saved and NOT validated this cycle**`

### 2026-08-19 — `job_never_fired`

An ABSENCE argument, so it is stated with its denominator. The claim is that no vig cron job of any kind started on 2026-08-19. Three things make that absence meaningful rather than a gap in the record: the log is continuous across the day (the rotation boundaries are 08-12 17:05 and 08-20 02:16, both outside it); the logger was demonstrably writing throughout, at 1727 mem_trim heartbeat lines — MORE than 08-18's 1698 and 08-20's 1726; and the neighbouring dates both show the firing signature, 2 running_job lines each. An independent source outside the host agrees: the delivery channel has zero messages on 08-19 and at least one on every other date in the window. WHY it did not fire is NOT answerable from any of these sources and is left open.
- `~/.hermes/profiles/vig/logs/agent.log.2` — `grep -c "^2026-08-19 .*Running job" over agent.log{,.1,.2,.3} => 0; the same grep for 2026-08-18 => 2 and for 2026-08-20 => 2`
- `~/.hermes/profiles/vig/logs/agent.log.2` — `grep -c "^2026-08-19 .*mem_trim" over agent.log{,.1,.2,.3} => 1727 (2026-08-18 => 1698, 2026-08-20 => 1726): the profile logger wrote continuously through the day with no job line in it`
- `~/.hermes/profiles/vig/logs/agent.log.2` — `2026-08-20 02:16:55,517 INFO cron.scheduler: Job 'Vig — MLB Standing-Authorized Execution Poller': script produced no output, skipping AI`
- `buzz channel 72db9b5f-3e70-4add-978d-5caaeab48a82` — `150 messages 2026-08-11..2026-08-31; per-date counts show 2026-08-19 = 0 and every other date >= 1. Independent of the VPS host.`

## Artifact receipts

Fingerprints for the files a reader would otherwise have to take on description. Size, mtime and hash make the claim checkable.

| date | root | file | size | mtime (UTC) | sha256 |
| --- | --- | --- | --- | --- | --- |
| 2026-08-20 | `sports-picks-skill` | `.picks/slate/2026-08-20.md` | 9017 | 2026-08-20T15:36:33Z | `8ce32a91d1773475500695b129fa9ea9bfbe569c5dad0c17ef953be91f74be9d` |
| 2026-08-20 | `sports-picks-skill` | `.picks/execute/2026-08-20-schedule.json` | 121 | 2026-08-20T15:36:33Z | `1e005ab696d25e5df5e60a22eef4a2ffe9c1f29f9d3b019ddaac7a2eb5ac06ea` |

## Roots searched

| label | role | path | dates with files |
| --- | --- | --- | --- |
| `sports-picks-runtime` | primary | `~/.buzz/.scratch/claude-drought-roots/sports-picks-runtime/.picks` | 16 |
| `sports-picks-skill` | secondary | `~/.buzz/.scratch/claude-drought-roots/sports-picks-skill/.picks` | 7 |

**Dates the primary root `sports-picks-runtime` does not have and another root does: 1.** These are the days a primary-only enumeration reports as having no artifact when the file exists.

- **2026-08-20** — in `sports-picks-skill`: `execute/2026-08-20-schedule.json`, `slate/2026-08-20.md`

For completeness, all 11 dates present in exactly one root. The rest of these are the secondary checkout simply no longer being written to, and are not defects:

- 2026-08-20 — only in `sports-picks-skill`, absent from `sports-picks-runtime`
- 2026-08-21 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-23 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-24 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-25 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-26 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-27 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-28 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-29 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-30 — only in `sports-picks-runtime`, absent from `sports-picks-skill`
- 2026-08-31 — only in `sports-picks-runtime`, absent from `sports-picks-skill`

## Finding — `namespace_silence`

A lookup against the wrong namespace returns silence, and silence reads as absence. It never raises and never logs, so the wrong answer arrives looking like a finding.

- **event_id joined against gamePk** — the slate's event_id is a different id space from the MLB gamePk — 2026-08-30 records event_id 401816733 for the game the schedule calls gamePk 824876. Joining outcomes on the id matches nothing and reads as 'no outcome data'. _Mitigation:_ the outcome join is keyed on the matchup, never on an id.
- **corpus enumerated from one .picks root** — the daily slate wrote into more than one checkout across this window, so a report built from the primary root alone reported an existing artifact as absent. _Mitigation:_ every date is enumerated across every known root.

## Data gaps
- **no_cached_mlb_schedule** — no denominator for these days: the scan's output cannot be read against the slate that was actually available. ['2026-08-12', '2026-08-13', '2026-08-14', '2026-08-19', '2026-08-20', '2026-08-31']
- **run_evidence_open_question** — Why did no vig cron job fire on 2026-08-19? The profile log shows the scheduler silent for the whole day while hermes itself kept logging, and none of the sources above records a reason.. ['2026-08-19']
- **run_evidence_open_question** — Why did the 2026-08-20 slate run write into the sports-picks-skill checkout when the scheduler recorded `using workdir /home/<account>/projects/sports-picks-runtime`? The run's own workdir line and the artifact's location disagree. See controls.workdir_lines for the quoted lines.. ['2026-08-20']
- **invalid_watchlist_status** — status is outside the validator's accepted set, so no transition can act on the entry again. [{'date': '2026-08-11', 'id': 'LW-20260811-MIL-SD', 'status': 'recheck_complete'}, {'date': '2026-08-11', 'id': 'LW-20260811-HOU-SF', 'status': 'recheck_complete'}]

Reconciliation: ok
