# Dedicated runtime deployment

Vig cron must never execute from a developer checkout: feature branches, dirty
trees, and unmerged experiments would silently become live betting behavior.
`scripts/deploy-runtime.sh` maintains a dedicated **runtime checkout** that
always tracks clean `origin/main`, plus the **profile-local script copies**
Hermes cron actually resolves and executes.

## Layout

| Path | Role |
|------|------|
| `~/projects/sports-picks-skill` | Developer checkout — never executed by cron |
| `~/projects/sports-picks-runtime` | Runtime checkout — clean `origin/main`, hard-reset on every deploy |
| `~/projects/sports-picks-runtime/.picks/` | Runtime pick state — gitignored, never touched by deploys |
| `~/.hermes/profiles/vig/scripts/` | Profile-local copies of the cron entrypoints and their sibling imports |
| `~/projects/sports-picks-runtime/.deploy/` | Runtime marker + per-deploy receipts (checksums, SHA) |

Cron jobs set `workdir` to the runtime checkout. The gate scripts resolve their
state root as: `SPORTS_PICKS_ROOT` env override → cwd if it contains `.picks/`
→ `~/projects/sports-picks-skill` fallback. The deploy therefore refuses to
finish while the runtime checkout has no `.picks/` — otherwise scripts would
silently fall back to the developer checkout.

## Usage

```bash
# Preview everything, change nothing
scripts/deploy-runtime.sh --dry-run

# First deploy: seed runtime .picks from the old checkout (copy; source untouched)
scripts/deploy-runtime.sh \
  --expect-sha <merged-main-tip> \
  --seed-picks-from ~/projects/sports-picks-skill \
  --repoint-cron-from ~/projects/sports-picks-skill

# Routine redeploy after a merge to main
scripts/deploy-runtime.sh --expect-sha <merged-main-tip>
```

## Safety guards

- **Marker guard** — the script only hard-resets a checkout it created itself
  (`.deploy/runtime.marker`). Pointed at any other checkout, it aborts.
- **Clean-main guard** — local modifications *and untracked files* in the
  runtime checkout abort the deploy (gitignored `.picks/` and `.deploy/` state
  is excluded as usual); after reset, `HEAD` must equal `origin/main`.
- **Exact-tip guard, preflighted** — `--expect-sha` is compared against the
  remote tip resolved via `git ls-remote` *before* any clone, fetch, checkout,
  or reset, so a mismatch leaves the runtime checkout exactly as it was. The
  deployed `HEAD` is re-checked after the reset as well.
- **State preservation** — existing `.picks/` is never modified or deleted;
  seeding only fills an absent/empty `.picks/` and never overwrites.
- **Fail-closed profile install** — the complete manifest set is staged in a
  sibling directory (starting from the live set, so unmanaged files survive),
  `sha256`-verified against the runtime checkout, and `py_compile`-smoke-checked
  there. Only then is the live directory swapped in by rename, with the prior
  set kept as a timestamped `.bak-<ts>` sibling; a failed swap restores it. No
  failure path leaves a partial live install. Unmanaged files are warned about,
  never deleted.
- **Cron repoint is opt-in, paused-only, and preflighted** — the enabled-job
  refusal runs read-only *before* the profile install, so a refused deploy
  leaves both `jobs.json` and the live profile scripts untouched. The apply
  step rewrites only `workdir` fields that exactly match the given path, backs
  up `jobs.json`, and writes atomically. It never touches `enabled`, schedules,
  or prompts.
- **No baked-in home** — the manifest scripts resolve Hermes, risk limits, the
  canonical ledger, and settlement paths from the invoking user's home (or env
  overrides `HERMES_BIN`, `VIG_RISK_LIMITS_PATH`, `VIG_PICKS_FILE`,
  `SPORTS_PICKS_ROOT`); no `/home/<user>` literal is hardcoded.

Every real deploy writes a receipt to `.deploy/receipt-<ts>.txt` with the
deployed SHA and per-file checksums.

Tested end-to-end in `tests/test_deploy_runtime.py` against a local fixture
origin built from this repo's own `scripts/` tree.
