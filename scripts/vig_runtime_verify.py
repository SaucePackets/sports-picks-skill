#!/usr/bin/env python3
"""Read-only check that the cron jobs and the deployed runtime agree.

``deploy-runtime.sh`` verifies the runtime at deploy time and then stops
looking. Everything that goes wrong afterwards is invisible: a cron job whose
``workdir`` was never repointed executes a developer checkout's scripts while
every status still reads healthy (the two reporting jobs sat paused with
``workdir: null`` for days, which would have reported from a stale checkout);
and the runtime checkout itself falls behind ``main`` — it was eight merges
behind on 2026-08-23 with nothing on the box saying so.

This verifies six things against the artifacts that define them, never
against a reconstruction of them:

1. The runtime is the deploy-managed one — ``.deploy/runtime.marker``, the
   same file ``deploy-runtime.sh`` writes and the executor's resolution rung
   keys on. Not "has a ``.picks/``", which is a side effect a developer
   checkout also has.
2. Every ENABLED cron job's ``workdir`` is that runtime directory. A paused
   job is reported as informational, since a paused job runs nothing — but a
   paused job with a null or foreign workdir is reported too, because
   resuming it is one command and the divergence arrives with it.
3. The runtime's checked-out commit, optionally pinned with ``--expect-sha``.
4. The runtime working tree is clean, since ``deploy-runtime.sh`` hard-resets
   and a local modification is both a deploy blocker and unreviewed code
   running in production.
5. Every cron job declares its OWNER — the profile the jobs file belongs to,
   and an origin. Check 2 asks where a job runs and nothing asked who owns
   it, so five of nine live jobs carried ``profile: null`` on 2026-09-03 with
   nothing on the box saying so.
6. The deployed profile script copies are the manifest, byte for byte, and
   nothing else that shadows a repo script. ``deploy-runtime.sh`` seeds each
   stage from the live directory so unmanaged files survive every deploy
   forever: a ``mlb_slate_receipt.py`` copied in by hand on 2026-09-01 was
   still there, still missing a validator the runtime copy had gained, and
   the deploy could not see it because it is not in the manifest.

Exits 1 on any finding, 0 when clean. Writes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

MARKER_REL = ".deploy/runtime.marker"
DEPLOY_SCRIPT_REL = "scripts/deploy-runtime.sh"

DEFAULT_RUNTIME_DIR = Path(
    os.environ.get("SPORTS_PICKS_RUNTIME_DIR") or Path.home() / "projects" / "sports-picks-runtime"
)
DEFAULT_CRON_JOBS = Path(
    os.environ.get("VIG_CRON_JOBS_FILE") or Path.home() / ".hermes/profiles/vig/cron/jobs.json"
)
DEFAULT_PROFILE_SCRIPTS = Path(
    os.environ.get("VIG_PROFILE_SCRIPTS_DIR") or Path.home() / ".hermes/profiles/vig/scripts"
)

LEVEL_FAIL = "FAIL"
LEVEL_WARN = "WARN"
LEVEL_OK = "OK"


def finding(level: str, check: str, message: str) -> dict[str, str]:
    return {"level": level, "check": check, "message": message}


def is_deploy_managed(runtime_dir: Path) -> bool:
    """The predicate, named once and used everywhere.

    Deploy-managed means ``deploy-runtime.sh`` created this checkout and may
    hard-reset it — which is exactly the marker file it writes. It does NOT
    mean "has a ``.picks/``": that is *has pick state*, which a developer
    checkout also has, and keying on it is the regression that cost PR #59 a
    review round. Kept as its own boolean so a test can assert the
    distinction directly rather than inferring it from a finding that a
    broader predicate could still produce for another reason.
    """
    return (runtime_dir / MARKER_REL).is_file()


def marker_findings(runtime_dir: Path) -> list[dict[str, str]]:
    marker = runtime_dir / MARKER_REL
    if not runtime_dir.is_dir():
        return [finding(LEVEL_FAIL, "runtime-dir", f"runtime directory does not exist: {runtime_dir}")]
    if not is_deploy_managed(runtime_dir):
        return [
            finding(
                LEVEL_FAIL, "runtime-marker",
                f"{marker} is missing: this is not a checkout deploy-runtime.sh created, "
                "so cron may be running an unmanaged tree",
            )
        ]
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [finding(LEVEL_FAIL, "runtime-marker", f"unreadable {marker}: {exc}")]
    return [finding(LEVEL_OK, "runtime-marker", f"deploy-managed runtime: {text}")]


def load_jobs(jobs_file: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], f"cron jobs file not found: {jobs_file}"
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"unreadable cron jobs file {jobs_file}: {exc}"
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return [], f"cron jobs file has no jobs list: {jobs_file}"
    return [job for job in jobs if isinstance(job, dict)], None


def job_label(job: dict[str, Any]) -> str:
    return f"{job.get('id') or '?'} ({job.get('name') or 'unnamed'})"


def cron_findings(jobs: Iterable[dict[str, Any]], runtime_dir: Path) -> list[dict[str, str]]:
    """Every job whose workdir is not the deploy-managed runtime.

    ``workdir`` is compared after resolution so a symlinked path is not
    reported as divergence, and a null workdir is called out by name: it is
    not "unset and harmless", it makes resolve_root() fall back to the
    developer checkout.
    """
    target = runtime_dir.resolve() if runtime_dir.exists() else runtime_dir
    findings: list[dict[str, str]] = []
    aligned = 0
    for job in jobs:
        workdir = job.get("workdir")
        enabled = job.get("enabled") is True
        level = LEVEL_FAIL if enabled else LEVEL_WARN
        state = "enabled" if enabled else f"not enabled (state={job.get('state') or '?'})"
        if workdir is None:
            findings.append(
                finding(
                    level, "cron-workdir",
                    f"{job_label(job)} is {state} with workdir: null — resolve_root() falls "
                    f"back to the developer checkout, not {target}",
                )
            )
            continue
        resolved = Path(str(workdir)).expanduser()
        resolved = resolved.resolve() if resolved.exists() else resolved
        if resolved != target:
            findings.append(
                finding(
                    level, "cron-workdir",
                    f"{job_label(job)} is {state} with workdir {workdir} — expected {target}",
                )
            )
            continue
        aligned += 1
        if job.get("failure_streak"):
            findings.append(
                finding(
                    LEVEL_WARN, "cron-health",
                    f"{job_label(job)} has failure_streak={job['failure_streak']} "
                    f"(last_status={job.get('last_status') or '?'})",
                )
            )
    findings.append(
        finding(LEVEL_OK, "cron-workdir", f"{aligned} job(s) point at {target}")
    )
    return findings


def profile_name_for(jobs_file: Path) -> str | None:
    """The profile a jobs file belongs to, read off the path that defines it.

    ``~/.hermes/profiles/vig/cron/jobs.json`` IS the statement that these are
    Vig's jobs; the expected owner is therefore derivable and must never be a
    constant in this file. A path that does not name a profile yields None and
    the ownership check reports that it could not run, rather than passing.
    """
    parts = jobs_file.expanduser().resolve().parts
    if "profiles" in parts:
        index = parts.index("profiles")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def ownership_findings(
    jobs: Iterable[dict[str, Any]], jobs_file: Path
) -> list[dict[str, str]]:
    """Every job must say which profile owns it, and where it came from.

    ``cron_findings`` asks where a job RUNS. Nothing asked who owns it, and the
    two are independent: on 2026-09-03 every job pointed at the deploy-managed
    runtime and five of nine — including the MLB evening slate that writes the
    same schedule the morning job writes — carried ``profile: null``. A job
    with no profile does not inherit the profile's environment, and the one
    check that looked could not see the difference.

    ``origin`` is a WARN, not a FAIL: it records who scheduled the job and its
    absence is a lost audit trail, not a runtime divergence.
    """
    expected = profile_name_for(jobs_file)
    if expected is None:
        return [
            finding(
                LEVEL_WARN, "cron-owner",
                f"{jobs_file} is not under a .../profiles/<name>/ path, so the owning "
                "profile cannot be derived and job ownership was not checked",
            )
        ]
    findings: list[dict[str, str]] = []
    owned = 0
    for job in jobs:
        profile = job.get("profile")
        enabled = job.get("enabled") is True
        state = "enabled" if enabled else f"not enabled (state={job.get('state') or '?'})"
        if profile != expected:
            findings.append(
                finding(
                    LEVEL_FAIL if enabled else LEVEL_WARN, "cron-owner",
                    f"{job_label(job)} is {state} with profile {profile!r} in "
                    f"{expected}'s jobs file — an unowned job does not run under the "
                    f"{expected} profile's environment",
                )
            )
        else:
            owned += 1
        if not job.get("origin"):
            findings.append(
                finding(
                    LEVEL_WARN, "cron-origin",
                    f"{job_label(job)} has no origin — nothing records who scheduled it",
                )
            )
    findings.append(
        finding(LEVEL_OK, "cron-owner", f"{owned} job(s) declare profile {expected!r}")
    )
    return findings


MANIFEST_BLOCK = re.compile(r"PROFILE_MANIFEST=\((.*?)\)", re.DOTALL)


def load_profile_manifest(deploy_script: Path) -> tuple[list[str], str | None]:
    """The manifest, parsed out of the deploy script that defines it.

    Restating this list in Python would be the same defect three review rounds
    in this repo have already paid for: two copies of one rule agree only until
    one of them changes. The shell array is the definition, so it is the thing
    that gets read.
    """
    try:
        text = deploy_script.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"unreadable deploy script {deploy_script}: {exc}"
    match = MANIFEST_BLOCK.search(text)
    if not match:
        return [], f"{deploy_script} has no PROFILE_MANIFEST=( ... ) block"
    # A comment inside the array is not an entry. The shell ignores it, and a
    # reader that does not would turn a commented-out `# foo.py` — or the
    # rationale comment this change itself added above `mlb_slate_receipt.py` —
    # into a manifest file, then FAIL for its absence from the profile dir.
    names = [
        stripped
        for stripped in (line.strip() for line in match.group(1).splitlines())
        if stripped.endswith(".py") and not stripped.startswith("#")
    ]
    if not names:
        return [], f"{deploy_script} PROFILE_MANIFEST is empty"
    return names, None


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def profile_script_findings(
    runtime_dir: Path, profile_scripts: Path
) -> list[dict[str, str]]:
    """The deployed profile copies are the manifest, and nothing that shadows it.

    Two failures, both invisible to ``deploy-runtime.sh`` after it finishes.
    A manifest file whose profile copy has drifted from the runtime checkout is
    a cron running code nobody reviewed at that commit. And an UNMANAGED copy
    of a repo script — a file the manifest does not name — is worse: the
    installer seeds each stage from the live directory precisely so unmanaged
    files survive, so a hand-copied module is pinned to the day it was copied
    and every future deploy steps around it. ``mlb_slate_receipt.py`` sat that
    way from 2026-09-01, missing the ``policy_disposition_errors`` rail its
    runtime original had, ready to call a slate clean that the gate refused.
    """
    if not profile_scripts.is_dir():
        return [
            finding(
                LEVEL_WARN, "profile-scripts",
                f"profile scripts directory does not exist: {profile_scripts}",
            )
        ]
    repo_scripts = runtime_dir / "scripts"
    manifest, error = load_profile_manifest(runtime_dir / DEPLOY_SCRIPT_REL)
    if error:
        return [finding(LEVEL_FAIL, "profile-manifest", error)]

    findings: list[dict[str, str]] = []
    matched = 0
    for name in manifest:
        deployed = profile_scripts / name
        source = repo_scripts / name
        if not deployed.is_file():
            findings.append(
                finding(
                    LEVEL_FAIL, "profile-scripts",
                    f"{name} is in PROFILE_MANIFEST but missing from {profile_scripts}; "
                    "any cron importing it dies at import time",
                )
            )
            continue
        deployed_sha, source_sha = _sha256(deployed), _sha256(source)
        if source_sha is None:
            findings.append(
                finding(
                    LEVEL_FAIL, "profile-scripts",
                    f"{name} is deployed but unreadable in the runtime checkout "
                    f"({source})",
                )
            )
        elif deployed_sha != source_sha:
            findings.append(
                finding(
                    LEVEL_FAIL, "profile-scripts",
                    f"{name} differs between {profile_scripts} and the runtime "
                    f"checkout ({deployed_sha[:12] if deployed_sha else '?'} vs "
                    f"{source_sha[:12]}): cron is running code that is not the "
                    "deployed commit",
                )
            )
        else:
            matched += 1

    managed = set(manifest)
    for path in sorted(profile_scripts.glob("*.py")):
        if path.name in managed:
            continue
        level, note = LEVEL_WARN, "it is not a script this repository ships"
        if (repo_scripts / path.name).is_file():
            level = LEVEL_FAIL
            note = (
                "it shadows a repo script of the same name, so the deploy never "
                "updates it and cron runs the copy frozen at the day it was made"
            )
        findings.append(
            finding(
                level, "profile-unmanaged",
                f"{path.name} sits in {profile_scripts} outside PROFILE_MANIFEST — {note}",
            )
        )
    findings.append(
        finding(
            LEVEL_OK, "profile-scripts",
            f"{matched}/{len(manifest)} manifest file(s) match the runtime checkout",
        )
    )
    return findings


def _git(runtime_dir: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(runtime_dir), *args],
            text=True, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"git {' '.join(args)} failed: {exc}"
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def checkout_findings(runtime_dir: Path, expect_sha: str | None) -> list[dict[str, str]]:
    if not (runtime_dir / ".git").exists():
        return [finding(LEVEL_FAIL, "runtime-head", f"not a git checkout: {runtime_dir}")]
    findings: list[dict[str, str]] = []
    code, head = _git(runtime_dir, "rev-parse", "HEAD")
    if code:
        return [finding(LEVEL_FAIL, "runtime-head", head)]
    if expect_sha and not head.startswith(expect_sha) and not expect_sha.startswith(head):
        findings.append(
            finding(
                LEVEL_FAIL, "runtime-head",
                f"runtime is at {head} but --expect-sha is {expect_sha}: the deployed code "
                "is not the commit you think it is",
            )
        )
    else:
        findings.append(finding(LEVEL_OK, "runtime-head", f"runtime HEAD {head}"))
    code, status = _git(runtime_dir, "status", "--porcelain")
    if code:
        findings.append(finding(LEVEL_FAIL, "runtime-clean", status))
    elif status:
        changed = "; ".join(status.splitlines()[:10])
        findings.append(
            finding(
                LEVEL_FAIL, "runtime-clean",
                f"runtime working tree is dirty — unreviewed code is running and the next "
                f"deploy will refuse or overwrite it: {changed}",
            )
        )
    else:
        findings.append(finding(LEVEL_OK, "runtime-clean", "runtime working tree is clean"))
    return findings


def verify(
    runtime_dir: Path,
    jobs_file: Path,
    expect_sha: str | None = None,
    profile_scripts: Path | None = None,
) -> list[dict[str, str]]:
    findings = marker_findings(runtime_dir)
    findings.extend(checkout_findings(runtime_dir, expect_sha))
    jobs, error = load_jobs(jobs_file)
    if error:
        findings.append(finding(LEVEL_FAIL, "cron-jobs", error))
    else:
        findings.extend(cron_findings(jobs, runtime_dir))
        findings.extend(ownership_findings(jobs, jobs_file))
    findings.extend(
        profile_script_findings(
            runtime_dir, profile_scripts or DEFAULT_PROFILE_SCRIPTS
        )
    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only check that Vig's cron jobs and deployed runtime have not diverged."
    )
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--cron-jobs", type=Path, default=DEFAULT_CRON_JOBS)
    parser.add_argument("--profile-scripts", type=Path, default=DEFAULT_PROFILE_SCRIPTS,
                        help="deployed Vig profile scripts directory")
    parser.add_argument("--expect-sha", default=None,
                        help="require the runtime checkout to be at this commit")
    parser.add_argument("--strict", action="store_true",
                        help="treat WARN findings as failures too")
    args = parser.parse_args(argv)

    findings = verify(
        args.runtime_dir, args.cron_jobs, args.expect_sha, args.profile_scripts
    )
    for item in findings:
        print(f"[{item['level']}] {item['check']}: {item['message']}")
    failed = sum(1 for item in findings if item["level"] == LEVEL_FAIL)
    warned = sum(1 for item in findings if item["level"] == LEVEL_WARN)
    if failed or (args.strict and warned):
        print(f"RUNTIME VERIFY FAILED: {failed} failure(s), {warned} warning(s)")
        return 1
    print(f"RUNTIME VERIFY OK: 0 failures, {warned} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
