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
  mlb_execution_gate.py
  mlb_final_scores.py
  mlb_lineup_watchlist.py
  mlb_postgame_evidence.py
  mlb_probability_model.py
  mlb_runtime_policy.py
  mlb_stage2_scan.py
  receipts_ledger_reconcile.py
  vig_calibration_report.py
  vig_mlb_review_gate.py
  vig_postgame_gate.py
  vig_review_gate_common.py
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

# --- Phase 0: --expect-sha preflight (read-only, before any mutation) -------
# Resolve the remote tip via ls-remote so a mismatch is rejected before the
# clone, fetch, checkout, or reset ever runs — a refused deploy must leave the
# runtime checkout exactly as it found it.

if [ -n "$EXPECT_SHA" ]; then
  REMOTE_SHA="$(git ls-remote "$REPO_URL" "refs/heads/$BRANCH" | cut -f1)"
  [ -n "$REMOTE_SHA" ] || die "cannot resolve refs/heads/$BRANCH on $REPO_URL"
  case "$REMOTE_SHA" in
    "$EXPECT_SHA"*) log "preflight: origin/$BRANCH is $REMOTE_SHA (matches --expect-sha)" ;;
    *) die "origin/$BRANCH tip $REMOTE_SHA does not match --expect-sha $EXPECT_SHA — refusing before any runtime changes" ;;
  esac
fi

# --- Phase 1: runtime checkout at clean origin/main -------------------------

if [ ! -d "$RUNTIME_DIR/.git" ]; then
  if [ -e "$RUNTIME_DIR" ] && [ -n "$(ls -A "$RUNTIME_DIR" 2>/dev/null)" ]; then
    die "$RUNTIME_DIR exists, is not empty, and is not a git checkout — refusing"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN: would clone $REPO_URL into $RUNTIME_DIR (branch $BRANCH)"
  else
    log "cloning $REPO_URL into $RUNTIME_DIR"
    git clone --branch "$BRANCH" "$REPO_URL" "$RUNTIME_DIR"
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
  else
    git -C "$RUNTIME_DIR" fetch origin "$BRANCH"
    git -C "$RUNTIME_DIR" checkout -q "$BRANCH"
    git -C "$RUNTIME_DIR" reset --hard -q "origin/$BRANCH"
  fi
fi

if [ "$DRY_RUN" = 1 ] && [ ! -d "$RUNTIME_DIR/.git" ]; then
  log "DRY-RUN: fresh clone — remaining phases would run after it; stopping here"
  exit 0
fi

HEAD_SHA="$(git -C "$RUNTIME_DIR" rev-parse HEAD)"
if [ "$DRY_RUN" != 1 ]; then
  MAIN_SHA="$(git -C "$RUNTIME_DIR" rev-parse "origin/$BRANCH")"
  [ "$HEAD_SHA" = "$MAIN_SHA" ] || die "HEAD $HEAD_SHA != origin/$BRANCH $MAIN_SHA after reset"
fi
if [ -n "$EXPECT_SHA" ]; then
  case "$HEAD_SHA" in
    "$EXPECT_SHA"*) ;;
    *) die "deployed tip $HEAD_SHA does not match --expect-sha $EXPECT_SHA" ;;
  esac
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
    cp "$src" "$STAGE/$f"
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
  log "installed ${#PROFILE_MANIFEST[@]} checksum-verified profile script copies"
fi

# --- Phase 4 (opt-in): repoint paused cron workdirs to the runtime dir ------

if [ -n "$REPOINT_CRON_FROM" ] && [ "$DRY_RUN" != 1 ]; then
  CRON_SUMMARY="$(cron_repoint apply)" || die "cron repoint failed: $CRON_SUMMARY"
  log "cron: $CRON_SUMMARY"
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
