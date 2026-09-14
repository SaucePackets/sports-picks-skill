# Script provenance — the canonical source and its derived copies

The same sports-picks scripts can exist in several places across two machines.
Only one is a source; the rest are copies produced by a defined mechanism.
Before this document there was no written rule about which was which, so a
developer checkout parked on a feature branch could look as authoritative as
the tree cron actually executes.

## The decision

> **`scripts/` on `origin/main` of `SaucePackets/sports-picks-skill` is the
> single canonical source of every sports-picks script. Every other copy is
> derived. A change reaches a copy only by landing on `main` first and then
> running the copy's documented derivation mechanism.**

Consequences that follow from it, and that the checker enforces:

- No copy may be edited in place. An edit to a derived copy is drift, not a
  change — the next derivation silently discards it.
- A copy is correct only when it is byte-identical to canonical. There is no
  "close enough" and no per-file exception list.
- `main` is the reference, not any working tree. Run the checker with
  `--ref origin/main` (after a `git fetch`) whenever the answer must be about
  the canonical source rather than about local work in progress.

## The seven copies

| # | Copy | Machine | Derivation | Allowed to differ? |
|---|------|---------|------------|--------------------|
| 1 | `~/.buzz/REPOS/sports-picks-skill/scripts` | Mac | `git pull` on `main` | No — must equal `origin/main` when on `main` |
| 2–3 | `~/.buzz/REPOS/sports-picks-skill-<feature>/scripts` | Mac | feature worktrees | **Yes**, by design — this is where changes are authored |
| 4 | `~/projects/sports-picks-runtime/scripts` | VPS | `scripts/deploy-runtime.sh` hard-resets to `origin/main` | No |
| 5 | `~/.hermes/profiles/vig/scripts` | VPS | `deploy-runtime.sh` installs the `PROFILE_MANIFEST` subset, checksum-verified | No, for manifest files |
| 6 | `~/projects/sports-picks-skill/scripts` | VPS | manual `git` — the legacy developer checkout | No, but **currently drifted** (see below) |
| 7 | `skills/sports-picks/scripts/http_util.py` | in-repo | vendored copy of `scripts/http_util.py` | No — must be byte-identical |

Copies 2–3 are the only ones permitted to differ, because a feature worktree is
where canonical content is *authored*; it becomes canonical when its PR merges.
Everything else is downstream of `main`.

### Copy 5 — the manifest subset

The profile-local directory holds only the files listed in `PROFILE_MANIFEST`
in `scripts/deploy-runtime.sh`: the cron entrypoints plus the siblings they
import. Files outside that list are *unmanaged* — the deploy warns about them
and never deletes them. On 2026-09-14, `test_vig_review_gate.py` was archived outside the profile import
directory after checking cron and profile callers. The remaining unmanaged files
were `mlb_producer_prompt_contract.py` (an operational migration copy) and
`nfl_actionable_scan.py` (an active NFL adapter). The checker reports unmanaged
files separately from drift; do not delete an active adapter just to make that
list empty.

### Copy 6 — legacy developer checkout

As observed on 2026-09-14, `~/projects/sports-picks-skill` was clean `main` at
`ad9b8d9`, while canonical/runtime main was `c0f22d7`. It is a developer copy,
not the deployed cron root. The older observation that it was parked on
`fix/vig-review-transition-schema` is no longer current.

All nine inspected Vig jobs now have the dedicated runtime workdir. Monthly
Calibration and Weekly Discipline are enabled there; the old null-workdir/paused
observation no longer applies. The execution poller remains disabled separately.
`resolve_root()` still has a legacy-directory fallback, so a future missing
workdir or runtime `.picks` directory remains a configuration risk. Do not treat
these dated observations as a permanent exception to provenance checks.
See [the pipeline audit](pipeline-audit-2026-09-14.md) for the tested deployment.

## Checking provenance

`scripts/check_script_provenance.py` is read-only: it hashes and compares, and
never writes, repairs, or touches runtime state. Stdlib only, no dependencies.

```bash
# Repo-internal invariants only (manifest resolves, vendored copy identical)
python3 scripts/check_script_provenance.py --ref origin/main

# Add any derived copy you can reach; repeat --copy freely
python3 scripts/check_script_provenance.py --ref origin/main \
  --copy 'mac-clone:full=~/.buzz/REPOS/sports-picks-skill/scripts' \
  --copy 'runtime:full=~/projects/sports-picks-runtime/scripts' \
  --copy 'vig-profile:manifest=~/.hermes/profiles/vig/scripts' \
  --copy 'vps-developer:full=~/projects/sports-picks-skill/scripts'
```

- `full` — the copy must be the canonical `scripts/` tree and nothing else.
  Use for whole-checkout copies (1, 4, 6). An extra file is `unexpected` and
  counts as drift: the runtime checkout is derived by `git reset --hard` with
  no `git clean`, so a script deleted from `main` lingers on disk and stays
  importable by the cron entrypoints. "Byte-identical, no exceptions" has to
  mean the file inventory too, or the rule above is unenforced for exactly the
  tree cron executes.
- `manifest` — only the `PROFILE_MANIFEST` subset must match. Use for copy 5.
  An extra file there is `unmanaged` and informational, because
  `deploy-runtime.sh` deliberately preserves files outside the manifest;
  `--strict` promotes those to drift.
- `--json` emits the same report for scripting. Exit codes: `0` clean, `1`
  drift, `2` usage or I/O error.

The manifest is parsed out of `deploy-runtime.sh` itself rather than restated,
so there is no second list free to drift from the one that actually ships
files. Run the checker from a Mac checkout against local copies, and over SSH
on the VPS for copies 4–6; it needs nothing but Python 3 and `git`.

Covered by `tests/test_script_provenance.py`.
