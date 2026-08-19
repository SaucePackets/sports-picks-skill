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
- **Clean-main guard** — local modifications to tracked files in the runtime
  checkout abort the deploy; after reset, `HEAD` must equal `origin/main`.
- **Exact-tip guard** — `--expect-sha` pins the deploy to a reviewed commit.
- **State preservation** — existing `.picks/` is never modified or deleted;
  seeding only fills an absent/empty `.picks/` and never overwrites.
- **Checksummed profile copies** — each manifest file is copied atomically
  (temp + rename), then `sha256`-verified against the runtime checkout, then
  `py_compile`-smoke-checked. Prior profile scripts are backed up to a
  timestamped sibling directory first. Unmanaged files are warned about, never
  deleted.
- **Cron repoint is opt-in and paused-only** — `--repoint-cron-from` rewrites
  only `workdir` fields that exactly match the given path, aborts if any
  matched job is enabled, backs up `jobs.json`, and writes atomically. It never
  touches `enabled`, schedules, or prompts.

Every real deploy writes a receipt to `.deploy/receipt-<ts>.txt` with the
deployed SHA and per-file checksums.

Tested end-to-end in `tests/test_deploy_runtime.py` against a local fixture
origin built from this repo's own `scripts/` tree.
