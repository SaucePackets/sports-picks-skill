# Repository cleanup and cron readiness audit — 2026-09-14

## Scope and limits

Extended the [pipeline audit](pipeline-audit-2026-09-14.md) across the complete
tracked-file inventory: 260 files before this extension, including 127 Python,
32 JSON, 9 shell, and 82 Markdown files. Inspected the installer, hooks,
dependency declarations, bundled skills and venue helpers, templates, current
entrypoints, tests, and historical-tool boundaries. All tracked Python/JSON/shell
files parsed successfully. This is an architecture, maintenance, and runtime
readiness audit, not a claim of line-by-line security verification or proof that
every external API, sport, and historical CLI works live.

## Cleanup implemented

- Removed `curator-report.md`: an unrelated report about 76 general agent skills,
  introduced in commit `30de23a`, with no repository references or matching library.
- Removed the installer's dead copy loop for untracked `.picks/PROCESS.md` and
  `.picks/REFLECTIONS.md`. Corrected Hermes/OpenClaw installation docs and README:
  nine skills are bundled; soccer is a reference, and runtime scripts/cron setup
  are separate from Markdown skill installation. Existing state is preserved.
- Corrected four seasonal helpers to parse zero-prefixed months in decimal.
  August/September previously raised Bash octal errors while still printing OK.
- Monthly calibration now groups by recorded confidence. It no longer silently
  calls a $25 High or $9 Small wager Medium, or guesses confidence for old records.
  Removed the hard-coded 54.5% break-even verdict: aggregate win-rate uncertainty
  cannot establish profitability at varying prices and stakes.
- Updated the lessons template to preserve pregame predictions/refusals and propose
  evidence-backed rules for review, rather than infer missed edges from winning
  passes. Updated the README's outdated lineup-only watchlist description.

Keep both venue helpers: they expose different documented interfaces with their
own tests; overlapping names do not prove a safe deletion. Keep the byte-identical
HTTP helper beside installed skills because standalone imports need it; provenance
checks enforce equality. Keep offline historical/checkpoint tooling because it is
used for reproducibility and several source files are hash-bound. Keep hooks that
prevent accidental private-ledger commits, even though clean clones exclude those
files. No supported dormant sport or offline CLI was removed solely for lacking a
cron reference.

## Validation

- Full suite: **1,790 passed; 714 subtests passed; one skipped**, 50.53 seconds.
  The skip requires the optional `sports_skills` package; that integration was
  not verified in this local environment.
- Isolated installer smoke: mocked clone of current skill files installed all
  nine skills and preserved an existing reflection sentinel. This checks copy/state
  behavior, not GitHub availability or Hermes skill indexing.
- All tracked Python ASTs, JSON documents, and shell syntax parsed successfully.
- Managed manifest resolves all 26 entries; vendored HTTP helpers match exactly.
- Focused regressions cover recorded confidence and August/September seasonal
  behavior. Earlier pipeline bypass mutations remain documented in the linked audit.

## Cron readiness (all nine Vig jobs)

Every job's workdir resolves to the dedicated runtime. Every named pre-run script
and Python script referenced in its prompt exists in the runtime/profile. Runtime
remains clean main at `c0f22d74b79c9f17af7347aea368cd83c0e31d6a` (PR #106).
PR #107 is open; its refactor is **not deployed**. This table describes the deployed
version, plus candidate test coverage, not a post-merge production certification.

| Job | Current verification | Still unverified / boundary |
|---|---|---|
| MLB morning `c9452052719c` | Fresh isolated deployed scanner succeeded for all 10 games; writer/receipt tests pass; prompt scripts exist | Last scheduler row retains the earlier missing-CA failure. Full repaired agent production run and delivery need verification |
| MLB evening `27087cc00dfa` | Same scanner/writer dependencies; append and occupancy tests pass; workdir correct | Next complete evening agent run has not been observed |
| MLB review `75e72e2dc5be` | Natural scheduled cycles succeeded at 13:00 and 13:15 CT; audit timestamp confirms invocation | Verified empty-card/reporting path; no fresh valid candidate promotion |
| MLB execution `84095861d05d` | Script exists; execution contract regression and bypass tests pass | Deliberately disabled; no order path invoked or enabled |
| MLB postgame `ee797915248f` | Actual gate exits 0 silently; canonical ledger has zero open picks; receipt and ledger reconciliation both exit 0 | Active settlement child-agent branch not invoked |
| Monthly calibration `073bda7f7d56` | Actual deployed report exits 0 with 44 decided picks; candidate report regression suite passes | Corrected confidence grouping waits for #107 deployment |
| NFL slate `bddc7e59a276` | TLS scoreboard request succeeds (16 events); adapter and writer exist; prior isolated 1-actionable-game bundle verified | Original row retains Buzz delivery failure. Full source-complete football handicap not established; adapter remains unmanaged |
| NFL settlement `bc72aadee4f5` | Actual final-score CLI succeeds for Sep 13; no open official ledger picks | Active agent settlement and delivery not exercised |
| Weekly discipline `0dc33c64fa3f` | Enabled, correct root; ledger/schedules/policy accessible; last recorded status OK | Agent report content and delivery not re-run; status alone is not evidence of correctness |

The Vig gateway is active. The running process's CA paths and profile-configured
CA/Buzz CLI paths exist. Hermes scheduler source reloads profile dotenv on each
run; startup `/proc` environment alone does not show subsequent reloads. Public
NFL HTTPS succeeded with the configured CA. No new Buzz message was sent during
this audit, so outbound delivery is not freshly certified here. Historical error
rows were preserved rather than relabeled as successes.

## Fresh pick check

Read-only market search succeeded for all ten matchups. Each selected moneyline
was matched to both team names and the exact scheduled UTC start (including
Central-evening games on the next UTC date), avoiding similarly named next-day
markets. All 20 side quotes were compared with freshly scanned two-sided de-vig
sportsbook fair probabilities. At approximately **18:21:49 UTC**, zero sides
cleared the configured **5 percentage point** floor. Maximum gap: **1.0394pp**.
This is a timestamped indicative quote comparison, not executable depth or a
bet recommendation. No model admission or risk limits changed.

The isolated scan completed at **18:21:37 UTC** with 10/10 reconciled games,
zero source failures, and zero fully complete reads. The only missing fields
were away/home lineups in every game: the source said a complete nine-player
batting order had not been published. This was more specific than a general
"missing data" diagnosis. No authoritative schedule or receipt was overwritten.
Server evidence: `/tmp/sports-repo-audit-20260914/`; a local retained copy of scan,
market responses, and the 20-side comparison accompanies the task report.

No qualifying fallback pick was found. This does not prove an independent model
could not find an edge, or that no later price will qualify. No independent MLB
model is admitted in the deployed configuration. The next substantive work is a
pregame research follow-up path for incomplete games and a frozen prospective
model evaluation against the market baseline. More duplicate validators or
post-loss rules cannot supply predictive evidence.

## Activation checklist after Jerry merges #107

Use the merged deployment driver and verify its receipt/profile hashes, then
repeat the safe report/reconciliation checks. Observe complete morning/evening
and NFL agent runs with matching writer artifacts and successful delivery. Keep
the execution poller paused. Only then describe the refactor as verified in cron;
passing local tests does not establish those live results.
