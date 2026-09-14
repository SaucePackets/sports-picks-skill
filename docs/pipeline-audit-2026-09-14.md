# Sports-picks pipeline audit — 2026-09-14

## Finding

The recent work has improved recording, input validation, provenance, and order
safety. It has not established a deployed predictive model that beats the market.
The server's `mlb_deployed_models` record is absent. The current fallback uses
sportsbook fair probability; passing a model-admission or artifact test is not
evidence of better picks. More validators cannot supply the missing model.

The 16 commits through PR #106 concern candidate contracts, nonce/root checks,
retained-evidence binding, data completeness, NFL construction, schema vocabulary,
occupancy, and historical acquisition/replay. Their documented guarantees concern
those mechanisms, not improved betting performance. This is a source/runtime
architecture audit, not an exhaustive line-by-line security review or an outcome
study of each historical change.

## What ran successfully

PR #106 merged as `c0f22d74b79c9f17af7347aea368cd83c0e31d6a` and was deployed
using the merged deploy driver. The dedicated runtime is clean `main` at that
commit. All 26 managed Vig profile copies matched the checkout by SHA256;
`check_script_provenance.py --ref origin/main` reported clean.

Deployment receipt on the server:
`~/projects/sports-picks-runtime/.deploy/receipt-20260914-174909.txt`.

The actual profile entrypoint `vig_mlb_review_gate.py` was invoked from the
runtime directory after confirming today's candidates and watchlist were empty.
It exited 0 and wrote `2026-09-14-decision-audit.json` at 17:50:17 UTC. The report's
schedule hash matched the input bytes, and the schedule stayed byte-identical.
It reports ten incomplete games with no linked recheck. This verifies the
no-work/reporting path; it does not verify a newly generated valid pick or order.
268 focused tests and 55 subtests passed using the server runtime's Python.

The next scheduled no-agent review also succeeded: scheduled for 13:00 CT,
dispatched at 13:00:25.092, status `ok`. It independently refreshed the audit at
18:00:25.533 UTC with schedule hash
`337966e3e2543c4c8d8e0daaa9480be800e4608121f699c7f46ae9dc9fdb894d`.
This verifies natural scheduler invocation, not only the manual smoke test.

The execution poller remained disabled. The risk-limits hash before deployment
was `bb0f140db8d22dceb7cc9a5c4c7dfe6ab0273ee36a2bbfc7a3ca73e38027cfc6`.
No model was admitted, no threshold changed, and no order was submitted.

## Changes from this audit

| Finding | Action | Evidence limit |
|---|---|---|
| Execution/review callers repeat model, component, and edge checks already performed by `mlb_candidate_contract.candidate_errors` | Remove the duplicate calls and unused imports; retain the shared call at production, approval, eligibility, and final lock | Full suite and deliberate bypass mutations test the boundaries; this is not removal of the underlying guards |
| Settlement still recommends a 2.5–3% floor as a tightening and calls policies validated after 15 bets | Keep historical cohort statistics; remove obsolete tuning/validation prescriptions | No active numeric policy change; proposal is in this PR |
| Reflection templates promote repeat losses into permanent gates | Record proposed rules for review, with repeated/structural evidence and sample limitations | Existing learned rules remain intact; no claim that every existing veto is useful or harmful |
| Live static reference says High $30, Medium $18, and Medium may pass only 4/5 gates | Backed up and corrected the live reference to current policy caps and all-required-gates semantics; corrected obsolete ledger paths and outcome-only process grading | Operational documentation correction only; risk file unchanged |
| Unmanaged 22-line `test_vig_review_gate.py` lives in production's import directory | Archived outside the import directory after checking all Hermes cron JSON and profile Python callers for references | Retained exact bytes; no automated job referenced it in that inspection |
| Three watchlist tests inherit authorization from the developer host | Declare standing authorization explicitly in those tests | Production authorization is unchanged; authorized and unauthorized cases remain tested |
| Old deploy driver loads its old manifest before updating its checkout | Used the merged driver for #106 and documented fetching the pinned driver first | A successful checkout reset alone does not prove the new manifest was installed |

The two live-file backups are under
`~/.hermes/profiles/vig/backups/pipeline-audit-20260914-175531/`:
`mlb-data-reference.before.md` and `test_vig_review_gate.py`.
The static-reference hash changed from
`837d5779d980cf4a95d6476a09556071c87a0aca0bb8062bd35e0135197ae281` to
`08c507fd47791da68c1ad43a7583180087d254c6c318e5250e4da9c7dc1ec2ea`.
The learned-rule file was not rewritten.

## Keep, consolidate, and defer

- **Keep the writer, input-completeness checks, schema/nonce binding, immutable
  occupancy, price refresh, review ownership, portfolio caps, deduplication,
  receipts, and settlement reconciliation.** These address demonstrated
  recording/execution failures. They do not predict winners.
