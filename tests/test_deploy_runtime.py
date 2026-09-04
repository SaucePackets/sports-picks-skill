"""End-to-end tests for scripts/deploy-runtime.sh against a local fixture origin.

The fixture origin is a real git repo built from this repo's own scripts/, so the
profile manifest baked into the deploy script is exercised against the actual
file inventory it will deploy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import import_closure

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "scripts" / "deploy-runtime.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    """A local 'origin' repo containing the real scripts/ tree on main."""
    src = tmp_path / "origin-src"
    (src / "scripts").mkdir(parents=True)
    for f in (REPO_ROOT / "scripts").iterdir():
        if f.is_file():
            shutil.copy(f, src / "scripts" / f.name)
    # The order executor and its requirements ship in the runtime checkout too,
    # and the deploy's venv check asks that file which interpreter it resolves —
    # so a fixture origin without it cannot exercise the check at all.
    exec_rel = Path("skills") / "sports-picks" / "scripts"
    (src / exec_rel).mkdir(parents=True)
    for name in ("polymarket_us_sdk_bet.py", "requirements-exec.txt"):
        shutil.copy(REPO_ROOT / exec_rel / name, src / exec_rel / name)
    # The real .gitignore matters: the clean-tree guard counts untracked files
    # as dirt, and only gitignore keeps runtime .picks/.deploy state excluded.
    shutil.copy(REPO_ROOT / ".gitignore", src / ".gitignore")
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "fixture main")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(bare))
    return bare


def _run(origin: Path, tmp_path: Path, *extra: str, check: bool = True,
         seed: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    args = [
        "bash", str(DEPLOY),
        "--repo-url", str(origin),
        "--runtime-dir", str(tmp_path / "runtime"),
        "--profile-scripts", str(tmp_path / "profile" / "scripts"),
        "--cron-jobs", str(tmp_path / "cron" / "jobs.json"),
    ]
    if seed:
        seed_dir = tmp_path / "seed"
        (seed_dir / ".picks").mkdir(parents=True, exist_ok=True)
        (seed_dir / ".picks" / "INDEX.md").write_text("seeded\n")
        args += ["--seed-picks-from", str(seed_dir)]
    args += list(extra)
    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise AssertionError(f"deploy failed:\n{proc.stdout}\n{proc.stderr}")
    return proc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fresh_deploy_installs_verified_profile_copies(origin, tmp_path):
    _run(origin, tmp_path)
    runtime = tmp_path / "runtime"
    origin_main = _git(runtime, "rev-parse", "origin/main")
    assert _git(runtime, "rev-parse", "HEAD") == origin_main
    assert (runtime / ".deploy" / "runtime.marker").is_file()
    assert (runtime / ".picks" / "INDEX.md").read_text() == "seeded\n"
    profile = tmp_path / "profile" / "scripts"
    for name in ("vig_mlb_review_gate.py", "vig_review_gate_common.py",
                 "mlb_execution_gate.py", "vig_postgame_gate.py"):
        assert _sha256(profile / name) == _sha256(runtime / "scripts" / name), name
    receipts = list((runtime / ".deploy").glob("receipt-*.txt"))
    assert len(receipts) == 1
    body = receipts[0].read_text()
    assert origin_main in body
    assert "vig_review_gate_common.py" in body


def test_expect_sha_mismatch_aborts_before_profile_install(origin, tmp_path):
    proc = _run(origin, tmp_path, "--expect-sha", "0" * 40, check=False)
    assert proc.returncode != 0
    assert "does not match --expect-sha" in proc.stderr
    assert not (tmp_path / "profile" / "scripts").exists()


def test_refuses_checkout_without_runtime_marker(origin, tmp_path):
    foreign = tmp_path / "runtime"
    subprocess.run(["git", "clone", "-q", str(origin), str(foreign)], check=True)
    proc = _run(origin, tmp_path, check=False)
    assert proc.returncode != 0
    assert "without .deploy/runtime.marker" in proc.stderr


def test_refuses_dirty_runtime_checkout(origin, tmp_path):
    _run(origin, tmp_path)
    (tmp_path / "runtime" / "scripts" / "vig_review_gate_common.py").write_text("tampered\n")
    proc = _run(origin, tmp_path, check=False)
    assert proc.returncode != 0
    assert "local modifications" in proc.stderr


def test_redeploy_preserves_existing_picks_state(origin, tmp_path):
    _run(origin, tmp_path)
    sentinel = tmp_path / "runtime" / ".picks" / "ledger.json"
    sentinel.write_text('{"live": true}\n')
    _run(origin, tmp_path)
    assert sentinel.read_text() == '{"live": true}\n'
    assert (tmp_path / "runtime" / ".picks" / "INDEX.md").read_text() == "seeded\n"


def test_missing_picks_without_seed_aborts(origin, tmp_path):
    proc = _run(origin, tmp_path, check=False, seed=False)
    assert proc.returncode != 0
    assert "no .picks state" in proc.stderr


def test_dry_run_makes_no_changes(origin, tmp_path):
    proc = _run(origin, tmp_path, "--dry-run")
    assert "DRY-RUN" in proc.stdout
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "profile").exists()


def test_dry_run_previews_a_behind_runtime_instead_of_dying(origin, tmp_path):
    """The preview must survive the case it exists for.

    A dry run deliberately skips the reset, so HEAD is still the OLD tip. The
    --expect-sha check ran against it unguarded, so a preview died with
    "deployed tip <old> does not match --expect-sha <new>" whenever the runtime
    was behind — which is every routine redeploy. Found by using it, 2026-08-24.
    """
    _run(origin, tmp_path)
    before = _git(tmp_path / "runtime", "rev-parse", "HEAD")
    profile = tmp_path / "profile" / "scripts"
    before_profile = _sha256(profile / "http_util.py")

    new_tip = _advance_origin(origin, tmp_path, "http_util.py", "# advanced\n")
    assert new_tip != before

    proc = _run(origin, tmp_path, "--dry-run", "--expect-sha", new_tip)
    assert proc.returncode == 0, proc.stderr
    # It names both ends, so the preview is actually informative.
    assert before in proc.stdout and new_tip in proc.stdout
    # And it changed nothing: not the checkout, not the installed profile copy.
    assert _git(tmp_path / "runtime", "rev-parse", "HEAD") == before
    assert _sha256(profile / "http_util.py") == before_profile


def test_deploy_warns_when_the_order_executor_venv_is_absent(origin, tmp_path):
    """A fresh runtime dir has no venv, so the executor's re-exec takes the
    silent path and the failure only surfaces at order time. The deploy does not
    create the venv (no network at deploy time, and .venv/ is gitignored), so
    the least it can do is say so — with the command that fixes it."""
    proc = _run(origin, tmp_path)
    runtime = tmp_path / "runtime"
    assert not (runtime / ".venv").exists()
    assert "no order-executor venv" in proc.stdout
    assert "requirements-exec.txt" in proc.stdout
    # Warned, not failed: review and settlement do not need this venv.
    assert proc.returncode == 0
    assert (tmp_path / "profile" / "scripts" / "vig_review_gate_common.py").is_file()


def _install_stub_venv(runtime: Path, importable: str) -> None:
    """A stub interpreter that succeeds ONLY for `import <importable>`.

    The first version was `#!/bin/sh\\nexit 0`, which exits 0 for any argument:
    it proved the healthy branch was reachable and would have passed identically
    if the deploy probed for a package that does not exist, so it could not tell
    WHICH import the check performs (Reviewer, PR #59).
    """
    venv_bin = runtime / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    stub = venv_bin / "python"
    stub.write_text(
        "#!/bin/sh\n"
        f'case "$*" in *"import {importable}"*) exit 0 ;; *) exit 1 ;; esac\n'
    )
    stub.chmod(0o755)


def test_deploy_reports_a_working_order_executor_venv(origin, tmp_path):
    """The healthy branch has to be reachable too, or the warning above is the
    only outcome the test suite has ever seen."""
    _run(origin, tmp_path)
    _install_stub_venv(tmp_path / "runtime", "polymarket_us")
    proc = _run(origin, tmp_path)
    assert "order-executor venv ok" in proc.stdout
    assert "no order-executor venv" not in proc.stdout


def test_the_venv_check_probes_for_polymarket_us_specifically(origin, tmp_path):
    """Paired with the test above: an interpreter that imports something else
    must NOT read as healthy. Without this, an `exit 0` stub makes the healthy
    branch pass no matter which module the deploy asks for."""
    _run(origin, tmp_path)
    _install_stub_venv(tmp_path / "runtime", "some_other_package")
    proc = _run(origin, tmp_path)
    assert "cannot import polymarket_us" in proc.stdout
    assert "order-executor venv ok" not in proc.stdout
    # Still a warning, not a failure.
    assert proc.returncode == 0


def test_dry_run_still_refuses_a_wrong_expect_sha(origin, tmp_path):
    """Relaxing the post-reset check must not relax the pin itself: Phase 0
    still compares --expect-sha against the real remote, read-only."""
    _run(origin, tmp_path)
    proc = _run(origin, tmp_path, "--dry-run", "--expect-sha", "0" * 40, check=False)
    assert proc.returncode != 0
    assert "does not match --expect-sha" in proc.stderr


def _write_jobs(tmp_path: Path, jobs: list[dict]) -> Path:
    cron = tmp_path / "cron"
    cron.mkdir(exist_ok=True)
    path = cron / "jobs.json"
    path.write_text(json.dumps({"jobs": jobs}, indent=2))
    return path


def test_cron_repoint_rewrites_only_matching_paused_workdirs(origin, tmp_path):
    old = "/opt/dev-checkout"
    jobs = [
        {"id": "a1", "name": "Vig — review", "enabled": False, "workdir": old},
        {"id": "b2", "name": "Vig — calibration", "enabled": False, "workdir": None},
        {"id": "c3", "name": "Other", "enabled": True, "workdir": "/elsewhere"},
    ]
    path = _write_jobs(tmp_path, jobs)
    _run(origin, tmp_path, "--repoint-cron-from", old)
    data = json.loads(path.read_text())["jobs"]
    by_id = {j["id"]: j for j in data}
    assert by_id["a1"]["workdir"] == str(tmp_path / "runtime")
    assert by_id["a1"]["enabled"] is False
    assert by_id["b2"]["workdir"] is None
    assert by_id["c3"]["workdir"] == "/elsewhere"
    assert list((tmp_path / "cron").glob("jobs.json.bak-deploy-*"))


def test_cron_repoint_refuses_enabled_jobs(origin, tmp_path):
    old = "/opt/dev-checkout"
    path = _write_jobs(tmp_path, [
        {"id": "a1", "name": "Vig — review", "enabled": True, "workdir": old},
    ])
    before = path.read_text()
    proc = _run(origin, tmp_path, "--repoint-cron-from", old, check=False)
    assert proc.returncode != 0
    assert "ENABLED" in proc.stderr
    assert path.read_text() == before


def _advance_origin(origin: Path, tmp_path: Path, name: str, content: str) -> str:
    """Push a new commit touching scripts/<name> to the fixture origin's main."""
    work = tmp_path / f"origin-work-{name}"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "scripts" / name).write_text(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", f"advance: {name}")
    _git(work, "push", "-q", "origin", "main")
    return _git(work, "rev-parse", "HEAD")


def _profile_snapshot(tmp_path: Path) -> dict[str, str]:
    profile = tmp_path / "profile" / "scripts"
    if not profile.is_dir():
        return {}
    return {p.name: _sha256(p) for p in sorted(profile.iterdir()) if p.is_file()}


def _no_profile_siblings(tmp_path: Path) -> bool:
    """No backup or stage directory was left next to the live profile dir."""
    parent = tmp_path / "profile"
    if not parent.is_dir():
        return True
    return not list(parent.glob("scripts.bak-*")) and not list(parent.glob("scripts.stage-*"))


def test_expect_sha_mismatch_on_redeploy_preserves_runtime_head(origin, tmp_path):
    _run(origin, tmp_path)
    runtime = tmp_path / "runtime"
    head_a = _git(runtime, "rev-parse", "HEAD")
    profile_before = _profile_snapshot(tmp_path)
    head_b = _advance_origin(origin, tmp_path, "new_module.py", "VALUE = 1\n")
    assert head_b != head_a
    proc = _run(origin, tmp_path, "--expect-sha", head_a, check=False)
    assert proc.returncode != 0
    assert "does not match --expect-sha" in proc.stderr
    # The refused deploy must not have moved the runtime checkout or profile.
    assert _git(runtime, "rev-parse", "HEAD") == head_a
    assert not (runtime / "scripts" / "new_module.py").exists()
    assert _profile_snapshot(tmp_path) == profile_before
    assert _no_profile_siblings(tmp_path)


def test_cron_enabled_refusal_precedes_profile_install(origin, tmp_path):
    old = "/opt/dev-checkout"
    _run(origin, tmp_path)
    profile_before = _profile_snapshot(tmp_path)
    path = _write_jobs(tmp_path, [
        {"id": "a1", "name": "Vig — review", "enabled": True, "workdir": old},
    ])
    jobs_before = path.read_text()
    proc = _run(origin, tmp_path, "--repoint-cron-from", old, check=False)
    assert proc.returncode != 0
    assert "ENABLED" in proc.stderr
    assert path.read_text() == jobs_before
    assert _profile_snapshot(tmp_path) == profile_before
    assert _no_profile_siblings(tmp_path)
    # A first-ever install must also refuse before creating the profile dir.
    shutil.rmtree(tmp_path / "profile")
    proc = _run(origin, tmp_path, "--repoint-cron-from", old, check=False)
    assert proc.returncode != 0
    assert "ENABLED" in proc.stderr
    assert not (tmp_path / "profile").exists()


def test_failed_staged_compile_leaves_live_profile_unchanged(origin, tmp_path):
    _run(origin, tmp_path)
    profile_before = _profile_snapshot(tmp_path)
    _advance_origin(origin, tmp_path, "vig_postgame_gate.py", "def broken(:\n")
    proc = _run(origin, tmp_path, check=False)
    assert proc.returncode != 0
    assert "py_compile failed" in proc.stderr
    assert _profile_snapshot(tmp_path) == profile_before
    assert _no_profile_siblings(tmp_path)


def test_untracked_file_fails_clean_tree_guard(origin, tmp_path):
    _run(origin, tmp_path)
    stray = tmp_path / "runtime" / "scripts" / "local-only.py"
    stray.write_text("print('hand edit')\n")
    proc = _run(origin, tmp_path, check=False)
    assert proc.returncode != 0
    assert "local modifications" in proc.stderr
    assert "local-only.py" in proc.stderr
    stray.unlink()
    # Ignored runtime state (.picks/, .deploy/) alone must still deploy cleanly.
    _run(origin, tmp_path)


def _racing_git_shim(tmp_path: Path, work: Path) -> Path:
    """A git wrapper that advances the fixture origin right after ls-remote.

    Deterministically reproduces the check/use window: the deploy script sees
    tip A from ls-remote, and by the time it fetches, the remote is at B.
    """
    real = shutil.which("git")
    assert real
    bindir = tmp_path / "shim-bin"
    bindir.mkdir()
    flag = tmp_path / "shim-fired"
    shim = bindir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'REAL="{real}"\n'
        f'if [ "$1" = "ls-remote" ] && [ ! -e "{flag}" ]; then\n'
        f'  : > "{flag}"\n'
        '  out="$("$REAL" "$@")"; rc=$?\n'
        f'  "$REAL" -C "{work}" push -q origin main >/dev/null 2>&1\n'
        "  printf '%s\\n' \"$out\"\n"
        "  exit $rc\n"
        "fi\n"
        'exec "$REAL" "$@"\n'
    )
    shim.chmod(0o755)
    return bindir


