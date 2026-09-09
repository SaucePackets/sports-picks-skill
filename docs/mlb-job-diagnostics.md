# Read-only MLB job diagnosis

`scripts/mlb_job_diagnostics.py` reads explicit schedule roots and cron SQLite
stores. It never invokes cron, writes a receipt, renders an execution task,
calls an order endpoint, or changes policy. It is not in the deployment manifest.
Its exit code is 0 for a readable report (including contradictions), 2 for
unreadable/invalid evidence. A report is not an eligibility verdict.

```sh
python3 scripts/mlb_job_diagnostics.py \
  --runtime-root /snapshot/home/projects/sports-picks-runtime \
  --script-cwd /snapshot/home/.hermes/profiles/vig/scripts \
  --home /snapshot/home --day 2026-09-08 \
  --execution-db /snapshot/home/.hermes/cron/executions.db \
  --execution-db /snapshot/home/.hermes/profiles/vig/cron/executions.db \
  --job-id example-poller \
  --since 2026-09-08T21:31:29Z --until 2026-09-08T22:40:00Z
```

Use operator-provided paths and job IDs. `--home` supplies the gate's fallback
home; `SPORTS_PICKS_ROOT`, if set, retains its production precedence and is
reported explicitly. The supplied script cwd must come from inspection of the
scheduler invocation. `jobs.json.workdir` alone does not establish subprocess
cwd. A copied snapshot needs its paths rebased consistently.

## A completed pre-check can read the wrong checkout

The observed Hermes scheduler at `869228cab4a8276d3b4c78da9d9939670c47bd0f`
has different call paths:

- `cron/scheduler.py:_run_no_agent_job` resolves the job workdir and passes it
  to `_run_job_script_with_claim_heartbeat`.
- The agent-mode pre-check in that same file calls the helper without workdir.
- `cron/scheduler_script.py:_run_job_script` consequently uses `path.parent`
  as subprocess cwd. The later agent workdir setup cannot affect a pre-check
  that already returned.

The sports gate's `resolve_root` uses the cwd only if it contains `.picks`,
otherwise falling back to `home/projects/sports-picks-skill` when it contains
`.picks`. If that checkout has no dated schedule, `mlb_execution_gate.main`
returns 0 with zero stdout. Hermes treats zero output as a skipped AI call and
successful job completion. Thus a correctly configured runtime workdir and a
completed cron row can coexist with an unread approved runtime schedule.

`tests/test_mlb_job_diagnostics.py` reproduces this with synthetic directories:
an approved runtime row, an empty developer checkout, and a profile script cwd.
The real root resolver selects the developer checkout; the real missing-file
gate branch returns silently. Passing runtime as cwd or an explicit root
override makes the resolver select runtime. No synthetic candidate is executed.

This proves the mechanism in inspected source and fixtures. Historical loaded
Python code, environment, and input bytes require their own receipts; a current
source checkout or deploy receipt is not a fingerprint of a long-running process.
Fixing the scheduler belongs to a separately authorized Hermes change. Do not
work around it by altering jobs, forcing a run, or changing fallback behavior.

## Keep stages separate

| Stage | Evidence to inspect | Limit |
|---|---|---|
| Fired | Owning profile's `cron/executions.db`, `started_at`, status | A claim alone is not a start; completed is not dispatch |
| Analyzed | `cron/output/<job-id>/...` response and retained scan | Narrative is not a writer receipt |
| Written | `.picks/execute/<day>-schedule.json`, writer landing result | Current file is not immutable history |
| Validated | `.picks/journal/<day>-slate-receipt.json` and errors/provenance | Read counts alone do not establish input provenance |
| Reviewed | `.picks/journal/<day>-runs.jsonl`, refused-review archive | `no_reviewable_work` need not mean zero approved candidates |
| Approved | Persisted decision and routing fields | Approval is not execution eligibility or dispatch |
| Dispatched/filled | Agent handoff, exact inputs, guarded order receipt, ledger | Missing evidence remains unknown |

Read both default and profile stores if ownership is disputed. SQLite opens
with `mode=ro`; missing databases fail rather than being created as empty ones.
Rows are joined on job ID, then timestamp offsets are normalized before applying
the half-open window. Output identifies whether the start or only the claim put
the row in the window. An empty query says nothing about uninspected stores.

The report's numerical check distinguishes the documented market-only identity
from its label: raw and conservative probability equal DK fair within 1e-9,
haircut is zero, and adjustment/haircut lists are empty. Booleans, non-finite or
out-of-range values are invalid. Non-market versions are `not_applicable`, not
approved. This diagnostic does not add a production gate, waive existing gates,
or certify forecasts. Keep private candidate history and host snapshots outside
the public repository.
