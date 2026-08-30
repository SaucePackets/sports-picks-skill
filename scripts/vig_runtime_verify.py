#!/usr/bin/env python3
"""Read-only check that the cron jobs and the deployed runtime agree.

``deploy-runtime.sh`` verifies the runtime at deploy time and then stops
looking. Everything that goes wrong afterwards is invisible: a cron job whose
``workdir`` was never repointed executes a developer checkout's scripts while
every status still reads healthy (the two reporting jobs sat paused with
``workdir: null`` for days, which would have reported from a stale checkout);
and the runtime checkout itself falls behind ``main`` — it was eight merges
behind on 2026-08-23 with nothing on the box saying so.

This verifies four things against the artifacts that define them, never
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

Exits 1 on any finding, 0 when clean. Writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable

MARKER_REL = ".deploy/runtime.marker"

DEFAULT_RUNTIME_DIR = Path(
    os.environ.get("SPORTS_PICKS_RUNTIME_DIR") or Path.home() / "projects" / "sports-picks-runtime"
)
DEFAULT_CRON_JOBS = Path(
    os.environ.get("VIG_CRON_JOBS_FILE") or Path.home() / ".hermes/profiles/vig/cron/jobs.json"
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
    runtime_dir: Path, jobs_file: Path, expect_sha: str | None = None
) -> list[dict[str, str]]:
    findings = marker_findings(runtime_dir)
    findings.extend(checkout_findings(runtime_dir, expect_sha))
    jobs, error = load_jobs(jobs_file)
    if error:
        findings.append(finding(LEVEL_FAIL, "cron-jobs", error))
    else:
        findings.extend(cron_findings(jobs, runtime_dir))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only check that Vig's cron jobs and deployed runtime have not diverged."
    )
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--cron-jobs", type=Path, default=DEFAULT_CRON_JOBS)
    parser.add_argument("--expect-sha", default=None,
                        help="require the runtime checkout to be at this commit")
    parser.add_argument("--strict", action="store_true",
                        help="treat WARN findings as failures too")
    args = parser.parse_args(argv)

    findings = verify(args.runtime_dir, args.cron_jobs, args.expect_sha)
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
