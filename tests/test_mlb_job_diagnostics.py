"""Synthetic reproduction: approved runtime card, silent wrong-root pre-check."""

import importlib.util
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("mlb_job_diagnostics", SCRIPTS / "mlb_job_diagnostics.py")
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)
import mlb_execution_gate as gate


def market(**changes):
    return {"model_version": "vig-mlb-market-v1", "dk_fair_prob": .44,
            "raw_probability": .44, "conservative_probability": .44,
            "uncertainty_haircut": 0,
            "probability_components": {"adjustments": [], "haircuts": []}, **changes}


def roots(tmp_path):
    home = tmp_path / "home"
    runtime = home / "projects" / "sports-picks-runtime"
    developer = home / "projects" / "sports-picks-skill"
    cwd = home / ".hermes" / "profiles" / "vig" / "scripts"
    cwd.mkdir(parents=True)
    (developer / ".picks").mkdir(parents=True)
    (runtime / ".picks" / "execute").mkdir(parents=True)
    (runtime / ".picks" / "execute" / "2026-09-08-schedule.json").write_text(
        json.dumps({"candidates": [market(vig_approved=True)]}))
    return home, runtime, developer, cwd


def test_wrong_cwd_falls_back_and_silent_success_does_not_mean_empty_runtime(tmp_path, monkeypatch):
    home, runtime, developer, cwd = roots(tmp_path)
    monkeypatch.delenv("SPORTS_PICKS_ROOT", raising=False)
    report = diag.root_snapshot(runtime, cwd, home, "2026-09-08")
    assert report["evidence_kind"] == "current_snapshot_not_historical_execution"
    assert report["resolved_gate_root"] == str(developer)
    assert not report["root_matches_runtime"]
    assert report["schedules"][0]["approved_count"] == 1
    assert report["schedules"][1]["exists"] is False
    # Exercise the real missing-schedule exit using synthetic disk state only.
    # A task renderer must never be reached, nor any policy/venue code invoked.
    with patch.object(gate, "resolve_root", return_value=developer), \
         patch.object(gate, "build_execution_prompt", side_effect=AssertionError("task rendering")):
        stdout = StringIO()
        with redirect_stdout(stdout):
            assert gate.main(["--now", "2026-09-08T21:32:00Z"]) == 0
        assert stdout.getvalue() == ""
    assert diag.root_snapshot(runtime, runtime, home, "2026-09-08")["root_matches_runtime"]
    monkeypatch.setenv("SPORTS_PICKS_ROOT", str(runtime))
    assert diag.root_snapshot(runtime, cwd, home, "2026-09-08")["root_matches_runtime"]


def test_missing_db_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        diag.execution_window(path, "poller", "2026-09-08T21:00Z", "2026-09-08T22:00Z")
    assert not path.exists()


def test_execution_window_joins_job_and_normalizes_offsets_without_claiming_dispatch(tmp_path):
    path = tmp_path / "executions.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE executions (id, job_id, status, claimed_at, started_at, finished_at)")
        db.executemany("INSERT INTO executions VALUES (?,?,?,?,?,?)", [
            ("inside", "poller", "completed", "2026-09-08T16:30:00-05:00", "2026-09-08T16:31:29-05:00", None),
            ("end", "poller", "completed", "2026-09-08T22:40:00Z", "2026-09-08T22:40:00Z", None),
            ("other", "review", "completed", "2026-09-08T21:32:00Z", "2026-09-08T21:32:00Z", None),
            ("claim", "poller", "claimed", "2026-09-08T21:33:00Z", None, None),
        ])
    before = path.read_bytes()
    report = diag.execution_window(path, "poller", "2026-09-08T21:31:29Z", "2026-09-08T22:40:00Z")
    assert [r["id"] for r in report["runs"]] == ["inside", "claim"]
    assert [r["window_basis"] for r in report["runs"]] == ["started_at", "claimed_at"]
    assert report["agent_dispatch"] == report["order_outcome"] == "unknown"
    assert path.read_bytes() == before


@pytest.mark.parametrize("since,until", [
    ("2026-09-08T21:00", "2026-09-08T22:00Z"),
    ("2026-09-08T22:00Z", "2026-09-08T21:00Z"),
])
def test_ambiguous_or_reversed_window_refused(tmp_path, since, until):
    with pytest.raises(ValueError):
        diag.execution_window(tmp_path / "absent", "poller", since, until)


@pytest.mark.parametrize("changes", [
    {"raw_probability": .56}, {"conservative_probability": .54},
    {"uncertainty_haircut": .02}, {"dk_fair_prob": True},
    {"raw_probability": float("nan")}, {"conservative_probability": float("inf")},
    {"raw_probability": None}, {"raw_probability": 1.1},
    {"probability_components": {"adjustments": [{"delta": 0}], "haircuts": []}},
    {"probability_components": {"adjustments": [], "haircuts": [{"amount": 0}]}},
    {"probability_components": {}},
])
def test_market_identity_cannot_be_established_by_label(changes):
    assert diag.market_identity(market(**changes))["status"] == "contradicted"


def test_valid_market_and_non_market_are_distinct():
    assert diag.market_identity(market()) == {"status": "consistent", "errors": []}
    assert diag.market_identity(market(model_version="challenger"))["status"] == "not_applicable"


def test_cli_unavailable_store_is_incomplete_not_no_runs(tmp_path, monkeypatch, capsys):
    home, runtime, _, cwd = roots(tmp_path)
    monkeypatch.delenv("SPORTS_PICKS_ROOT", raising=False)
    assert diag.main(["--runtime-root", str(runtime), "--script-cwd", str(cwd),
                      "--home", str(home), "--day", "2026-09-08",
                      "--execution-db", str(tmp_path / "absent.db"), "--job-id", "poller",
                      "--since", "2026-09-08T21:31:29Z", "--until", "2026-09-08T22:40Z"]) == 2
    assert json.loads(capsys.readouterr().out)["evidence_complete"] is False


@pytest.mark.parametrize("started,claimed", [
    (None, None), (None, 123), (None, ""),
    (None, "invalid"), (None, "2026-09-08T21:32:00"),
    ("", "2026-09-08T21:32:00Z"), (0, "2026-09-08T21:32:00Z"),
])
def test_cli_invalid_execution_timestamp_is_incomplete(tmp_path, monkeypatch, capsys, started, claimed):
    home, runtime, _, cwd = roots(tmp_path)
    monkeypatch.delenv("SPORTS_PICKS_ROOT", raising=False)
    path = tmp_path / "executions.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE executions (id, job_id, status, claimed_at, started_at, finished_at)")
        db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?)",
                   ("broken", "poller", "completed", claimed, started, None))
    before = path.read_bytes()
    assert diag.main(["--runtime-root", str(runtime), "--script-cwd", str(cwd),
                      "--home", str(home), "--day", "2026-09-08",
                      "--execution-db", str(path), "--job-id", "poller",
                      "--since", "2026-09-08T21:31:29Z", "--until", "2026-09-08T22:40Z"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["evidence_complete"] is False
    assert report["error"]
    assert "execution_windows" not in report
    assert path.read_bytes() == before
