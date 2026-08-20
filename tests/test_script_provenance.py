"""Tests for scripts/check_script_provenance.py.

Most cases run against a synthetic repo fixture so drift can be manufactured
without touching the real tree; the last group asserts the invariants hold for
this repo as it actually stands.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_script_provenance as prov

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_script_provenance.py"

DEPLOY_STUB = """#!/usr/bin/env bash
set -euo pipefail

PROFILE_MANIFEST=(
  http_util.py
  gate.py  # trailing comment
)

echo deploy
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A synthetic canonical repo: two manifest files, one non-manifest file."""
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "deploy-runtime.sh").write_text(DEPLOY_STUB)
    (scripts / "http_util.py").write_text("CANON = 'http'\n")
    (scripts / "gate.py").write_text("CANON = 'gate'\n")
    (scripts / "devtool.py").write_text("CANON = 'dev'\n")
    vendored = root / "skills" / "sports-picks" / "scripts"
    vendored.mkdir(parents=True)
    shutil.copy(scripts / "http_util.py", vendored / "http_util.py")
    return root


def _copy_tree(repo: Path, dest: Path, names: list[str]) -> Path:
    dest.mkdir(parents=True)
    for name in names:
        shutil.copy(repo / "scripts" / name, dest / name)
    return dest


def _report(repo: Path, copies=(), *, ref=None, strict=False) -> dict:
    return prov.build_report(repo, ref, list(copies), strict)


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["check"] == name)


def _statuses(check: dict) -> dict[str, str]:
    return {f["file"]: f["status"] for f in check["findings"]}


# --- manifest parsing -------------------------------------------------------

def test_parses_manifest_from_deploy_script_ignoring_comments():
    assert prov.parse_profile_manifest(DEPLOY_STUB) == ["http_util.py", "gate.py"]


def test_manifest_parse_reads_the_real_deploy_script():
    text = (REPO_ROOT / "scripts" / "deploy-runtime.sh").read_text()
    manifest = prov.parse_profile_manifest(text)
    assert "vig_mlb_review_gate.py" in manifest
    assert all(name.endswith(".py") for name in manifest)


@pytest.mark.parametrize("text", ["echo hi\n", "PROFILE_MANIFEST=(\n\n)\n"])
def test_manifest_parse_rejects_missing_or_empty_array(text: str):
    with pytest.raises(prov.ProvenanceError):
        prov.parse_profile_manifest(text)


def test_manifest_check_fails_when_an_entry_is_not_in_scripts(repo: Path):
    (repo / "scripts" / "gate.py").unlink()
    report = _report(repo)
    check = _check(report, "profile-manifest-resolves")
    assert not check["ok"] and _statuses(check) == {"gate.py": "missing"}
    assert not report["ok"]


# --- vendored copy identity -------------------------------------------------

def test_vendored_copy_identical_passes(repo: Path):
    report = _report(repo)
    assert _check(report, "vendored-copies-identical")["ok"]
    assert report["ok"]


def test_vendored_copy_drift_is_reported(repo: Path):
    (repo / "skills" / "sports-picks" / "scripts" / "http_util.py").write_text("DRIFTED = 1\n")
    report = _report(repo)
    check = _check(report, "vendored-copies-identical")
    assert not check["ok"]
    assert _statuses(check) == {"skills/sports-picks/scripts/http_util.py": "differs"}


def test_missing_vendored_copy_is_reported(repo: Path):
    (repo / "skills" / "sports-picks" / "scripts" / "http_util.py").unlink()
    check = _check(_report(repo), "vendored-copies-identical")
    assert _statuses(check) == {"skills/sports-picks/scripts/http_util.py": "missing"}


# --- derived copies ---------------------------------------------------------

def test_full_copy_matching_canonical_is_clean(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "runtime",
                        ["deploy-runtime.sh", "http_util.py", "gate.py", "devtool.py"])
    report = _report(repo, [("runtime", "full", target)])
    assert _check(report, "copy:runtime")["ok"]
    assert report["ok"]


def test_full_copy_surfaces_differing_and_missing_files(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "dev", ["deploy-runtime.sh", "http_util.py", "gate.py"])
    (target / "gate.py").write_text("LOCAL_EDIT = 1\n")
    report = _report(repo, [("dev", "full", target)])
    check = _check(report, "copy:dev")
    assert not check["ok"]
    assert _statuses(check) == {"gate.py": "differs", "devtool.py": "missing"}
    assert not report["ok"]


def test_manifest_copy_ignores_non_manifest_canonical_files(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "profile", ["http_util.py", "gate.py"])
    check = _check(_report(repo, [("profile", "manifest", target)]), "copy:profile")
    assert check["ok"]
    assert "2 canonical files" in check["detail"]


def test_manifest_copy_reports_drift_in_a_manifest_file(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "profile", ["http_util.py", "gate.py"])
    (target / "http_util.py").write_text("STALE = 1\n")
    check = _check(_report(repo, [("profile", "manifest", target)]), "copy:profile")
    assert not check["ok"] and _statuses(check)["http_util.py"] == "differs"


def test_unmanaged_extra_file_is_reported_but_not_drift(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "profile", ["http_util.py", "gate.py"])
    (target / "test_orphan.py").write_text("ORPHAN = 1\n")
    check = _check(_report(repo, [("profile", "manifest", target)]), "copy:profile")
    assert check["ok"]
    assert _statuses(check) == {"test_orphan.py": "unmanaged"}


def test_strict_promotes_unmanaged_to_drift(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "profile", ["http_util.py", "gate.py"])
    (target / "test_orphan.py").write_text("ORPHAN = 1\n")
    report = _report(repo, [("profile", "manifest", target)], strict=True)
    assert not _check(report, "copy:profile")["ok"]


def test_symlinked_copy_file_is_not_accepted(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "profile", ["gate.py"])
    link = target / "http_util.py"
    link.symlink_to(repo / "scripts" / "http_util.py")
    check = _check(_report(repo, [("profile", "manifest", target)]), "copy:profile")
    assert _statuses(check)["http_util.py"] == "not-a-regular-file"


def test_unreachable_copy_path_is_drift(repo: Path, tmp_path: Path):
    check = _check(_report(repo, [("gone", "full", tmp_path / "nope")]), "copy:gone")
    assert not check["ok"] and check["findings"][0]["status"] == "unreachable"


def test_checker_never_writes_to_the_copy(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "dev", ["http_util.py"])
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()}
    _report(repo, [("dev", "full", target)])
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in target.iterdir()}
    assert before == after


# --- git ref mode -----------------------------------------------------------

@pytest.fixture()
def committed_repo(repo: Path) -> Path:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "canonical")
    return repo


def test_ref_mode_ignores_uncommitted_working_tree_edits(committed_repo: Path, tmp_path: Path):
    target = _copy_tree(committed_repo, tmp_path / "runtime",
                        ["deploy-runtime.sh", "http_util.py", "gate.py", "devtool.py"])
    (committed_repo / "scripts" / "gate.py").write_text("WIP = 1\n")

    working = _report(committed_repo, [("runtime", "full", target)])
    assert _statuses(_check(working, "copy:runtime")) == {"gate.py": "differs"}

    committed = _report(committed_repo, [("runtime", "full", target)], ref="main")
    assert _check(committed, "copy:runtime")["ok"]
    assert committed["canonical"].endswith("@main")


def test_ref_mode_reads_the_manifest_from_the_ref(committed_repo: Path):
    # An uncommitted manifest edit naming a file that does not exist: the
    # working tree must fail on it, the committed ref must not see it at all.
    (committed_repo / "scripts" / "deploy-runtime.sh").write_text("PROFILE_MANIFEST=(\n  only.py\n)\n")

    working = _check(_report(committed_repo), "profile-manifest-resolves")
    assert not working["ok"] and _statuses(working) == {"only.py": "missing"}

    committed = _check(_report(committed_repo, ref="main"), "profile-manifest-resolves")
    assert committed["ok"] and "2 manifest entries" in committed["detail"]


def test_unknown_ref_is_an_error(committed_repo: Path):
    with pytest.raises(prov.ProvenanceError):
        _report(committed_repo, ref="no-such-ref")


# --- CLI --------------------------------------------------------------------

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECKER), *args], capture_output=True, text=True)


def test_cli_exits_zero_and_prints_clean(repo: Path):
    proc = _cli("--repo-root", str(repo))
    assert proc.returncode == 0, proc.stderr
    assert "provenance: clean" in proc.stdout


def test_cli_exits_one_on_drift_and_names_the_copy(repo: Path, tmp_path: Path):
    target = _copy_tree(repo, tmp_path / "dev", ["deploy-runtime.sh", "http_util.py", "gate.py"])
    (target / "gate.py").write_text("LOCAL_EDIT = 1\n")
    proc = _cli("--repo-root", str(repo), "--copy", f"dev:full={target}")
    assert proc.returncode == 1
    assert "DRIFT" in proc.stdout and "gate.py" in proc.stdout


def test_cli_json_report_is_machine_readable(repo: Path):
    proc = _cli("--repo-root", str(repo), "--json")
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert {c["check"] for c in payload["checks"]} == {
        "profile-manifest-resolves", "vendored-copies-identical",
    }


def test_cli_rejects_a_bad_copy_spec(repo: Path):
    for bad in ("dev=/tmp/x", "dev:sideways=/tmp/x", "dev:full"):
        proc = _cli("--repo-root", str(repo), "--copy", bad)
        assert proc.returncode == 2, bad


def test_cli_reports_a_repo_root_without_scripts(tmp_path: Path):
    proc = _cli("--repo-root", str(tmp_path))
    assert proc.returncode == 2 and "error:" in proc.stderr


# --- the real repo ----------------------------------------------------------

def test_this_repo_satisfies_its_own_provenance_invariants():
    report = _report(REPO_ROOT)
    assert report["ok"], report["checks"]
