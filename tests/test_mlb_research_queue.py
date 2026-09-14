"""Research can recover inputs without granting pick/review/execution authority."""

import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mlb_research_queue as queue
import mlb_data_completeness as data
from test_mlb_data_completeness import row, NOW

DAY = NOW.date().isoformat()


def seed(root, rows=None):
    rows = [row()] if rows is None else rows
    p = root / ".picks/tmp" / f"stage2-{DAY}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(rows).encode()
    p.write_bytes(raw)
    p.with_suffix(".coverage.json").write_text(
        json.dumps(
            {
                "schema": data.SCHEMA,
                "scan_sha256": hashlib.sha256(raw).hexdigest(),
                "schedule_verified": True,
                "scheduled_games": len(rows),
            }
        )
    )
    return p


def incomplete():
    r = row()
    r["away_lineup"]["state"] = "unconfirmed"
    return r


def clock():
    return queue.instant(row()["time"]) - timedelta(minutes=100)


def current(r, now):
    for side in ("away", "home"):
        r[f"{side}_lineup"]["retrieved_at_utc"] = now.isoformat()
    return r


def test_recovers_lineups_without_touching_authoritative_schedule(tmp_path):
    p = seed(tmp_path, [incomplete()])
    original = p.read_bytes()

    def refresh(r, now):
        return current(row(), now), [{"normalized_response": "fixture"}]

    result = queue.run_cycle(tmp_path, DAY, now=clock(), refresh_fn=refresh)
    item = result["games"]["1"]
    assert item["status"] == "ready_for_producer"
    assert len(item["attempts"]) == 1
    assert p.read_bytes() == original
    assert not (tmp_path / ".picks/execute").exists()
    handoff = json.loads(
        (tmp_path / ".picks/research" / DAY / "producer-handoff.json").read_text()
    )
    assert handoff["execution_enabled"] is False
    assert len(handoff["games"]) == 1
    evidence = tmp_path / ".picks/research" / DAY / item["attempts"][0]["evidence"]
    assert evidence.exists()


def test_not_yet_due_and_retry_interval_are_enforced(tmp_path):
    seed(tmp_path, [incomplete()])
    calls = []

    def refresh(r, now):
        calls.append(now)
        return r, []

    first = queue.instant(row()["time"]) - timedelta(hours=4)
    queue.run_cycle(tmp_path, DAY, now=first, refresh_fn=refresh)
    assert not calls
    queue.run_cycle(tmp_path, DAY, now=clock(), refresh_fn=refresh)
    queue.run_cycle(
        tmp_path, DAY, now=clock() + timedelta(minutes=14), refresh_fn=refresh
    )
    assert len(calls) == 1
    queue.run_cycle(
        tmp_path, DAY, now=clock() + timedelta(minutes=15), refresh_fn=refresh
    )
    assert len(calls) == 2


def test_provider_failure_is_retained_and_budget_is_bounded(tmp_path, monkeypatch):
    seed(tmp_path, [incomplete()])
    monkeypatch.setattr(queue, "MAX_ATTEMPTS", 2)

    def fail(*args):
        raise OSError("offline fixture")

    queue.run_cycle(tmp_path, DAY, now=clock(), refresh_fn=fail)
    state = queue.run_cycle(
        tmp_path, DAY, now=clock() + timedelta(minutes=15), refresh_fn=fail
    )
    assert state["games"]["1"]["status"] == "exhausted"
    assert len(state["games"]["1"]["attempts"]) == 2
    assert "offline fixture" in state["games"]["1"]["attempts"][0]["error"]


