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
- **Exact-tip guard, race-free** — `--expect-sha` must be a full 40-hex commit
  id and is compared for exact equality (an abbreviation pins nothing). The
  `git ls-remote` preflight buys an early refusal before any clone or fetch,
  but it is not authoritative: the remote can advance in that window. The
  binding check is against the commit the deploy actually obtained — the
  `FETCH_HEAD` of a refs-only `git fetch` on a redeploy, or a `--no-checkout`
  clone on first deploy — and only a match lets the checkout/reset run. A
  mismatch leaves the runtime checkout exactly as it was (first deploy discards
  its own clone). The deployed `HEAD` is re-checked after the reset as well.
- **State preservation** — existing `.picks/` is never modified or deleted;
  seeding only fills an absent/empty `.picks/` and never overwrites.
- **Fail-closed profile install** — the complete manifest set is staged in a
  sibling directory (starting from the live set, so unmanaged files survive),
  `sha256`-verified against the runtime checkout, and `py_compile`-smoke-checked
  there. Only then is the live directory swapped in by rename, with the prior
  set kept as a timestamped `.bak-<ts>` sibling; a failed swap restores it. No
  failure path leaves a partial live install. Unmanaged files are warned about,
  never deleted.
- **No symlink escape** — a symlinked profile scripts dir is refused outright.
  Managed staged destinations are unlinked before they are written, so a
  symlink inherited from the live set can never be followed out of the staging
  directory, and every staged *and* installed manifest path is asserted to be a
  regular non-symlink file whose `sha256` matches the runtime checkout.
- **Cron repoint is opt-in, paused-only, and preflighted** — the enabled-job
  refusal runs read-only *before* the profile install, so a refused deploy
  leaves both `jobs.json` and the live profile scripts untouched. The apply
  step rewrites only `workdir` fields that exactly match the given path, backs
  up `jobs.json`, and writes atomically. It never touches `enabled`, schedules,
  or prompts.
- **No baked-in home** — the manifest scripts resolve Hermes, risk limits, the
  canonical ledger, and settlement paths from the invoking user's home (or env
  overrides `HERMES_BIN`, `VIG_RISK_LIMITS_PATH`, `VIG_PICKS_FILE`,
  `SPORTS_PICKS_ROOT`); no `/home/<user>` literal is hardcoded. Enforced over
  the manifest scripts *and* every text file under `skills/`, against any
  account name rather than one known-bad literal.
- **`--dry-run` previews a behind runtime** — a dry run skips the reset, so the
  post-reset `--expect-sha` comparison is skipped with it and the preview
  reports which commit a real deploy would move to. The pin itself is still
  enforced read-only against the remote in Phase 0, so a wrong `--expect-sha`
  is still refused in a dry run.

## The order-executor venv is NOT deployed

`skills/sports-picks/scripts/polymarket_us_sdk_bet.py` re-execs into
`<runtime>/.venv/bin/python`, where the Polymarket SDK lives. The deploy neither
creates that venv nor installs into it: it needs no network at deploy time, and
`.venv/` is gitignored so an existing one is never touched or reset.

That re-exec is guarded by a path-exists test, so a runtime dir **without** a
venv takes the silent path — no error at deploy, no error at import, and a
failure at order time. A fresh runtime dir is exactly that case. The deploy
therefore ends with a read-only check that warns (never fails — the review and
settlement lanes do not need this venv) and prints the command:

```bash
python3 -m venv <runtime>/.venv
<runtime>/.venv/bin/python -m pip install -r \
  <runtime>/skills/sports-picks/scripts/requirements-exec.txt
```

The interpreter is resolved from `SPORTS_PICKS_RUNTIME_DIR` (what `--runtime-dir`
sets), or overridden outright with `SPORTS_PICKS_VENV_PYTHON`.

Every real deploy writes a receipt to `.deploy/receipt-<ts>.txt` with the
deployed SHA and per-file checksums.

Tested end-to-end in `tests/test_deploy_runtime.py` against a local fixture
origin built from this repo's own `scripts/` tree.

To verify after a deploy that the runtime checkout and the profile copies still
match canonical `origin/main`, run the read-only
`scripts/check_script_provenance.py`. See
[script-provenance.md](script-provenance.md) for the canonical-source decision
and the full inventory of derived copies.
