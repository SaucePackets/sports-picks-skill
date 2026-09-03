#!/usr/bin/env bash
# Deploy the dedicated sports-picks runtime checkout and the Vig profile-local
# script copies. The runtime checkout always tracks clean origin/main; Vig cron
# must execute from it (via profile-local script copies), never from a
# developer checkout. See docs/deploy-runtime.md.
set -euo pipefail

REPO_URL="${SPORTS_PICKS_REPO_URL:-https://github.com/SaucePackets/sports-picks-skill.git}"
RUNTIME_DIR="${SPORTS_PICKS_RUNTIME_DIR:-$HOME/projects/sports-picks-runtime}"
PROFILE_SCRIPTS_DIR="${VIG_PROFILE_SCRIPTS_DIR:-$HOME/.hermes/profiles/vig/scripts}"
CRON_JOBS_FILE="${VIG_CRON_JOBS_FILE:-$HOME/.hermes/profiles/vig/cron/jobs.json}"
BRANCH="main"
EXPECT_SHA=""
SEED_PICKS_FROM=""
REPOINT_CRON_FROM=""
DRY_RUN=0

# Marker distinguishing a checkout this script created (and may hard-reset)
# from any other checkout on disk. Never present in a developer checkout.
MARKER_REL=".deploy/runtime.marker"

# Profile-local copies: every cron entrypoint plus every sibling module those
# entrypoints import from the profile scripts directory.
PROFILE_MANIFEST=(
  execution_guard.py
  http_util.py
  mlb_baseball_evidence.py
  mlb_eligibility_report.py
  mlb_execution_gate.py
  mlb_final_scores.py
  mlb_game_reads.py
  mlb_lineup_watchlist.py
  mlb_postgame_evidence.py
  mlb_probability_model.py
  mlb_runtime_policy.py
  mlb_stage2_scan.py
  numeric_util.py
  receipts_ledger_reconcile.py
  vig_calibration_report.py
  vig_ledger_reconcile.py
  vig_mlb_review_gate.py
  vig_postgame_gate.py
  vig_review_gate_common.py
  vig_run_journal.py
  vig_soccer_review_gate.py
)

usage() {
  cat <<'EOF'
Usage: deploy-runtime.sh [options]

Options:
  --runtime-dir DIR        Runtime checkout path (default: ~/projects/sports-picks-runtime)
  --profile-scripts DIR    Vig profile scripts dir (default: ~/.hermes/profiles/vig/scripts)
  --cron-jobs FILE         Vig cron jobs.json (default: ~/.hermes/profiles/vig/cron/jobs.json)
  --repo-url URL           Git remote to deploy from
  --expect-sha SHA         Refuse to deploy unless origin/main resolves to this commit
  --seed-picks-from DIR    First-deploy only: copy DIR/.picks into the runtime checkout
                           when the runtime has no .picks yet. Never overwrites.
  --repoint-cron-from DIR  Rewrite cron job workdirs that exactly equal DIR to the
                           runtime dir. Aborts if any matched job is enabled.
  --dry-run                Print planned actions; perform no writes at all
  -h, --help               Show this help

Environment overrides: SPORTS_PICKS_REPO_URL, SPORTS_PICKS_RUNTIME_DIR,
VIG_PROFILE_SCRIPTS_DIR, VIG_CRON_JOBS_FILE.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --runtime-dir) RUNTIME_DIR="$2"; shift 2 ;;
    --profile-scripts) PROFILE_SCRIPTS_DIR="$2"; shift 2 ;;
    --cron-jobs) CRON_JOBS_FILE="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --expect-sha) EXPECT_SHA="$2"; shift 2 ;;
    --seed-picks-from) SEED_PICKS_FROM="$2"; shift 2 ;;
    --repoint-cron-from) REPOINT_CRON_FROM="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

log() { echo "[deploy-runtime] $*"; }
die() { echo "[deploy-runtime] ERROR: $*" >&2; exit 1; }

MARKER="$RUNTIME_DIR/$MARKER_REL"
TS="$(date +%Y%m%d-%H%M%S)"

[ "$RUNTIME_DIR" != "$PROFILE_SCRIPTS_DIR" ] || die "runtime dir and profile scripts dir must differ"

