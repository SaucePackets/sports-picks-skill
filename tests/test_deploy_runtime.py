"""End-to-end tests for scripts/deploy-runtime.sh against a local fixture origin.

The fixture origin is a real git repo built from this repo's own scripts/, so the
profile manifest baked into the deploy script is exercised against the actual
file inventory it will deploy.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
         seed: bool = True) -> subprocess.CompletedProcess:
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
    proc = subprocess.run(args, capture_output=True, text=True)
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


def _manifest_names() -> list[str]:
    block = DEPLOY.read_text().split("PROFILE_MANIFEST=(", 1)[1].split(")", 1)[0]
    names = [line.strip() for line in block.splitlines() if line.strip().endswith(".py")]
    assert "execution_guard.py" in names and len(names) >= 10
    return names


def test_manifest_scripts_have_no_clawdbot_assumptions():
    for name in _manifest_names():
        src = (REPO_ROOT / "scripts" / name).read_text()
        assert "/home/clawdbot" not in src, f"hardcoded /home/clawdbot path in scripts/{name}"


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
