"""Inventory identity, fixed batches and strict retained-data replay."""

import json
from copy import deepcopy
from pathlib import Path
import pytest
import mlb_history_batches as m
from test_mlb_market_free_checkpoint import game, schedule


def test_months_cover_only_declared_training_window():
    bounds = list(m.months("2023-03-01", "2024-09-30"))
    assert len(bounds) == 19 and bounds[0] == ("2023-03-01", "2023-03-31")
    assert bounds[-1] == ("2024-09-01", "2024-09-30")
    assert ("2024-02-01", "2024-02-29") in bounds


def test_monthly_census_preserves_original_pointers_and_hash():
    a = json.loads(schedule("2023-04-01", [game("2023-04-01")]))
    b = json.loads(schedule("2023-04-02", [game("2023-04-02")]))
    raw = m.encoded(dict(dates=a["dates"] + b["dates"], totalGames=2))
    rows = m.monthly_games(raw, "2023-04-01", "2023-04-30")
    assert rows[1]["source_pointer"] == "/dates/1/games/0"
    assert all(r["schedule_sha256"] == m.sha(raw) for r in rows)
    with pytest.raises(ValueError, match="monthly_date"):
        m.monthly_games(raw, "2023-04-02", "2023-04-30")


def test_duplicate_monthly_date_refused():
    a = json.loads(schedule("2023-04-01", [game("2023-04-01")]))
    raw = m.encoded(dict(dates=a["dates"] * 2, totalGames=2))
    with pytest.raises(ValueError, match="monthly_date"):
        m.monthly_games(raw, "2023-04-01", "2023-04-30")


def test_initialize_refuses_changed_plan_and_unplanned_files(tmp_path):
    root = tmp_path / "run"
    m.initialize(root, {"x": 1})
    m.initialize(root, {"x": 1})
    with pytest.raises(ValueError, match="sealed_bytes_changed"):
        m.initialize(root, {"x": 2})
    root = tmp_path / "other"
    root.mkdir()
    (root / "foreign").write_text("x")
    with pytest.raises(ValueError, match="unplanned_existing_files"):
        m.initialize(root, {})


def seed_batch(monkeypatch, tmp_path):
    g = m.monthly_games(
        schedule("2023-04-01", [game("2023-04-01")]), "2023-04-01", "2023-04-30"
    )[0]
    target = dict(game_id=g["game_id"], occurrences=[g], snapshot_refusal=None)
    source = dict(batches=1, targets=[target])
    monkeypatch.setattr(m, "inventory", lambda *args: source)
    return target


def test_sealed_failure_never_retries_and_corruption_refuses(tmp_path, monkeypatch):
    target = seed_batch(monkeypatch, tmp_path)
    calls = []

    def fail(root, kind, key, url):
        calls.append(url)
        r = dict(
            kind=kind,
            key=key,
            url=url,
            retrieved_at_local="2026-09-14T00:00:00Z",
            completed_at_local="2026-09-14T00:00:01Z",
            http_status=None,
            failure="timeout",
            size=0,
            body_sha256=None,
        )
        (root / f"{kind}-{key}.json").write_bytes(m.encoded(r))

    monkeypatch.setattr(m.probe, "fetch", fail)
    root = tmp_path / "batch"
    r = m.batch(tmp_path, root, 0, True)
    assert len(calls) == 1 and r["summary"]["feeds"] == 0
    assert m.batch(tmp_path, root, 0, True) == r and len(calls) == 1
    assert m.batch(tmp_path, root, 0, False) == r
    p = root / f'feed-{target["game_id"]}.json'
    v = json.loads(p.read_text())
    v["url"] = "https://wrong"
    p.write_bytes(m.encoded(v))
    with pytest.raises(ValueError, match="receipt_identity"):
        m.batch(tmp_path, root, 0, True)
    assert len(calls) == 1


def test_unknown_receipt_blocks_before_network(tmp_path, monkeypatch):
    seed_batch(monkeypatch, tmp_path)
    root = tmp_path / "batch"

    def no_network(*args):
        raise AssertionError("unexpected fetch")

    # A foreign file must fail initialization, before any request.
    root.mkdir()
    (root / "feed-999.json").write_text("{}")
    monkeypatch.setattr(m.probe, "fetch", no_network)
    with pytest.raises(ValueError, match="unplanned_existing_files"):
        m.batch(tmp_path, root, 0, True)


def test_monthly_inventory_roundtrip_is_exact_without_network(tmp_path, monkeypatch):
    def fetch(root, kind, key, url):
        body = m.encoded(dict(dates=[], totalGames=0))
        digest = m.sha(body)
        (root / "objects" / digest).write_bytes(body)
        r = dict(
            kind=kind,
            key=key,
            url=url,
            retrieved_at_local="2026-09-14T00:00:00Z",
            completed_at_local="2026-09-14T00:00:01Z",
            size=len(body),
            http_status=200,
            failure=None,
            body_sha256=digest,
        )
        (root / f"{kind}-{key}.json").write_bytes(m.encoded(r))

    monkeypatch.setattr(m.probe, "fetch", fetch)
    first = m.inventory(tmp_path / "inventory", True)

    def forbidden(*args):
        raise AssertionError("network during replay")

    monkeypatch.setattr(m.probe, "fetch", forbidden)
    assert m.inventory(tmp_path / "inventory", False) == first
    assert m.inventory(tmp_path / "inventory", True) == first


def test_interrupted_batch_resumes_cached_failure_without_retry(tmp_path, monkeypatch):
    target = seed_batch(monkeypatch, tmp_path)
    calls = []

    def interrupted(root, kind, key, url):
        calls.append(url)
        r = dict(
            kind=kind,
            key=key,
            url=url,
            retrieved_at_local="2026-09-14T00:00:00Z",
            completed_at_local="2026-09-14T00:00:01Z",
            http_status=None,
            failure="timeout",
            size=0,
            body_sha256=None,
        )
        (root / f"{kind}-{key}.json").write_bytes(m.encoded(r))
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(m.probe, "fetch", interrupted)
    root = tmp_path / "run"
    with pytest.raises(RuntimeError, match="interruption"):
        m.batch(tmp_path, root, 0, True)
    assert not (root / "report.json").exists()
    result = m.batch(tmp_path, root, 0, True)
    assert result["summary"]["feeds"] == 0 and len(calls) == 1


def test_concurrent_batch_refuses_before_fetch(tmp_path, monkeypatch):
    import fcntl

    root = tmp_path / "run"
    with (tmp_path / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            m.batch(tmp_path, root, 0, True)