# A symlinked profile scripts dir would put the staging swap (and every managed
# write) outside the directory this script believes it owns.
[ ! -L "$PROFILE_SCRIPTS_DIR" ] || die "profile scripts dir is a symlink: $PROFILE_SCRIPTS_DIR — refusing to manage it"

# --- Phase 0: --expect-sha preflight (read-only, before any mutation) -------
# The pin is a full 40-hex commit id compared for exact equality: a prefix match
# would let an abbreviation (in the limit, one hex character) pin nothing.
# ls-remote only buys an early refusal — it cannot be trusted as the deployed
# commit, because the remote can advance between resolving it and fetching. The
# authoritative check is against the actually-fetched commit, below, before the
# worktree is touched.

if [ -n "$EXPECT_SHA" ]; then
  case "$EXPECT_SHA" in
    *[!0-9a-fA-F]*) die "--expect-sha must be a full 40-hex commit sha, got: $EXPECT_SHA" ;;
  esac
  [ "${#EXPECT_SHA}" -eq 40 ] || die "--expect-sha must be a full 40-hex commit sha (got ${#EXPECT_SHA} chars): $EXPECT_SHA"
  EXPECT_SHA="$(printf '%s' "$EXPECT_SHA" | tr 'A-F' 'a-f')"

  REMOTE_SHA="$(git ls-remote "$REPO_URL" "refs/heads/$BRANCH" | cut -f1)"
  [ -n "$REMOTE_SHA" ] || die "cannot resolve refs/heads/$BRANCH on $REPO_URL"
  if [ "$REMOTE_SHA" = "$EXPECT_SHA" ]; then
    log "preflight: origin/$BRANCH is $REMOTE_SHA (matches --expect-sha)"
  else
    die "origin/$BRANCH tip $REMOTE_SHA does not match --expect-sha $EXPECT_SHA — refusing before any runtime changes"
  fi
fi

# --- Phase 1: runtime checkout at clean origin/main -------------------------

DEPLOY_TARGET_SHA=""

if [ ! -d "$RUNTIME_DIR/.git" ]; then
  if [ -e "$RUNTIME_DIR" ] && [ -n "$(ls -A "$RUNTIME_DIR" 2>/dev/null)" ]; then
    die "$RUNTIME_DIR exists, is not empty, and is not a git checkout — refusing"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN: would clone $REPO_URL into $RUNTIME_DIR (branch $BRANCH)"
  else
    log "cloning $REPO_URL into $RUNTIME_DIR"
    runtime_preexisting=0
    if [ -d "$RUNTIME_DIR" ]; then runtime_preexisting=1; fi
    # --no-checkout: the pin is verified against the commit we actually got
    # before a single file is written into the working tree.
    git clone --no-checkout --branch "$BRANCH" "$REPO_URL" "$RUNTIME_DIR"
    DEPLOY_TARGET_SHA="$(git -C "$RUNTIME_DIR" rev-parse HEAD)"
    if [ -n "$EXPECT_SHA" ] && [ "$DEPLOY_TARGET_SHA" != "$EXPECT_SHA" ]; then
      rm -rf "$RUNTIME_DIR"
      if [ "$runtime_preexisting" = 1 ]; then mkdir -p "$RUNTIME_DIR"; fi
      die "cloned $BRANCH tip $DEPLOY_TARGET_SHA does not match --expect-sha $EXPECT_SHA — clone discarded, nothing deployed"
    fi
    git -C "$RUNTIME_DIR" checkout -q "$BRANCH"
    mkdir -p "$(dirname "$MARKER")"
    echo "runtime checkout created by deploy-runtime.sh $TS" > "$MARKER"
  fi
else
  # Never hard-reset a checkout this script did not create (e.g. a developer
  # checkout someone pointed us at by mistake).
  [ -f "$MARKER" ] || die "$RUNTIME_DIR is a git checkout without $MARKER_REL — refusing to manage it"
  origin_url="$(git -C "$RUNTIME_DIR" remote get-url origin)"
  [ "$origin_url" = "$REPO_URL" ] || die "runtime origin is $origin_url, expected $REPO_URL"
  # Untracked files count as dirt too: anything unmanaged in a managed runtime
  # is a hand edit that a redeploy must not silently bless. Ignored paths
  # (.picks/, .deploy/) are excluded by gitignore as usual.
  dirty="$(git -C "$RUNTIME_DIR" status --porcelain --untracked-files=all)"
  [ -z "$dirty" ] || die "runtime checkout has local modifications or untracked files — investigate, do not deploy:
