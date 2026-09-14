# September 14 job output audit

The NFL run `nfl-20260914-1625` produced **no proposed pick and no inactives watchlist** for Denver–Kansas City (`401872931`). Its saved schedule says analysis complete. All five bundle files exist under the runtime `.picks/nfl/runs/` directory. The existing verifier successfully recomposed the report and checked receipt hashes at the receipt's original `2026-09-14T16:28:31.109237+00:00` timestamp. Current-time verification correctly refuses stale evidence; this historical verification is not fresh market validation.

The NFL execution `72e9a6b0f65e4ef68564726d85503a9c` is `completed` with delivery outcome `failed`. The job status is `delivery_failed`. No corresponding detail row remains in the delivery database, so the exact delivery failure cause is not established by this audit. The gateway is currently active. Analysis completion, delivery success, and a proposed pick are different outcomes.

The assessment recorded a 54% probability against a 55% exchange research ask, with several other gates failing, including official QB confirmation. There is no supported positive-edge pick in this bundle. Its supplied research is not independent source authentication.

## MLB attempts

| Output time CT | Result | Evidence |
|---|---|---|
| 14:51 | Writer rejected two unavailable exchange quotes | Producer `fc103ae24a0e4dd3a457f24f6a4e0b18`; defect addressed by merged PR #110 |
| 15:08 | Producer exceeded 300-second subprocess timeout | Producer `4e69c14bffda46afb0daa6b9915da568`; distinct from writer defect |
| 15:21 | Ten reads landed, zero candidates | Producer `73114fa871654cefbdc61535fd5d97da`; six passes and four incomplete-input reads |

The successful run began before deployment of PR #110 and contained no `not_priced` reads. It cannot prove the previously failing writer case ran successfully in production. The exact failed input was instead validated in isolated base/tip replay. Runtime is now `d0352ed44ff0e5967b273dbcce6462082b1543d7`; the later review at 15:30 CT completed successfully.

The producer receipt is complete with corroborated writer provenance, but does not establish acquisition freshness. The successful slate still uses market fallback. Its largest recorded side edge is approximately 2.68 percentage points against the unchanged five-point floor.

## Reporting changes

Successful research now reports actual disposition counts from the saved schedule, explicitly including zero candidate reads. Counts include retained records and do not imply newly created proposals or approval. Timeout failures retain partial stdout/stderr separately for scan and producer, with a short phase/time/log-directory error instead of displaying the entire command and prompt. This improves diagnostics; it does not increase the timeout or establish that future producer runs finish within it.

NFL's immutable report renderer is unchanged: altering it in place would invalidate historical report recomposition. A future report-format change needs explicit versioning.

No old artifacts were overwritten, no jobs were forced, and no delivery retry was sent. Execution job `84095861d05d` remains disabled and paused. No order, approval, policy, or model-admission state was changed.