def test_remote_advancing_after_preflight_preserves_runtime_head(origin, tmp_path):
    """--expect-sha A must not deploy B when the remote moves after ls-remote."""
    _run(origin, tmp_path)
    runtime = tmp_path / "runtime"
    head_a = _git(runtime, "rev-parse", "HEAD")
    profile_before = _profile_snapshot(tmp_path)

    work = tmp_path / "race-work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "scripts" / "raced_module.py").write_text("VALUE = 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "race: advance origin after ls-remote")
    head_b = _git(work, "rev-parse", "HEAD")
    assert head_b != head_a

    env = dict(os.environ)
    env["PATH"] = f"{_racing_git_shim(tmp_path, work)}{os.pathsep}{env['PATH']}"
    proc = _run(origin, tmp_path, "--expect-sha", head_a, check=False, env=env)

    # The shim really did move the remote — otherwise this test proves nothing.
    assert (tmp_path / "shim-fired").exists()
    assert _git(work, "ls-remote", str(origin), "refs/heads/main").split()[0] == head_b

    assert proc.returncode != 0
    assert "does not match --expect-sha" in proc.stderr
    assert _git(runtime, "rev-parse", "HEAD") == head_a
    assert not (runtime / "scripts" / "raced_module.py").exists()
    assert _profile_snapshot(tmp_path) == profile_before
    assert _no_profile_siblings(tmp_path)