@pytest.mark.parametrize("mutation", ["start", "game", "team"])
def test_refresh_cannot_replace_identity(tmp_path, mutation):
    seed(tmp_path, [incomplete()])

    def corrupt(r, now):
        if mutation == "start":
            r["time"] = (queue.instant(r["time"]) + timedelta(minutes=1)).isoformat()
        elif mutation == "game":
            r["game_pk"] = 2
        else:
            r["away"] = "Other"
        return r, []

    state = queue.run_cycle(tmp_path, DAY, now=clock(), refresh_fn=corrupt)
    assert state["games"]["1"]["status"] == "pending"
    assert state["games"]["1"]["attempts"][0]["status"] == "error"


def test_expiry_with_missing_source_clears_handoff(tmp_path):
    p = seed(tmp_path, [current(row(), clock())])
    queue.run_cycle(tmp_path, DAY, now=clock())
    p.unlink()
    state = queue.run_cycle(tmp_path, DAY, now=queue.instant(row()["time"]))
    assert state["games"]["1"]["status"] == "expired"
    handoff = json.loads(
        (tmp_path / ".picks/research" / DAY / "producer-handoff.json").read_text()
    )
    assert handoff["games"] == []


def test_occupied_game_never_researched(tmp_path):
    seed(tmp_path, [incomplete()])
    p = tmp_path / ".picks/execute" / f"{DAY}-schedule.json"
    p.parent.mkdir()
    raw = json.dumps({"candidates": [{"game_pk": 1}], "lineup_watchlist": []})
    p.write_text(raw)

    def fail(*args):
        pytest.fail("occupied game refreshed")

    state = queue.run_cycle(tmp_path, DAY, now=clock(), refresh_fn=fail)
    assert state["games"]["1"]["status"] == "occupied"
    assert p.read_text() == raw


def test_source_hash_mismatch_and_duplicates_refused(tmp_path):
    p = seed(tmp_path)
    p.write_text("[]")
    with pytest.raises(ValueError, match="byte-bound"):
        queue.run_cycle(tmp_path, DAY, now=clock())
    seed(tmp_path, [row(), row()])
    with pytest.raises(ValueError):
        queue.run_cycle(tmp_path, DAY, now=clock())


def test_changed_start_does_not_reset_budget_or_research_wrong_game(tmp_path):
    seed(tmp_path, [incomplete()])
    queue.run_cycle(tmp_path, DAY, now=clock(), refresh_fn=lambda r, n: (r, []))
    changed = incomplete()
    changed["time"] = (
        queue.instant(changed["time"]) + timedelta(minutes=5)
    ).isoformat()
    seed(tmp_path, [changed])
    state = queue.run_cycle(tmp_path, DAY, now=clock() + timedelta(minutes=15))
    assert state["games"]["1"]["status"] == "invalid_identity"
    assert len(state["games"]["1"]["attempts"]) == 1


def test_previous_day_expires_without_reading_corrupt_scan(tmp_path):
    p = seed(tmp_path, [incomplete()])
    queue.run_cycle(tmp_path, DAY, now=clock(), refresh_fn=lambda r, n: (r, []))
    p.write_text("broken")
    queue.expire_previous(
        tmp_path,
        (clock() + timedelta(days=1)).date().isoformat(),
        now=clock() + timedelta(days=1),
    )
    state = json.loads((tmp_path / ".picks/research" / DAY / "queue.json").read_text())
    assert state["games"]["1"]["status"] == "expired"


def test_live_refresh_keeps_retained_field_timestamps(tmp_path, monkeypatch):
    from test_mlb_data_completeness import feed

    r = incomplete()
    r["retrieved_at_utc"] = NOW.isoformat()
    f = feed()
    f["gameData"]["probablePitchers"] = {
        s: {"fullName": "Starter"} for s in ("away", "home")
    }
    monkeypatch.setattr(queue.scanner, "get", lambda url: f)
    result, responses = queue.refresh(
        r,
        clock(),
        fetch=lambda url, timeout, transport=queue.scanner.get: transport(url),
    )
    assert result["retrieved_at_utc"] == NOW.isoformat()
    assert result["away_offense"] == r["away_offense"]
    assert (
        result["research_field_observations"]["away_lineup"]
        == responses[0]["retrieved_at_utc"]
    )
    assert len(responses) == 1