- **Consolidate duplicate validation at callers.** Share the implementation;
  continue checking it independently at every boundary that can accept a card.
  Baseball evidence, tradeability, daily exposure, and idempotency answer
  different questions and remain separate.
- **Keep historical tools as offline tools.** The 13 acquisition/shadow/replay
  modules listed below account for about 2,753 lines and are not imported by the
  managed profile scripts. Checkpoint JSON binds several of their implementations
  by hash. An unreferenced scheduled entrypoint is not proof that a documented
  offline CLI is dead.
- **Use decision audit for current-cycle gaps and measurement lane for
  longitudinal observations.** Eligibility is price arithmetic; decision audit
  adds model errors and recheck state; measurement joins all-game observations
  to final outcomes. Their overlap does not make their evidence claims identical.
  Prose replay is historical hypothesis generation, not prediction evidence.
- **Version the profile-local NFL actionable-scan adapter in a dedicated change.**
  It is active and unmanaged, so removing it would break the NFL job. The other
  unmanaged copy, `mlb_producer_prompt_contract.py`, is an operational migration
  tool; it should not be mistaken for a live predictive model.

## Remaining work that can actually address the drought

1. Close the research follow-up gap. The current watchlist handles already
   qualified candidates waiting for allowed lineup/starter inputs; it does not
   schedule research for every incomplete game. Today's ten unmatched incomplete
   games are evidence of missing accounting, not ten proven lost bets. A follow-up
   worker needs explicit ownership, freshness, pre-pitch deadlines, and terminal
   outcomes without relaxing candidate admission.
2. Run a frozen prospective experiment on all scheduled games. Retain timestamped
   inputs and predictions before outcomes, compare one version against sportsbook
   fair probability, and use the existing chronological/provenance/admission
   contracts. Historical checkpoint completion and selected winning passes cannot
   substitute for that evidence.
3. Measure subjective vetoes as hypotheses. Preserve the original reason and input
   evidence, then compare predeclared cohorts. Do not add a permanent gate after
   two losses, delete one after two winning passes, or treat a favorable small
   sample as validation.
4. Verify an actual future candidate through review before considering execution
   activation. The poller remains paused; creating an audit file is not an order
   test and does not prove live fills.

## Validation of the cleanup

Baseline and cleanup were tested in the same Python 3.14 environment with pytest,
pytest-subtests, and the pinned shadow requirements. Baseline has three
host-dependent watchlist failures and one skip. The cleanup fixes the fixtures
rather than ignoring those failures; all 71 watchlist tests and 12 subtests pass.
Final whole-suite result: **1,781 passed, 714 subtests passed, 1 skipped, zero failures**. The skip is `test_sports_skills_espn_shim.py`, whose optional `sports_skills` dependency is unavailable in this environment.

Independent throwaway mutations bypassed the shared contract at each boundary:
execution eligibility triggered six test failures; final lock triggered four;
review routing triggered two. The mutations were never applied to the worktree
or server. Existing append/receipt tests continue to protect retained records.

## Top-level Python inventory

This inventory classifies the 61 `scripts/*.py` entrypoints/modules; it does not
claim all are imported by cron. `Managed` means listed in the deployment manifest,
`Historical` means the retained acquisition/shadow/replay family, and `Tool` means
an auxiliary CLI/library outside that manifest. The skill's venue executors and
sport data scripts are additional dependencies, not deletion candidates merely
because they live outside this directory.