def test_abbreviated_or_malformed_expect_sha_is_rejected(origin, tmp_path):
    _run(origin, tmp_path)
    runtime = tmp_path / "runtime"
    head = _git(runtime, "rev-parse", "HEAD")
    profile_before = _profile_snapshot(tmp_path)
    for pin in (head[:1], head[:8], head[:39], head + "a", "z" * 40):
        proc = _run(origin, tmp_path, "--expect-sha", pin, check=False)
        assert proc.returncode != 0, pin
        assert "full 40-hex" in proc.stderr, pin
    assert _git(runtime, "rev-parse", "HEAD") == head
    assert _profile_snapshot(tmp_path) == profile_before
    assert _no_profile_siblings(tmp_path)
    # The full sha still deploys, so the guard is not simply refusing everything.
    _run(origin, tmp_path, "--expect-sha", head.upper())


def test_refuses_symlinked_profile_scripts_dir(origin, tmp_path):
    real_dir = tmp_path / "elsewhere-scripts"
    real_dir.mkdir()
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "scripts").symlink_to(real_dir)
    proc = _run(origin, tmp_path, check=False)
    assert proc.returncode != 0
    assert "symlink" in proc.stderr
    assert not list(real_dir.iterdir())
    assert not (tmp_path / "runtime").exists()