def test_real_refresh_refuses_wrong_provider_game(tmp_path, monkeypatch):
    from test_mlb_data_completeness import feed

    f = feed()
    f["gamePk"] = 999
    monkeypatch.setattr(queue.scanner, "get", lambda url: f)
    with pytest.raises(ValueError, match="identity changed"):
        queue.refresh(
            incomplete(),
            clock(),
            fetch=lambda url, timeout, transport=queue.scanner.get: transport(url),
        )


def test_review_runs_research_even_with_empty_card_and_reports_failure(
    tmp_path, monkeypatch
):
    import vig_review_gate_common as gate

    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "schedule_day_now", lambda: DAY)
    monkeypatch.setattr(gate, "_run_gate", lambda *args: 0)
    monkeypatch.setattr(gate, "write_slate_receipt", lambda *args: None)
    monkeypatch.setattr(gate, "write_decision_snapshot", lambda *args: None)
    calls = []
    monkeypatch.setattr(
        gate, "run_research_cycle", lambda root, day: calls.append((root, day))
    )
    assert gate.run_gate("MLB") == 0
    assert calls == [(tmp_path, DAY)]

    def fail(*args):
        raise ValueError("broken source")

    monkeypatch.setattr(gate, "run_research_cycle", fail)
    assert gate.run_gate("MLB") == 1


def test_missing_prices_and_injuries_use_correlated_espn_identity(monkeypatch):
    from test_mlb_data_completeness import feed

    r = incomplete()
    r["away_fair"] = None
    r["away_injuries"] = None
    f = feed()
    f["gameData"]["probablePitchers"] = {
        s: {"fullName": "Starter"} for s in ("away", "home")
    }
    event = {
        "id": r["event_id"],
        "date": r["time"],
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": s,
                        "team": {"id": str(i), "abbreviation": r[s + "_abbr"]},
                    }
                    for s, i in [("away", 101), ("home", 118)]
                ],
                "odds": [
                    {
                        "moneyline": {
                            "away": {"close": {"odds": "+100"}},
                            "home": {"close": {"odds": "-100"}},
                        }
                    }
                ],
            }
        ],
    }
    calls = []

    def get(url):
        calls.append(url)
        if "/feed/live" in url:
            return f
        if "scoreboard" in url:
            return {"events": [event]}
        return {"items": []}

    monkeypatch.setattr(queue.scanner, "get", get)
    result, responses = queue.refresh(
        r,
        clock(),
        fetch=lambda url, timeout, transport=queue.scanner.get: transport(url),
    )
    assert result["away_fair"] == 0.5
    assert result["away_injuries"] == []
    assert any("/teams/101/injuries" in url for url in calls)
    assert len(responses) == 3
    event["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "NYY"
    with pytest.raises(ValueError, match="team identity"):
        queue.refresh(
            r,
            clock(),
            fetch=lambda url, timeout, transport=queue.scanner.get: transport(url),
        )


def test_offense_refresh_uses_new_response_not_daily_cache(monkeypatch):
    from test_mlb_data_completeness import feed

    r = incomplete()
    r["away_offense"] = None
    f = feed()
    f["gameData"]["probablePitchers"] = {
        s: {"fullName": "Starter"} for s in ("away", "home")
    }
    monkeypatch.setattr(
        queue.scanner,
        "team_offense_quality",
        lambda *args: pytest.fail("cached source relabeled"),
    )

    def fetch(url, timeout):
        if "/feed/live" in url:
            return f
        return f'team_id,woba,est_woba\n{r["away_abbr"]},0.31,0.32\n'

    result, responses = queue.refresh(r, clock(), fetch=fetch)
    assert result["away_offense"] == {"woba": 0.31, "xwoba": 0.32}
    assert len(responses) == 2