| File | Role | Approximate lines |
|---|---|---:|
| [__init__.py](../scripts/__init__.py) | Tool | 0 |
| [check_script_provenance.py](../scripts/check_script_provenance.py) | Tool | 310 |
| [execution_guard.py](../scripts/execution_guard.py) | Managed | 605 |
| [http_util.py](../scripts/http_util.py) | Managed | 139 |
| [intl_soccer_model.py](../scripts/intl_soccer_model.py) | Tool | 485 |
| [mlb_appearance_census.py](../scripts/mlb_appearance_census.py) | Historical | 169 |
| [mlb_baseball_evidence.py](../scripts/mlb_baseball_evidence.py) | Managed | 366 |
| [mlb_bulk_starter_admission.py](../scripts/mlb_bulk_starter_admission.py) | Historical | 289 |
| [mlb_bulk_starter_snapshots.py](../scripts/mlb_bulk_starter_snapshots.py) | Historical | 293 |
| [mlb_candidate_contract.py](../scripts/mlb_candidate_contract.py) | Managed | 54 |
| [mlb_chronological_acquire.py](../scripts/mlb_chronological_acquire.py) | Historical | 78 |
| [mlb_chronological_admission.py](../scripts/mlb_chronological_admission.py) | Historical | 211 |
| [mlb_chronological_checkpoint.py](../scripts/mlb_chronological_checkpoint.py) | Historical | 83 |
| [mlb_chronological_starter_probe.py](../scripts/mlb_chronological_starter_probe.py) | Historical | 107 |
| [mlb_data_completeness.py](../scripts/mlb_data_completeness.py) | Managed | 229 |
| [mlb_decision_audit.py](../scripts/mlb_decision_audit.py) | Managed | 201 |
| [mlb_eligibility_report.py](../scripts/mlb_eligibility_report.py) | Managed | 460 |
| [mlb_execution_gate.py](../scripts/mlb_execution_gate.py) | Managed | 493 |
| [mlb_final_scores.py](../scripts/mlb_final_scores.py) | Managed | 91 |
| [mlb_game_reads.py](../scripts/mlb_game_reads.py) | Managed | 1245 |
| [mlb_historical_admission.py](../scripts/mlb_historical_admission.py) | Historical | 305 |
| [mlb_job_diagnostics.py](../scripts/mlb_job_diagnostics.py) | Tool | 144 |
| [mlb_lineup_watchlist.py](../scripts/mlb_lineup_watchlist.py) | Managed | 1319 |
| [mlb_market_free_checkpoint.py](../scripts/mlb_market_free_checkpoint.py) | Historical | 315 |
| [mlb_measurement_lane.py](../scripts/mlb_measurement_lane.py) | Tool | 1040 |
| [mlb_model_benchmark.py](../scripts/mlb_model_benchmark.py) | Historical | 247 |
| [mlb_model_eval_dataset.py](../scripts/mlb_model_eval_dataset.py) | Tool | 339 |
| [mlb_postgame_evidence.py](../scripts/mlb_postgame_evidence.py) | Managed | 724 |
| [mlb_probability_chain_report.py](../scripts/mlb_probability_chain_report.py) | Tool | 257 |
| [mlb_probability_model.py](../scripts/mlb_probability_model.py) | Managed | 824 |
| [mlb_producer_prompt_contract.py](../scripts/mlb_producer_prompt_contract.py) | Tool | 404 |
| [mlb_provenance_checkpoint.py](../scripts/mlb_provenance_checkpoint.py) | Historical | 69 |
| [mlb_real_shadow_capture.py](../scripts/mlb_real_shadow_capture.py) | Historical | 287 |
| [mlb_runtime_policy.py](../scripts/mlb_runtime_policy.py) | Managed | 311 |
| [mlb_shadow_collection.py](../scripts/mlb_shadow_collection.py) | Historical | 300 |
| [mlb_slate_receipt.py](../scripts/mlb_slate_receipt.py) | Managed | 412 |
| [mlb_slate_writer.py](../scripts/mlb_slate_writer.py) | Managed | 1006 |
| [mlb_stage2_scan.py](../scripts/mlb_stage2_scan.py) | Managed | 712 |
| [nfl_card_writer.py](../scripts/nfl_card_writer.py) | Tool | 521 |
| [nfl_final_scores.py](../scripts/nfl_final_scores.py) | Tool | 103 |
| [nfl_stage2_scan.py](../scripts/nfl_stage2_scan.py) | Tool | 548 |
| [numeric_util.py](../scripts/numeric_util.py) | Managed | 101 |
| [polymarket_wc_markets.py](../scripts/polymarket_wc_markets.py) | Tool | 239 |
| [receipts_ledger_reconcile.py](../scripts/receipts_ledger_reconcile.py) | Managed | 105 |
| [resolve_exec_venv.py](../scripts/resolve_exec_venv.py) | Tool | 56 |
| [sports_skills_espn_shim.py](../scripts/sports_skills_espn_shim.py) | Tool | 161 |
| [vig-review-verify.py](../scripts/vig-review-verify.py) | Tool | 312 |
| [vig_calibration_report.py](../scripts/vig_calibration_report.py) | Managed | 132 |
| [vig_drought_diagnostic.py](../scripts/vig_drought_diagnostic.py) | Tool | 1506 |
| [vig_historical_audit.py](../scripts/vig_historical_audit.py) | Tool | 1296 |
| [vig_ledger_reconcile.py](../scripts/vig_ledger_reconcile.py) | Managed | 266 |
| [vig_loss_evidence_report.py](../scripts/vig_loss_evidence_report.py) | Tool | 643 |
| [vig_mlb_review_gate.py](../scripts/vig_mlb_review_gate.py) | Managed | 4 |
| [vig_pick_replay.py](../scripts/vig_pick_replay.py) | Tool | 948 |
| [vig_postgame_gate.py](../scripts/vig_postgame_gate.py) | Managed | 145 |
| [vig_refusal_hypothesis_scan.py](../scripts/vig_refusal_hypothesis_scan.py) | Tool | 732 |
| [vig_review_gate_common.py](../scripts/vig_review_gate_common.py) | Managed | 2039 |
| [vig_run_journal.py](../scripts/vig_run_journal.py) | Managed | 334 |
| [vig_runtime_verify.py](../scripts/vig_runtime_verify.py) | Tool | 468 |
| [vig_slate_gate_replay.py](../scripts/vig_slate_gate_replay.py) | Tool | 1894 |
| [vig_soccer_review_gate.py](../scripts/vig_soccer_review_gate.py) | Managed | 4 |