def test_managed_symlink_cannot_escape_the_staging_dir(origin, tmp_path):
    _run(origin, tmp_path)
    profile = tmp_path / "profile" / "scripts"
    external = tmp_path / "external-sentinel.py"
    external.write_text("SENTINEL = 'untouched'\n")
    managed = profile / "execution_guard.py"
    managed.unlink()
    managed.symlink_to(external)

    _run(origin, tmp_path)

    assert external.read_text() == "SENTINEL = 'untouched'\n"
    assert not managed.is_symlink()
    assert _sha256(managed) == _sha256(tmp_path / "runtime" / "scripts" / "execution_guard.py")


def _manifest_names() -> list[str]:
    block = DEPLOY.read_text().split("PROFILE_MANIFEST=(", 1)[1].split(")", 1)[0]
    names = [line.strip() for line in block.splitlines() if line.strip().endswith(".py")]
    assert "execution_guard.py" in names and len(names) >= 10
    return names


def test_the_two_readers_of_the_manifest_read_the_same_manifest():
    """``vig_runtime_verify`` parses the same shell array this test does.

    Two parsers over ONE source is fine and worth keeping — they check each
    other. Two parsers that quietly accept different sets is not: the drift
    check would then verify a manifest the deploy does not install. So the
    agreement is asserted rather than assumed.
    """
    import importlib.util

    path = DEPLOY.parent / "vig_runtime_verify.py"
    spec = importlib.util.spec_from_file_location("vig_runtime_verify_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    names, error = module.load_profile_manifest(DEPLOY)
    assert error is None, error
    assert names == _manifest_names()


def test_the_profile_manifest_is_closed_under_sibling_imports():
    """A manifest module that imports a sibling the manifest omits is a dead profile.

    The deploy copies exactly the manifest into the Vig profile scripts dir.
    A copied module that imports a sibling which was NOT copied raises
    ImportError at import time, so every cron job using it dies — not
    degraded, dead. This PR is what makes that edge live: it added
    `vig_review_gate_common.py`'s unconditional `from vig_run_journal import
    ...`, the first new hard sibling edge into the manifest set, and nothing
    held the manifest right (Reviewer, PR #60).

    The gap set is empty today, so this passes at this tip and reds the moment
    someone adds an import without adding the module.
    """
    manifest = set(_manifest_names())
    reached = import_closure.closure(manifest)

    # Vacuity guard against a closure that computes nothing: the edge this PR
    # introduced must actually be visible to the walk.
    assert "vig_run_journal.py" in import_closure.sibling_imports(
        "vig_review_gate_common.py"
    )
    writer_edges = import_closure.sibling_imports("mlb_slate_writer.py")
    assert "mlb_slate_writer.py" in manifest
    assert {
        "mlb_game_reads.py",
        "mlb_lineup_watchlist.py",
        "mlb_runtime_policy.py",
        "mlb_slate_receipt.py",
        "mlb_stage2_scan.py",
    } <= writer_edges

    missing = sorted(reached - manifest)
    assert not missing, (
        "PROFILE_MANIFEST is not import-closed; a profile copy would fail to "
        f"import: {missing}"
    )


# Any absolute home directory, not one account name. Matching the literal
# /home/clawdbot only ever caught the one account that had already burned us:
# rename the account in an offending line and the guard went quiet while the
# defect was identical. /Users/ is included because a developer's macOS home
# bakes in exactly as well as a Linux one.
BAKED_IN_HOME = re.compile(r"/(?:home|Users)/[A-Za-z0-9._][A-Za-z0-9._-]*")

# Anything a deploy puts on the box or an agent is told to read. Restricting
# this to scripts/ and references/ left 23 files under skills/ unwalked,
# including every SKILL.md — the primary file each skill's agent reads — and
# every validate_params.sh.
SKILL_TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".txt", ".yaml", ".yml"}


def _baked_in_home_offenders(paths) -> list[str]:
    offenders = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            match = BAKED_IN_HOME.search(line)
            if match:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: {match.group(0)} in {line.strip()}"
                )
    return offenders


def _deployed_text_sources() -> list[Path]:
    """Every text file the deploy puts on the box outside skills/.

    Walking only _manifest_names() left ten files in scripts/ unwalked — among
    them deploy-runtime.sh, check_script_provenance.py, install-hermes.sh and
    the NFL/soccer scanners — plus docs/ and templates/ entirely, all of which
    ship in the runtime checkout. The manifest guard got the widened REGEX in
    the first commit of this PR and not the widened WALK, and the walk was the
    weakness (Reviewer, PR #59).
    """
    paths = sorted(
        p
        for root in ("scripts", "docs", "templates")
        for p in (REPO_ROOT / root).rglob("*")
        if p.is_file()
        and p.suffix in SKILL_TEXT_SUFFIXES
        and "__pycache__" not in p.parts
    )
    manifest = set(_manifest_names())
    found = {p.name for p in paths}
    # Vacuity guards, each a distinct class: the whole manifest, the deploy
    # script that walks it, and the docs tree.
    assert manifest <= found, sorted(manifest - found)
    assert "deploy-runtime.sh" in found and "install-hermes.sh" in found, sorted(found)
    assert any(p.parent.name == "docs" for p in paths), paths
    return paths