$dirty"
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN: would fetch origin and hard-reset $RUNTIME_DIR to origin/$BRANCH"
    # Read-only, so the preview can name the commit it would move to. This is
    # not a pin — the real deploy re-resolves it from the fetch (Phase 0's
    # comment on why ls-remote is not authoritative applies here too).
    DRY_RUN_TARGET_SHA="$(git ls-remote "$REPO_URL" "refs/heads/$BRANCH" | cut -f1)"
  else
    # fetch updates refs only — HEAD, the index, and the working tree are
    # untouched. The pin is checked against the commit this fetch actually
    # brought down, so a remote that advanced after the ls-remote preflight is
    # rejected here, with the runtime checkout still exactly as we found it.
    git -C "$RUNTIME_DIR" fetch -q origin "$BRANCH"
    DEPLOY_TARGET_SHA="$(git -C "$RUNTIME_DIR" rev-parse FETCH_HEAD)"
    if [ -n "$EXPECT_SHA" ] && [ "$DEPLOY_TARGET_SHA" != "$EXPECT_SHA" ]; then
      die "fetched $BRANCH tip $DEPLOY_TARGET_SHA does not match --expect-sha $EXPECT_SHA — refusing before any runtime changes"
    fi
    git -C "$RUNTIME_DIR" checkout -q "$BRANCH"
    git -C "$RUNTIME_DIR" reset --hard -q "$DEPLOY_TARGET_SHA"
  fi
fi

if [ "$DRY_RUN" = 1 ] && [ ! -d "$RUNTIME_DIR/.git" ]; then
  log "DRY-RUN: fresh clone — remaining phases would run after it; stopping here"
  exit 0
fi

HEAD_SHA="$(git -C "$RUNTIME_DIR" rev-parse HEAD)"
if [ "$DRY_RUN" != 1 ]; then
  [ "$HEAD_SHA" = "$DEPLOY_TARGET_SHA" ] || die "HEAD $HEAD_SHA != verified target $DEPLOY_TARGET_SHA after checkout"
fi
# A dry run deliberately skipped the reset, so HEAD is still the OLD tip and
# comparing it to --expect-sha always fails whenever the runtime is behind —
# which is every routine redeploy, the one case a preview exists for. The pin
# was already checked for real against the remote in Phase 0, and the
# authoritative post-reset check is the one guarded above.
if [ "$DRY_RUN" != 1 ] && [ -n "$EXPECT_SHA" ] && [ "$HEAD_SHA" != "$EXPECT_SHA" ]; then
  die "deployed tip $HEAD_SHA does not match --expect-sha $EXPECT_SHA"
fi
if [ "$DRY_RUN" = 1 ] && [ -n "${DRY_RUN_TARGET_SHA:-}" ] && [ "$HEAD_SHA" != "$DRY_RUN_TARGET_SHA" ]; then
  log "DRY-RUN: runtime is at $HEAD_SHA; a real deploy would move it to $DRY_RUN_TARGET_SHA"
fi
log "runtime checkout at $HEAD_SHA ($RUNTIME_DIR)"

# --- Phase 2: runtime .picks state (preserve; seed only when absent) --------

if [ -d "$RUNTIME_DIR/.picks" ] && [ -n "$(ls -A "$RUNTIME_DIR/.picks" 2>/dev/null)" ]; then
  log "runtime .picks exists — preserved untouched"
elif [ -n "$SEED_PICKS_FROM" ]; then
  [ -d "$SEED_PICKS_FROM/.picks" ] || die "--seed-picks-from: $SEED_PICKS_FROM/.picks not found"
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN: would seed .picks from $SEED_PICKS_FROM/.picks"
  else
    log "seeding .picks from $SEED_PICKS_FROM/.picks (source left untouched)"
    cp -a "$SEED_PICKS_FROM/.picks" "$RUNTIME_DIR/.picks.seed-tmp-$TS"
    # rmdir only succeeds on an empty dir, so populated state can never be lost
    [ -d "$RUNTIME_DIR/.picks" ] && rmdir "$RUNTIME_DIR/.picks"
    mv "$RUNTIME_DIR/.picks.seed-tmp-$TS" "$RUNTIME_DIR/.picks"
  fi
