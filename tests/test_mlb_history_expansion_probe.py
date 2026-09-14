"""Bounded acquisition, deterministic offline replay, and snapshot refusals."""

import json
from copy import deepcopy

import pytest
import mlb_history_expansion_probe as m
from test_mlb_participant_history_experiment import fixture


def snapshot():
    g, f = fixture()
    # Existing fixture contains a 2025 game; the generalized probe must bind
    # the reported season to the selected date rather than hardcode a season.
    f["metaData"] = {"timeStamp": "20250421_180000"}
    f["gameData"]["status"] = {
        "abstractGameState": "Preview",
        "detailedState": "Scheduled",
    }
    f["gameData"]["probablePitchers"] = {"away": {"id": 10}, "home": {"id": 20}}
    return g, f


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("season", "snapshot_season_mismatch"),
        ("time", "snapshot_time_mismatch"),
        ("team", "snapshot_team_mismatch"),
        ("date", "snapshot_schedule_mismatch"),
        ("live", "snapshot_not_uninterrupted_pregame"),
        ("resumed", "snapshot_not_uninterrupted_pregame"),
        ("pitcher", "same_probable_pitcher"),
    ],
)
def test_snapshot_rejects_identity_or_timing_contradictions(mutation, reason):
    g, f = snapshot()
    assert m.check_snapshot(g, f, "20250421_180000") == {"away": "10", "home": "20"}
    if mutation == "season":
        f["gameData"]["game"]["season"] = "2024"
    if mutation == "time":
        f["metaData"]["timeStamp"] = "20250421_190000"
    if mutation == "team":
        f["gameData"]["teams"]["away"]["id"] = 999
    if mutation == "date":
        f["gameData"]["datetime"]["officialDate"] = "2025-04-22"
    if mutation == "live":
        f["gameData"]["status"]["abstractGameState"] = "Live"
    if mutation == "resumed":
        f["gameData"]["datetime"]["resumeDate"] = "2025-04-22"
    if mutation == "pitcher":
        f["gameData"]["probablePitchers"]["home"]["id"] = 10
    with pytest.raises(ValueError, match=reason):
        m.check_snapshot(g, f, "20250421_180000")


def test_earlier_season_allowed_only_when_identity_agrees():
    g, f = snapshot()
    g["source_date"] = g["raw_game"]["officialDate"] = "2023-04-21"
    g["scheduled_start"] = g["scheduled_start"].replace("2025", "2023")
    dt = f["gameData"]["datetime"]
    for k, v in list(dt.items()):
        if isinstance(v, str):
            dt[k] = v.replace("2025", "2023")
    f["gameData"]["game"]["season"] = "2023"
    f["metaData"]["timeStamp"] = "20230421_180000"
    assert m.check_snapshot(g, f, "20230421_180000")["away"] == "10"


def test_all_failed_dates_retained_and_replay_does_not_fetch(tmp_path, monkeypatch):
    root = tmp_path / "run"
    calls = []

    def failed(root, kind, key, url):
        calls.append(url)
        receipt = dict(
            kind=kind,
            key=key,
            url=url,
            retrieved_at_local="2026-09-14T00:00:00Z",
            completed_at_local="2026-09-14T00:00:01Z",
            size=0,
            http_status=None,
            failure="TimeoutError",
            body_sha256=None,
        )
        (root / f"{kind}-{key}.json").write_bytes(m.encoded(receipt))

    monkeypatch.setattr(m, "fetch", failed)
    result = m.probe(root, True)
    assert len(calls) == 6 and result["request_count"] == 6
    assert all(r["refusal"] == "source_unavailable" for r in result["dates"])
    assert all("/2025-" not in u for u in calls)

    def forbidden(*args):
        raise AssertionError("network during replay")

    monkeypatch.setattr(m, "fetch", forbidden)
    assert m.probe(root) == result
    with pytest.raises(FileExistsError):
        m.probe(root, True)
    receipt = root / "schedule-2023-04-17.json"
    data = json.loads(receipt.read_text())
    data["failure"] = "changed"
    receipt.write_bytes(m.encoded(data))
    with pytest.raises(ValueError, match="replay_changed"):
        m.probe(root)


def test_changed_objects_refused(tmp_path):
    (tmp_path / "objects").mkdir()
    body = b"{}"
    digest = m.sha(body)
    (tmp_path / "objects" / digest).write_bytes(b"[]")
    r = dict(
        kind="feed",
        key="1",
        url="url",
        retrieved_at_local="2026-09-14T00:00:00Z",
        completed_at_local="2026-09-14T00:00:01Z",
        size=2,
        http_status=200,
        failure=None,
        body_sha256=digest,
    )
    (tmp_path / "feed-1.json").write_bytes(m.encoded(r))
    with pytest.raises(ValueError, match="object_integrity"):
        m.retained(tmp_path, "feed", "1", "url", False, [])


def test_selection_time_must_be_strictly_before_cutoff():
    with pytest.raises(ValueError, match="no_provider_snapshot_before_cutoff"):
        m.selected_code(m.encoded(["20230421_180000"]), "2023-04-21T18:00:00Z")


def test_earlier_schedule_preserves_identity_counts_and_moved_dates():
    from test_mlb_market_free_checkpoint import game, schedule

    g = game("2023-04-17")
    parsed = m.schedule_census(schedule("2023-04-17", [g]), "2023-04-17")
    assert parsed[0]["source_date"] == "2023-04-17"
    g["officialDate"] = "2023-04-18"
    assert (
        m.schedule_census(schedule("2023-04-17", [g]), "2023-04-17")[0]["raw_game"][
            "officialDate"
        ]
        == "2023-04-18"
    )
    g["officialDate"] = "2024-04-18"
    with pytest.raises(ValueError, match="official_date"):
        m.schedule_census(schedule("2023-04-17", [g]), "2023-04-17")


def test_cache_validated_before_reuse(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "objects").mkdir()
    # A new root cannot silently accept unbound/missing cached schedules.
    with pytest.raises(ValueError, match="receipt_missing_or_symlink"):
        m.probe(tmp_path / "run", True, cache)