# A cd into a fixed runtime path in agent-read docs. Not covered by
# BAKED_IN_HOME, which bans /home/<acct> and /Users/<acct> and says nothing about
# "~". The default runtime dir is a legitimate string in code (it IS the default)
# and a defect in an instruction, because the executor resolves its interpreter
# partly from the working directory — so a doc that pins cwd contradicts the
# carrier whenever the runtime was deployed anywhere else (Reviewer, PR #59).
PINNED_RUNTIME_CWD = re.compile(r"cd\s+\S*projects/sports-picks-(runtime|skill)")


def test_agent_read_docs_do_not_pin_a_runtime_working_directory():
    offenders = []
    for path in _skill_sources():
        if path.suffix != ".md":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if PINNED_RUNTIME_CWD.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "agent-read doc pins a working directory the executor resolves from:\n"
        + "\n".join(offenders)
    )


def test_deployed_scripts_and_docs_have_no_baked_in_home():
    """Production runs as sauce_packets, where /home/clawdbot does not exist."""
    offenders = _baked_in_home_offenders(_deployed_text_sources())
    assert not offenders, "baked-in home path in deployed files:\n" + "\n".join(offenders)


def _skill_sources() -> list[Path]:
    """Every runtime-reachable text file under skills/.

    Deliberately not a curated glob list. The previous version walked only
    scripts/*.py and references/*.md, which is how the Polymarket executor got
    outside every guard in the first place — a narrower glob fails exactly the
    way a missing guard does, and the non-empty assertions below defend against
    an EMPTY glob, not a NARROW one."""
    paths = sorted(
        p
        for p in (REPO_ROOT / "skills").rglob("*")
        if p.is_file()
        and p.suffix in SKILL_TEXT_SUFFIXES
        and "__pycache__" not in p.parts
    )
    # Vacuity guards: each names a distinct file class that has to be present.
    assert any(p.name == "polymarket_us_sdk_bet.py" for p in paths), paths
    assert sum(p.name == "SKILL.md" for p in paths) >= 5, paths
    assert any(p.name == "validate_params.sh" for p in paths), paths
    assert any(p.parent.name == "references" for p in paths), paths
    return paths


def test_skill_files_have_no_baked_in_home():
    """The manifest guard above stops at scripts/. Everything under skills/ is
    reachable at runtime and was outside every guard until 2026-08-23, when a
    baked-in interpreter path in polymarket_us_sdk_bet.py had to be hand-patched
    on the production box.

    A baked-in home is worse here than a wrong path usually is: the self-heal
    re-exec is guarded by os.path.exists, so a home that does not exist makes
    the guard silently False and the executor runs without polymarket_us."""
    offenders = _baked_in_home_offenders(_skill_sources())
    assert not offenders, "baked-in home path in runtime-reachable skill files:\n" + "\n".join(offenders)


EXECUTOR = REPO_ROOT / "skills" / "sports-picks" / "scripts" / "polymarket_us_sdk_bet.py"
RESOLVER = REPO_ROOT / "scripts" / "resolve_exec_venv.py"


def _executor_prologue() -> str:
    prologue = EXECUTOR.read_text().split("import argparse", 1)[0]
    assert "_SP_VENV" in prologue
    return prologue


