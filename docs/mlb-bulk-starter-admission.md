# Offline bulk starter and pitcher-history admission

`mlb_bulk_starter_admission.py` extends retained starter replay to every occurrence
in the unchanged [chronological contract](mlb-chronological-contract.json). It
constructs only the predeclared home-minus-away strikeout and walk fractions.
It performs no acquisition, fitting, scoring, odds lookup, policy change, runtime
write, or deployment. It never admits a challenger or asserts original pregame
predictions. The new script is not included in the runtime installer.

The supplied evidence admits **zero feature pairs**. This is an implemented
admission path with explicit data refusals, not a recovered training dataset.
The [checkpoint](mlb-bulk-starter-admission.json) binds the exact report and inputs.

## Gates and limits

Every daily schedule, including empty dates and non-regular games, contributes to
the census. Unknown daily coverage gives a null denominator and blocks admission.
Cross-date duplicate IDs and moved/resumed targets are refused. The existing
bundle loader checks every manifest object, including unused/error responses,
before parsing. The snapshot loader checks every `timestamps-*.json` and
`snapshot-*.json` receipt and body, even when unused. Hash/size/path integrity
failures abort replay; source/schema gaps remain explicit refusals. Exploratory
`precutoff-*` diagnostics and unreferenced objects are not inputs. Receipt hashes
are recorded in a deterministic list and bound by its aggregate hash; compare
the report to the committed checkpoint to detect receipt removal or replacement.
Hashes establish retained integrity, not independent provider authenticity.

A starter must come from the latest indexed provider snapshot strictly before
scheduled start minus 60 minutes. Training rows additionally cap that cutoff at
the fixed training completion boundary. Returned metadata, game IDs, numeric team
IDs, official/original date, scheduled start, regular-season type, season and
Preview state must corroborate. Only `gameData.probablePitchers` supplies starter
identity; target final feeds and boxscore starters never substitute. Snapshot
metadata remains independently unverified historical availability evidence.

Prior appearances require the existing schedule/final/completion checks, plus
boxscore team/person IDs, unique pitcher lists, completed sequential plate
appearances, top/bottom defensive side, and per-game batters-faced, strikeout and
walk totals. Every counted play retains a source pointer. Unknown/non-PA events,
zero-completed-PA pitching appearances, before-scheduled-start plays, and count
conflicts refuse the entire game's appearance set. These are conservative parser
limits, not claims that those games lacked legitimate pitching appearances.
This adapter corroborates within retained MLB feeds/boxscores; it does not claim
independent Savant validation or use present-day season totals.

Histories select each pitcher's last three regular-season appearances in the
fixed window, ordered by scheduled start and numeric game ID. Official dates must
be strictly earlier than the target date. Each selected game's completion must
be strictly before the observation cutoff: a too-late recent appearance refuses
the history rather than being skipped for an older one. The denominator is the
sum of completed plate appearances across three games; strikeouts include
strikeout double plays and walks include intentional walks. Fractions pool counts
rather than averaging three game percentages. Historical team membership is
corroborated per appearance; transfers need not preserve the target team.

**Any unresolved earlier regular-season game blocks history for either pitcher.**
The retained source may not establish who pitched in that game, so filtering the
gap to the target team/pitcher would risk silently skipping an appearance.
This intentionally over-refuses, including gaps older than the selected three.
Non-regular games are outside this contract's history population. Missing, refused,
and fewer-than-three histories stay null; no zero, mean, or fallback is supplied.
Feature availability is separate from target outcome/label admission. Removing a
target final does not change its features; no labels or challenger rows are emitted.

## Retained checkpoint

The replay uses the existing owner-controlled evidence, without new network reads:
`RESEARCH/MLB_CHRONOLOGICAL_ADMISSION_2026_09_07/{ACQUISITION,STARTER_PROBE}`
relative to the shared Buzz workspace. It covers 122 known dates, 1,657 total
schedule occurrences, and 1,291 regular-season occurrences (1,271 unique games).
The report preserves all 366 non-regular occurrences as excluded.

| Split | Regular occurrences | Starter candidates | Feature pairs |
|---|---:|---:|---:|
| Warmup | 0 | 0 | 0 |
| Train | 467 | 1 | 0 |
| Validation | 422 | 1 | 0 |
| Test | 402 | 1 | 0 |

There are 1,243 missing retained starter-source refusals, 40 duplicate-ID
occurrences, five target identity refusals, and three pitcher-history refusals
among regular-season targets. Only the existing three snapshots are retained;
this run does not claim bulk snapshot acquisition. Of 1,291 regular appearance
game occurrences, 984 yield 8,307 corroborated pitcher appearances. Refusals:
223 unsupported/non-PA events, 40 repeated IDs, 22 before-scheduled-start plays,
seven pitching-count conflicts, five schedule mismatches, four nonfinal sources,
three play-chronology conflicts, and three zero-completed-PA appearances.
Missing bulk snapshots and unresolved appearance census are the remaining data
blockers. Fitting/scoring/eligibility stay false and performance stays null.

## Reproduce and review

Use Python 3.14.7 from the repo root:

```sh
python3 scripts/mlb_bulk_starter_admission.py \
  --bundle "$EVIDENCE/ACQUISITION" \
  --snapshot-dir "$EVIDENCE/STARTER_PROBE" > bulk.json
shasum -a 256 bulk.json
```

`EVIDENCE` refers to the chronological evidence directory above. The output goes
to stdout only; redirection is an operator action. Exit 0 means a report was
produced, not that any features were admitted. Integrity errors exit 2. The CLI
has no acquisition or scoring switch. Reviewers need owner-authorized access to
the retained bytes; GitHub alone is insufficient. No raw feeds are published.
The bulk report and test logs are retained under
`RESEARCH/MLB_BULK_STARTER_ADMISSION_2026_09_07`.

Validation uses the full package pytest suite and an exact-base comparison.
Adversarial controls cover a positive end-to-end feature row, weighted fractions,
strict game/training cutoffs, same-day/future exclusion, missing census and sources,
duplicates, invalid identities and counts, target-final independence, and corrupt
unused/failed receipts and symlink/traversal rejection. Synthetic fixtures only
validate the parser; the separate retained replay establishes the counts above.

Full-suite comparison in the same isolated environment (pytest 9.1.1,
cryptography 50.0.1, polymarket-us 0.1.2, httpx 0.28.1): assigned base
`9a8f4e0ef60f9739fd3bc21aeab8ac7b6a06cc68` has 1,473 passed, one skipped,
634 subtests passed and four failed; the implementation has 1,484 passed with
the same skip/subtests and four failures. All eleven new tests pass. The shared
failures are `test_acquire_lock_refuses_when_candidate_already_locked` and the
three lineup-watchlist policy expectations listed in the retained full logs.
The suite is **not green**; these failures are unchanged by this slice.