else
  # resolve_root() in the gate scripts falls back to the developer checkout
  # when cwd has no .picks — exactly the failure this deploy exists to close.
  die "runtime has no .picks state and no --seed-picks-from was given"
fi

# Shared cron repoint helper. mode=check is read-only: it fails on any matched
# ENABLED job and writes nothing. mode=apply performs the backup + rewrite.
cron_repoint() {
  python3 - "$1" "$CRON_JOBS_FILE" "$REPOINT_CRON_FROM" "$RUNTIME_DIR" "$TS" <<'PYEOF'
import json, os, sys

mode, jobs_file, old_workdir, new_workdir, ts = sys.argv[1:6]
with open(jobs_file) as fh:
    data = json.load(fh)
jobs = data["jobs"] if isinstance(data, dict) and "jobs" in data else data

matched = [j for j in jobs if j.get("workdir") == old_workdir]
if not matched:
    print("no jobs matched workdir " + old_workdir)
    sys.exit(0)
enabled = [j for j in matched if j.get("enabled")]
if enabled:
    names = ", ".join(str(j.get("name")) for j in enabled)
    sys.exit("refusing to repoint ENABLED jobs (pause them first): " + names)
if mode == "check":
    print(f"{len(matched)} paused job(s) eligible for repoint")
    sys.exit(0)

backup = jobs_file + ".bak-deploy-" + ts
with open(backup, "w") as fh:
    json.dump(data, fh, indent=2)
for j in matched:
    j["workdir"] = new_workdir
tmp = jobs_file + ".tmp-" + ts
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2)
os.replace(tmp, jobs_file)
ids = ", ".join(str(j.get("id")) for j in matched)
print(f"repointed {len(matched)} paused job(s) [{ids}]; backup: {backup}")
PYEOF
}

# --- Phase 3a: cron repoint preflight (before any profile mutation) ---------
# An ENABLED matched job must refuse the whole deploy while the live profile
# directory is still untouched — not after new code is already installed.

CRON_SUMMARY="not requested"
if [ -n "$REPOINT_CRON_FROM" ]; then
  [ -f "$CRON_JOBS_FILE" ] || die "cron jobs file not found: $CRON_JOBS_FILE"
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN: would repoint cron workdirs $REPOINT_CRON_FROM -> $RUNTIME_DIR in $CRON_JOBS_FILE"
    CRON_SUMMARY="dry-run"
  else
    precheck="$(cron_repoint check)" || die "cron repoint preflight failed: $precheck"
    log "cron preflight: $precheck"
  fi
fi

# --- Phase 3b: profile-local script copies (staged, verified, atomic swap) --
# The complete set is staged and validated next to the live directory first;
# the live directory only changes in the final rename swap, and a failed swap
# restores the previous set. No failure path leaves a partial live install.

for f in "${PROFILE_MANIFEST[@]}"; do
  [ -f "$RUNTIME_DIR/scripts/$f" ] || die "manifest file missing from deployed checkout: scripts/$f"
  [ ! -L "$RUNTIME_DIR/scripts/$f" ] || die "manifest source is a symlink, not a regular file: scripts/$f"
done