def _resolve_venv(env: dict[str, str], cwd: Path | None = None,
                  executor: Path | None = None) -> str:
    """What the executor will re-exec into, via the SAME resolver the deploy uses.

    Deliberately not a second copy of the prologue-extraction: the deploy had
    one and this helper had another, and two copies of one computation agree
    only until one of them changes (Reviewer, PR #59).

    cwd matters, because the ladder consults the working directory — a test that
    does not pin it measures whatever directory pytest happened to start in.
    """
    proc = subprocess.run(
        [sys.executable, str(RESOLVER), str(executor or EXECUTOR)],
        env=env, cwd=str(cwd) if cwd else tempfile.gettempdir(),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _run_prologue(env: dict[str, str], expr: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _executor_prologue() + f"\nprint({expr})\n"],
        env=env, cwd=str(cwd) if cwd else tempfile.gettempdir(),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# The sentinel short-circuits the re-exec itself, so path resolution can be
# measured on any host whether or not the resolved path exists here.
_NO_REEXEC = {"PATH": "/usr/bin:/bin", "_SP_VENV_REEXEC": "1"}


def test_executor_venv_path_follows_invoking_home(tmp_path):
    """The re-exec target must move with HOME and be overridable, so the same
    checkout works for whichever account the runtime happens to run as."""
    assert _run_prologue({**_NO_REEXEC, "HOME": "/home/sauce_packets"}, "_SP_VENV") == (
        "/home/sauce_packets/projects/sports-picks-runtime/.venv/bin/python"
    )
    # Moves with HOME rather than naming any one account.
    assert _run_prologue(
        {**_NO_REEXEC, "HOME": "/home/someone-else"}, "_SP_VENV"
    ).startswith("/home/someone-else/")
    # Explicit override wins outright.
    override = str(tmp_path / "custom" / "python")
    assert _run_prologue(
        {**_NO_REEXEC, "HOME": "/home/sauce_packets", "SPORTS_PICKS_VENV_PYTHON": override},
        "_SP_VENV",
    ) == override


def test_executor_venv_path_follows_the_runtime_dir_knob(tmp_path):
    """The venv lives inside the runtime checkout, so the default must track
    SPORTS_PICKS_RUNTIME_DIR — the variable deploy-runtime.sh itself reads for
    that directory, and what --runtime-dir sets.

    Hardcoding that setting's default value is the same silent-skip defect one
    supported flag away: deploy with --runtime-dir and the venv is over there
    while the executor looks under ~/projects, os.path.exists is False, and the
    re-exec never fires.

    This covers the ENV VAR only. The --runtime-dir FLAG is a separate carrier
    and is covered end-to-end by
    test_runtime_dir_flag_reaches_the_executor_through_the_workdir — the
    previous version of this test asserted only that both strings appeared in
    deploy-runtime.sh, which was true at 3f186b6 where the flag path was
    entirely broken (Reviewer, PR #59)."""
    env = {**_NO_REEXEC, "HOME": "/home/sauce_packets",
           "SPORTS_PICKS_RUNTIME_DIR": "/srv/vig/runtime"}
    assert _run_prologue(env, "_SP_VENV") == "/srv/vig/runtime/.venv/bin/python"

    # A ~ in the deploy flag is expanded rather than taken literally.
    assert _run_prologue(
        {**_NO_REEXEC, "HOME": "/home/sauce_packets",
         "SPORTS_PICKS_RUNTIME_DIR": "~/alt-runtime"},
        "_SP_VENV",
    ) == "/home/sauce_packets/alt-runtime/.venv/bin/python"

    # The direct interpreter override still wins over the directory knob.
    override = str(tmp_path / "custom" / "python")
    assert _run_prologue(
        {**env, "SPORTS_PICKS_VENV_PYTHON": override}, "_SP_VENV"
    ) == override


def test_runtime_dir_flag_reaches_the_executor_through_the_workdir(tmp_path):
    """--runtime-dir must actually change which interpreter the executor uses.

    It did not. The flag sets a SHELL LOCAL; deploy-runtime.sh exports nothing
    and the cron repoint writes workdirs and no environment, so
    SPORTS_PICKS_RUNTIME_DIR was never in the executor's environment and only
    the env-var path worked (Reviewer, PR #59).

    What the flag DOES reach is cron's workdir, which the repoint sets to the
    runtime checkout. So the prologue consults the current directory when it
    looks like a state root — the same ladder resolve_root() uses — and the flag
    arrives through a carrier that exists.

    Deliberately does NOT set SPORTS_PICKS_RUNTIME_DIR: that is the path that
    already worked, and setting it here would let this pass while the flag
    stayed broken."""
    elsewhere = tmp_path / "elsewhere" / "runtime"
    (elsewhere / ".deploy").mkdir(parents=True)
    (elsewhere / ".deploy" / "runtime.marker").write_text("runtime checkout\n")

    cron_like = {"PATH": "/usr/bin:/bin", "HOME": "/home/sauce_packets",
                 "_SP_VENV_REEXEC": "1"}
    # Resolved exactly as a cron-invoked executor would: workdir is the runtime
    # checkout, and neither directory knob is in the environment.
    assert _run_prologue(cron_like, "_SP_VENV", cwd=elsewhere) == str(
        elsewhere / ".venv" / "bin" / "python"
    )

    # A cwd that is NOT a deploy-managed runtime must not be mistaken for one.
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    assert _run_prologue(cron_like, "_SP_VENV", cwd=plain) == (
        "/home/sauce_packets/projects/sports-picks-runtime/.venv/bin/python"
    )


def test_a_dev_checkout_with_picks_is_not_mistaken_for_the_runtime(tmp_path):
    """The rung's discriminator is .deploy/runtime.marker, not .picks/.

    ".picks/ exists" means "has pick state", not "is the deploy-managed
    runtime" — and docs/deploy-runtime.md names a second such directory in this
    repo's own instructions: --seed-picks-from ~/projects/sports-picks-skill.
    A dev checkout has .picks/ and no .venv, and every documented order command
    is a relative path run from cwd, so that rung captured the resolution and
    reopened the silent skip with NO flag and NO env var needed — a regression
    against what is deployed today, on the order lane (Reviewer, PR #59).

    The marker is the file deploy-runtime.sh writes into a checkout it created
    and is the only checkout it will hard-reset, so it is exactly the
    "deploy-managed runtime" predicate the carrier argument rests on.
    """
    cron_like = {"PATH": "/usr/bin:/bin", "HOME": "/home/sauce_packets",
                 "_SP_VENV_REEXEC": "1"}

    dev = tmp_path / "devcheckout"
    (dev / ".picks").mkdir(parents=True)
    assert _run_prologue(cron_like, "_SP_VENV", cwd=dev) == (
        "/home/sauce_packets/projects/sports-picks-runtime/.venv/bin/python"
    )

    # The same directory, once a deploy has claimed it, IS the runtime.
    (dev / ".deploy").mkdir()
    (dev / ".deploy" / "runtime.marker").write_text("runtime checkout\n")
    assert _run_prologue(cron_like, "_SP_VENV", cwd=dev) == str(
        dev / ".venv" / "bin" / "python"
    )


def test_the_marker_the_rung_reads_is_the_one_the_deploy_writes(origin, tmp_path):
    """Paired with the test above, and the reason it is not a magic string.

    If the deploy ever renames or relocates its marker, the executor's rung must
    stop resolving — otherwise the two agree only by coincidence."""
    _run(origin, tmp_path)
    runtime = tmp_path / "runtime"
    marker = runtime / ".deploy" / "runtime.marker"
    assert marker.is_file()
    assert "runtime.marker" in DEPLOY.read_text()

    cron_like = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"),
                 "_SP_VENV_REEXEC": "1"}
    assert _resolve_venv(cron_like, cwd=runtime) == str(
        runtime / ".venv" / "bin" / "python"
    )
    marker.unlink()
    assert _resolve_venv(cron_like, cwd=runtime) == str(
        tmp_path / "home" / "projects" / "sports-picks-runtime" / ".venv" / "bin" / "python"
    )


