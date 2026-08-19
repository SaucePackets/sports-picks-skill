"""End-to-end tests for scripts/deploy-runtime.sh against a local fixture origin.

The fixture origin is a real git repo built from this repo's own scripts/, so the
profile manifest baked into the deploy script is exercised against the actual
file inventory it will deploy.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
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