CHECKSUM_LINES=""
if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN: would stage, verify, and atomically install ${#PROFILE_MANIFEST[@]} manifest files into $PROFILE_SCRIPTS_DIR"
else
  mkdir -p "$(dirname "$PROFILE_SCRIPTS_DIR")"
  STAGE="${PROFILE_SCRIPTS_DIR%/}.stage-$TS"
  rm -rf "$STAGE"
  trap 'rm -rf "$STAGE"' EXIT
  if [ -d "$PROFILE_SCRIPTS_DIR" ]; then
    # Start the stage from the live set so unmanaged files survive the swap.
    cp -a "$PROFILE_SCRIPTS_DIR" "$STAGE"
  else
    mkdir -p "$STAGE"
  fi
  for f in "${PROFILE_MANIFEST[@]}"; do
    src="$RUNTIME_DIR/scripts/$f"
    # cp -a above preserved the live set verbatim, symlinks included. Writing
    # through a managed symlink would follow it out of the staging directory
    # and clobber its target, so unlink the destination first: every managed
    # path must be a regular file created here, never a link we inherited.
    rm -f "$STAGE/$f"
    cp "$src" "$STAGE/$f"
    { [ -f "$STAGE/$f" ] && [ ! -L "$STAGE/$f" ]; } || die "staged $f is not a regular file"
    src_sum="$(sha256sum "$src" | cut -d' ' -f1)"
    dst_sum="$(sha256sum "$STAGE/$f" | cut -d' ' -f1)"
    [ "$src_sum" = "$dst_sum" ] || die "checksum mismatch after copy: $f ($src_sum != $dst_sum)"
    CHECKSUM_LINES="$CHECKSUM_LINES$src_sum  $f"$'\n'
  done
  for f in "${PROFILE_MANIFEST[@]}"; do
    python3 -m py_compile "$STAGE/$f" || die "py_compile failed for $f — live profile scripts left untouched"
  done
  rm -rf "$STAGE/__pycache__"
  log "staged and verified ${#PROFILE_MANIFEST[@]} manifest files (checksums + py_compile)"
  for existing in "$STAGE"/*.py; do
    name="$(basename "$existing")"
    managed=0
    for f in "${PROFILE_MANIFEST[@]}"; do [ "$f" = "$name" ] && managed=1 && break; done
    [ "$managed" = 1 ] || log "WARNING: unmanaged file in profile scripts dir: $name"
  done
  backup=""
  if [ -d "$PROFILE_SCRIPTS_DIR" ]; then
    backup="${PROFILE_SCRIPTS_DIR%/}.bak-$TS"
    mv "$PROFILE_SCRIPTS_DIR" "$backup"
    log "moved previous profile scripts to $backup"
  fi
  if ! mv "$STAGE" "$PROFILE_SCRIPTS_DIR"; then
    [ -n "$backup" ] && mv "$backup" "$PROFILE_SCRIPTS_DIR"
    die "failed to activate staged profile scripts — previous set restored"
  fi
  for f in "${PROFILE_MANIFEST[@]}"; do
    dst="$PROFILE_SCRIPTS_DIR/$f"
    { [ -f "$dst" ] && [ ! -L "$dst" ]; } || die "installed $f is not a regular file"
    dst_sum="$(sha256sum "$dst" | cut -d' ' -f1)"
    src_sum="$(sha256sum "$RUNTIME_DIR/scripts/$f" | cut -d' ' -f1)"
    [ "$src_sum" = "$dst_sum" ] || die "checksum mismatch after install: $f ($src_sum != $dst_sum)"
  done
  log "installed ${#PROFILE_MANIFEST[@]} checksum-verified profile script copies"
fi

# --- Phase 4 (opt-in): repoint paused cron workdirs to the runtime dir ------

if [ -n "$REPOINT_CRON_FROM" ] && [ "$DRY_RUN" != 1 ]; then
  CRON_SUMMARY="$(cron_repoint apply)" || die "cron repoint failed: $CRON_SUMMARY"
  log "cron: $CRON_SUMMARY"
fi

# --- Order-executor venv check (read-only, warns, never fails) ---------------
#
# The deploy installs no venv and no packages by design — it should not need
# network at deploy time, and .venv/ is gitignored so an existing one is never
# touched. But polymarket_us_sdk_bet.py re-execs into $RUNTIME_DIR/.venv, and
# that re-exec is guarded by a path-exists test, so a runtime dir without a venv
# takes the SILENT path: no output here, and the failure surfaces much later at
# order time. A fresh runtime dir is exactly that case.
#
# Warn rather than fail: the review and settlement lanes do not need this venv,
# and refusing the whole deploy over the order lane would be the
# outage-becomes-terminal shape this repo keeps removing.
# The path is ASKED OF THE EXECUTOR'S OWN PROLOGUE, never rebuilt here. Rebuilding
# it made the check and the executor two independent computations of one path, so
# they could disagree and only the check got to speak: a deploy with a non-default
# --runtime-dir printed "order-executor venv ok" for a venv the executor never
# consults. Before that check existed the divergence was silent; reporting it
# affirmatively healthy is worse (Reviewer, PR #59).
#
# Resolved the way CRON will resolve it: cwd is the runtime checkout, because
# that is what the repoint writes as workdir, and EVERY SPORTS_PICKS_* variable
# is cleared because they belong to the deploy shell and a cron job does not
# inherit it.
#
# All three, not two. An earlier version cleared the two directory knobs and
# honoured SPORTS_PICKS_VENV_PYTHON, claiming that could only make the check
# stricter. It could not: with that variable exported at a working interpreter,
# the deploy printed "order-executor venv ok" about it while the executor
# resolved somewhere else entirely — the same false green, one exported variable
# instead of one flag, and it is the variable the skip-reason message tells the
# operator to set. The asymmetry was the bug: either cron inherits this shell, in
# which case clearing the directory knobs is wrong, or it does not, in which case
# honouring the interpreter knob is. Both cannot hold (Reviewer, PR #59).
#
# Consequence, stated rather than hidden: a SPORTS_PICKS_VENV_PYTHON set in the
# CRON JOB's own environment is invisible here, so this check can warn about a
# venv that job would never have used. Warning too loudly is the safe direction;
# the reverse is what got blocked twice.
EXEC_REQS="skills/sports-picks/scripts/requirements-exec.txt"
EXEC_SRC="$RUNTIME_DIR/skills/sports-picks/scripts/polymarket_us_sdk_bet.py"
EXEC_RESOLVER="$RUNTIME_DIR/scripts/resolve_exec_venv.py"
EXEC_VENV_PY=""
if [ -f "$EXEC_SRC" ] && [ -f "$EXEC_RESOLVER" ]; then
  EXEC_VENV_PY="$(cd "$RUNTIME_DIR" && \
    env -u SPORTS_PICKS_RUNTIME_DIR -u SPORTS_PICKS_ROOT -u SPORTS_PICKS_VENV_PYTHON \
    python3 "$EXEC_RESOLVER" "$EXEC_SRC" 2>/dev/null)" || EXEC_VENV_PY=""
fi
if [ -z "$EXEC_VENV_PY" ]; then
  log "WARNING: could not ask $EXEC_SRC which interpreter it will re-exec into —"
  log "WARNING:   skipping the order-executor venv check rather than guessing the path."
elif [ ! -x "$EXEC_VENV_PY" ]; then
  # The venv dir comes from the resolved interpreter, so the remedy names the
  # place the executor will actually look rather than a second guess at it.
  EXEC_VENV_DIR="$(dirname "$(dirname "$EXEC_VENV_PY")")"
  log "WARNING: no order-executor venv at $EXEC_VENV_PY — polymarket_us_sdk_bet.py"
  log "WARNING:   will skip its self-heal re-exec and fail at order time. Create it:"
  log "WARNING:   python3 -m venv $EXEC_VENV_DIR && $EXEC_VENV_PY -m pip install -r $RUNTIME_DIR/$EXEC_REQS"
elif ! "$EXEC_VENV_PY" -c "import polymarket_us" >/dev/null 2>&1; then
  log "WARNING: order-executor venv $EXEC_VENV_PY cannot import polymarket_us. Install it:"
  log "WARNING:   $EXEC_VENV_PY -m pip install -r $RUNTIME_DIR/$EXEC_REQS"
else
  log "order-executor venv ok: $EXEC_VENV_PY imports polymarket_us"
fi

# --- Receipt ----------------------------------------------------------------

if [ "$DRY_RUN" = 1 ]; then
  log "DRY-RUN complete — no changes were made"
  exit 0
fi

RECEIPT="$RUNTIME_DIR/.deploy/receipt-$TS.txt"
mkdir -p "$(dirname "$RECEIPT")"
{
  echo "deploy-runtime receipt $TS"
  echo "runtime_dir: $RUNTIME_DIR"
  echo "deployed_sha: $HEAD_SHA"
  echo "profile_scripts_dir: $PROFILE_SCRIPTS_DIR"
  echo "cron: $CRON_SUMMARY"
  echo "sha256 (runtime checkout == profile copy):"
  printf '%s' "$CHECKSUM_LINES"
} > "$RECEIPT"
log "receipt written: $RECEIPT"
cat "$RECEIPT"