def test_deploy_asks_the_executor_which_interpreter_it_will_use(origin, tmp_path):
    """The deploy's venv check must not rebuild the path the executor resolves.

    Rebuilding it made the check and the prologue two independent computations
    of one path, so a deploy with a non-default --runtime-dir printed
    "order-executor venv ok" for a venv the executor never consults. Before the
    check existed that divergence was silent; reporting it affirmatively healthy
    is worse (Reviewer, PR #59, probe 2).

    This is the paired assertion: the deploy's verdict and the executor's own
    resolution must name the SAME interpreter, for a runtime dir that is not the
    default."""
    runtime = tmp_path / "elsewhere" / "runtime"
    (tmp_path / "seed" / ".picks").mkdir(parents=True)
    (tmp_path / "seed" / ".picks" / "INDEX.md").write_text("seeded\n")
    args = [
        "bash", str(DEPLOY),
        "--repo-url", str(origin),
        "--runtime-dir", str(runtime),
        "--profile-scripts", str(tmp_path / "profile" / "scripts"),
        "--cron-jobs", str(tmp_path / "cron" / "jobs.json"),
        "--seed-picks-from", str(tmp_path / "seed"),
    ]
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    env.pop("SPORTS_PICKS_RUNTIME_DIR", None)
    env.pop("SPORTS_PICKS_ROOT", None)
    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    # What the executor will actually re-exec into, resolved as cron does.
    resolved = _run_prologue(
        {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"),
         "_SP_VENV_REEXEC": "1"},
        "_SP_VENV",
        cwd=runtime,
    )
    assert resolved == str(runtime / ".venv" / "bin" / "python")
    # The deploy names that same interpreter, and not the default one.
    assert resolved in proc.stdout
    assert str(tmp_path / "home" / "projects" / "sports-picks-runtime") not in proc.stdout

    # The assertion above cannot tell "asked the prologue" from "rebuilt the
    # path", because for a runtime dir both produce the same answer — which is
    # the point of the fix and also a hole in the test. The previous version
    # closed it with SPORTS_PICKS_VENV_PYTHON; that variable is now cleared (see
    # test_the_deploy_check_ignores_an_exported_interpreter_knob), so the
    # discriminator has to be the resolution ITSELF.
    #
    # Change where the EXECUTOR looks, change nothing else, and the deploy must
    # follow. A reconstruction of $RUNTIME_DIR/.venv/bin/python cannot.
    patched = EXECUTOR.read_text().replace(
        '".venv", "bin", "python"', '".venv-alt", "bin", "python"'
    )
    assert '".venv-alt"' in patched
    _advance_origin_skill(origin, tmp_path, "polymarket_us_sdk_bet.py", patched)
    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert str(runtime / ".venv-alt" / "bin" / "python") in proc.stdout
    assert str(runtime / ".venv" / "bin" / "python") not in proc.stdout


def _advance_origin_skill(origin: Path, tmp_path: Path, name: str, content: str) -> str:
    """Push a commit replacing skills/sports-picks/scripts/<name> on main."""
    work = tmp_path / f"origin-skill-work-{name}"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "skills" / "sports-picks" / "scripts" / name).write_text(content)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", f"advance skill: {name}")
    _git(work, "push", "-q", "origin", "main")
    return _git(work, "rev-parse", "HEAD")


def test_the_deploy_check_ignores_an_exported_interpreter_knob(origin, tmp_path):
    """All three SPORTS_PICKS_* knobs are cleared, not two.

    The check used to clear the two directory knobs and honour
    SPORTS_PICKS_VENV_PYTHON, on the claim that honouring it "can only make this
    check stricter, never falsely green". It could not: exported at a WORKING
    interpreter, the deploy printed "order-executor venv ok" about that
    interpreter while the executor resolved somewhere else — the same false
    green, one exported variable instead of one flag, and it is the variable the
    skip-reason message tells the operator to set.

    The asymmetry was the bug: either cron inherits the deploy shell, so
    clearing the directory knobs is wrong, or it does not, so honouring the
    interpreter knob is. Both cannot hold (Reviewer, PR #59)."""
    _run(origin, tmp_path)
    runtime = tmp_path / "runtime"
    # An interpreter that imports polymarket_us happily — the false-green shape.
    good = tmp_path / "exported" / "bin" / "python"
    good.parent.mkdir(parents=True)
    good.write_text("#!/bin/sh\nexit 0\n")
    good.chmod(0o755)

    proc = _run(origin, tmp_path, env={**os.environ,
                                       "SPORTS_PICKS_VENV_PYTHON": str(good)})
    # It reports the interpreter the EXECUTOR resolves, and never the export.
    assert str(good) not in proc.stdout
    assert str(runtime / ".venv" / "bin" / "python") in proc.stdout
    assert "order-executor venv ok" not in proc.stdout


def test_executor_records_why_the_reexec_was_skipped(tmp_path):
    """A skipped re-exec is silent, and the failure surfaces much later as
    'missing dependency: pip install polymarket-us' — the wrong remedy, since
    on the production box the package IS installed, in a venv this process
    never entered. sdk_client reports this reason instead."""
    missing = tmp_path / "nowhere" / "python"
    reason = _run_prologue(
        {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
         "SPORTS_PICKS_VENV_PYTHON": str(missing)},
        "_SP_VENV_SKIP_REASON",
    )
    assert str(missing) in reason and "does not exist" in reason
    # It names the knobs that fix it, not a pip install.
    assert "SPORTS_PICKS_RUNTIME_DIR" in reason and "pip install" not in reason

    # Already re-execed and still missing is a different failure and says so.
    reason = _run_prologue(
        {**_NO_REEXEC, "HOME": str(tmp_path)}, "_SP_VENV_SKIP_REASON"
    )
    assert "already re-execed" in reason

    # And sdk_client actually reports it rather than the pip remedy.
    src = (REPO_ROOT / "skills" / "sports-picks" / "scripts" / "polymarket_us_sdk_bet.py").read_text()
    body = src.split("def sdk_client(", 1)[1].split("\ndef ", 1)[0]
    assert "_SP_VENV_SKIP_REASON" in body


def test_executor_hermes_env_follows_invoking_home(tmp_path):
    """Credentials are read from ~/.hermes/.env. That path must follow HOME:
    the old candidate list fell back to a literal /home/clawdbot, which does not
    exist on the production box, so the fallback could only ever miss."""
    executor = REPO_ROOT / "skills" / "sports-picks" / "scripts" / "polymarket_us_sdk_bet.py"
    code = (
        # Stubbed because this asserts a path constant, not any HTTP behaviour,
        # and httpx is not a test-environment dependency.
        "import sys, types; sys.modules.setdefault('httpx', types.ModuleType('httpx'))\n"
        "import importlib.util as u\n"
        f"s = u.spec_from_file_location('sp_exec', {str(executor)!r})\n"
        "m = u.module_from_spec(s); s.loader.exec_module(m)\n"
        "print(m.HERMES_ENV)\n"
    )
    home = tmp_path / "somebody"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "_SP_VENV_REEXEC": "1"},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(home / ".hermes" / ".env")


def test_production_identity_paths_resolve_from_home():
    """Every state path must follow the invoking user's home (production user is
    sauce_packets, where /home/clawdbot does not exist)."""
    fake_home = "/home/sauce_packets"
    env = {k: v for k, v in os.environ.items() if k not in (
        "HERMES_BIN", "VIG_RISK_LIMITS_PATH", "VIG_PICKS_FILE", "SPORTS_PICKS_ROOT")}
    env["HOME"] = fake_home
    env["PATH"] = "/usr/bin:/bin"
    code = "\n".join([
        "import execution_guard, mlb_execution_gate, vig_review_gate_common, vig_postgame_gate",
        "print(execution_guard.RISK_LIMITS_PATH)",
        "print(execution_guard.CANONICAL_PICKS_PATH)",
        "print(mlb_execution_gate.RISK_LIMITS_PATH)",
        "print(vig_review_gate_common.HERMES)",
        "print(vig_postgame_gate.PICKS)",
        "prompt = vig_postgame_gate.build_settlement_prompt(",
        "    open_pick_ids='mlb-x', open_count=1, cohort_section='',",
        "    small_cohort_section='', recon_section='')",
        "assert '/home/clawdbot' not in prompt, 'clawdbot path in settlement prompt'",
        "assert f'{__import__(\"os\").environ[\"HOME\"]}/notes/Sports/picks/picks.json' in prompt",
        "assert 'record.json' in prompt",
    ])
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT / "scripts", env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    lines = proc.stdout.strip().splitlines()
    assert lines == [
        f"{fake_home}/.hermes/vig/state/risk_limits.json",
        f"{fake_home}/notes/Sports/picks/picks.json",
        f"{fake_home}/.hermes/vig/state/risk_limits.json",
        f"{fake_home}/.local/bin/hermes",
        f"{fake_home}/notes/Sports/picks/picks.json",
    ]


STAGED_DIR = REPO_ROOT / "docs" / "staged"
WIRING_DOC = REPO_ROOT / "docs" / "pipeline-wiring-2026-09-03.md"


def test_staged_live_edit_fragments_match_the_hashes_the_procedure_declares():
    """The procedure's apply step reads these files; the doc quotes them.

    Two copies of one string agree only until one of them changes, and the
    whole reason this lane exists is a rail keyed on a hand-copied field. The
    doc declares a sha256 and a byte count for each staged fragment, so the
    fragment and its declaration are pinned to each other here: edit the
    routing block without re-deriving the hash and this reds, instead of an
    operator installing a prompt nobody reviewed.
    """
    declared = {
        name: (int(size), digest)
        for name, size, digest in re.findall(
            r"`docs/staged/([^`]+)` \| (\d+) \| `([0-9a-f]{64})`", WIRING_DOC.read_text()
        )
    }
    assert declared, "the procedure declares no staged fragment hashes"
    on_disk = sorted(p.name for p in STAGED_DIR.iterdir() if p.is_file())
    # Both directions: an undeclared fragment is as bad as a stale hash.
    assert on_disk == sorted(declared), (on_disk, sorted(declared))
    for name, (size, digest) in declared.items():
        raw = (STAGED_DIR / name).read_bytes()
        assert len(raw) == size, name
        assert hashlib.sha256(raw).hexdigest() == digest, name


def test_the_staged_evening_prompt_transform_reaches_the_reviewed_hash():
    """The apply step is pinned to an after-hash; that hash must be reachable.

    Reproduces the procedure's transform against the recorded before-text and
    asserts the same after-hash the doc tells the operator to require. Without
    this the procedure could declare an after-hash nothing produces, and the
    apply would abort in production with the prompt already half-migrated in
    the operator's head.
    """
    before = (REPO_ROOT / "tests" / "fixtures" / "evening-slate-prompt-27087cc00dfa.txt").read_text()
    assert (
        hashlib.sha256(before.encode()).hexdigest()
        == "dbae855c2e15de931739481e9174ee16d002459d972ff6b2526dded834c8fca4"
    )
    old = "[Discipline line. Total proposed exposure. Vig review pending. No automatic execution.]"
    block = (STAGED_DIR / "evening-slate-routing-block.txt").read_text().rstrip("\n")
    ageout = (STAGED_DIR / "evening-slate-ageout.txt").read_text().rstrip("\n")
    task0 = [line for line in before.split("\n") if line.startswith("0. Read")][0]
    assert before.count(old) == 1 and before.count(task0) == 1
    after = before.replace(task0, task0 + ageout).replace(old, block)
    assert (
        hashlib.sha256(after.encode()).hexdigest()
        == "decdf0e1e4b7f27ff34bc897f162b5ccfe75c8baac98754abb8efff64e2fcf4e"
    )
    assert len(after) == 17693
    # The point of the edit. Note the phrase itself survives inside the
    # routing block, which QUOTES it as forbidden output — so the assertion is
    # on the retired template line, not on the words.
    assert old not in after
    assert "STANDING AUTHORIZATION" in after
    assert "AGE-OUT:" in after
