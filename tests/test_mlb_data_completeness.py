import copy
import datetime as dt
import json
from pathlib import Path
from unittest import mock

import pytest
from scripts import mlb_data_completeness as data
from scripts import mlb_stage2_scan as scan

NOW = dt.datetime(2026, 9, 9, 22, tzinfo=dt.timezone.utc)


def game():
    return {"gamePk": 1, "gameDate": "2026-09-09T23:00:00Z", "teams": {
        "away": {"team": {"id": 109, "abbreviation": "AZ"}},
        "home": {"team": {"id": 118, "abbreviation": "KC"}}}}


def feed():
    g = game()
    teams = {}
    for side, offset in (("away", 0), ("home", 10)):
        ids = list(range(1 + offset, 10 + offset))
        teams[side] = {"team": g["teams"][side]["team"], "battingOrder": ids,
            "players": {f"ID{p}": {"person": {"id": p, "fullName": f"Player {p}"},
                "parentTeamId": g["teams"][side]["team"]["id"],
                "battingOrder": str(slot * 100), "gameStatus": {"isSubstitute": False}}
                for slot, p in enumerate(ids, 1)}}
    return {"gamePk": 1, "gameData": {"teams": {s: g["teams"][s]["team"] for s in teams},
        "datetime": {"dateTime": g["gameDate"]},
        "status": {"abstractGameState": "Preview", "detailedState": "Pre-Game"}},
        "liveData": {"boxscore": {"teams": teams}}}


def row():
    r = {"game_pk": 1, "event_id": "100", "away": "Arizona", "home": "Kansas City", "time": game()["gameDate"]}
    for side in ("away", "home"):
        r.update({f"{side}_abbr": game()["teams"][side]["team"]["abbreviation"],
            f"{side}_team_id": game()["teams"][side]["team"]["id"],
            f"{side}_lineup": data.lineup_from_feed(feed(), game(), side, NOW.isoformat()),
            f"{side}_offense": {"woba": .3, "xwoba": .32},
            f"{side}_starter": "Starter", f"{side}_starter_stats": {"era": 3., "whip": 1., "fip": 3., "k_bb_pct": 12.},
            f"{side}_bullpen": {"era": 3., "whip": 1., "ip": 20.},
            f"{side}_form": {"n": 7, "w": 4, "l": 3, "rf": 30, "ra": 25},
            f"{side}_injuries": [], f"{side}_fair": .5})
    r["data_completeness"] = data.assess(r, NOW)
    return r


@pytest.mark.parametrize("aliases", [("AZ", "ARI"), ("WSH", "WSN"), ("CHW", "CWS")])
def test_aliases_normalize_both_lookup_directions(aliases):
    for source in aliases:
        offense = data.canonical_offense({source: {"woba": .32}})
        for consumer in aliases:
            assert offense[data.canonical_team(consumer)] == {"woba": .32}


@pytest.mark.parametrize("alias", [None, "", "ZZZ", "ARI-invalid", 109])
def test_unknown_alias_fails_closed(alias):
    with pytest.raises(ValueError):
        data.canonical_team(alias)


def test_conflicting_aliases_fail_closed():
    with pytest.raises(ValueError, match="conflicting"):
        data.canonical_offense({"AZ": {"woba": .3}, "ARI": {"woba": .4}})


@pytest.mark.parametrize("mutation", ["empty", "eight", "duplicate", "wrong_team", "wrong_game", "wrong_slot", "substitute", "started", "wrong_time"])
def test_confirmation_requires_complete_corroborated_pregame_order(mutation):
    f = feed()
    b = f["liveData"]["boxscore"]["teams"]["away"]
    if mutation == "empty": b["battingOrder"] = []
    if mutation == "eight": b["battingOrder"].pop()
    if mutation == "duplicate": b["battingOrder"][-1] = 1
    if mutation == "wrong_team": b["team"] = {"id": 99}
    if mutation == "wrong_game": f["gamePk"] = 2
    if mutation == "wrong_slot": b["players"]["ID1"]["battingOrder"] = "200"
    if mutation == "substitute": b["players"]["ID1"]["gameStatus"]["isSubstitute"] = True
    if mutation == "started": f["gameData"]["status"]["abstractGameState"] = "Live"
    if mutation == "wrong_time": f["gameData"]["datetime"]["dateTime"] = "2026-09-09T23:30Z"
    result = data.lineup_from_feed(f, game(), "away", NOW.isoformat())
    assert result["state"] == "unconfirmed"
    assert not result["players"]


def test_confirmed_order_and_freshness_boundaries():
    r = row()
    assert data.assess(r, NOW)["status"] == "ready_for_evaluation"
    assert data.assess(r, NOW + dt.timedelta(seconds=1800))["status"] == "ready_for_evaluation"
    for when in (NOW - dt.timedelta(seconds=1), NOW + dt.timedelta(seconds=1801)):
        assert data.assess(r, when)["status"] == "incomplete_input_data"
    r["away_lineup"]["players"] = []
    assert "invalid_lineup_players" in data.assess(r, NOW)["lineup_errors"]["away"]


def test_price_absence_is_separate_from_missing_inputs_and_no_automatic_pass():
    r = row()
    r["away_fair"] = None
    assert data.assess(r, NOW)["status"] == "not_priced"
    r["away_offense"] = None
    a = data.assess(r, NOW)
    assert a["status"] == "incomplete_input_data"
    assert not a["prices_available"]
    assert a["missing_fields"] == ["away_offense"]


def test_historical_missing_data_is_preserved_and_explicit():
    rows = json.loads((Path(__file__).parent / "fixtures/mlb-data-completeness/stage2-2026-09-09.json").read_text())
    assert len(rows) == 15
    arizona = next(r for r in rows if r["away_abbr"] == "ARI")
    assert arizona["away_offense"] is None
    assert all("away_lineup" not in r and "home_lineup" not in r for r in rows)
    receipt = data.coverage(rows, NOW, scheduled_games=15, schedule_verified=True)
    assert receipt["reconciled"]
    assert receipt["complete_reads"] == 0
    assert receipt["missing_fields"]["away_offense"] == 1
    assert receipt["unpriced_games"] == 2
    assert sum(receipt["status_counts"].values()) == 15


def test_receipt_cannot_certify_unknown_or_duplicate_schedule():
    assert not data.coverage([], NOW)["reconciled"]
    assert not data.coverage([row(), row()], NOW, scheduled_games=2, schedule_verified=True)["reconciled"]
    assert data.coverage([], NOW, scheduled_games=0, schedule_verified=True)["reconciled"]


def test_pass_checked_against_source_and_current_freshness_not_summary():
    r = row()
    read = {k: r[k] for k in ("game_pk", "event_id", "away", "home")}
    read["disposition"] = "pass"
    assert not data.read_disposition_errors([read], [r], NOW)
    assert data.read_disposition_errors([read], [r], NOW + dt.timedelta(seconds=1801))
    r["away_offense"] = None  # recorded summary still says ready
    assert data.read_disposition_errors([read], [r], NOW)
    read["disposition"] = "incomplete_input_data"
    assert not data.read_disposition_errors([read], [r], NOW)


def test_injury_outage_is_not_an_empty_healthy_roster():
    with mock.patch.object(scan, "get", side_effect=TimeoutError("outage")):
        with pytest.raises(TimeoutError):
            scan.MlbSlateCollector("2026-09-09", 2026).injuries("109")


def test_injury_response_does_not_silently_truncate_after_ten():
    def get(url):
        if "/injuries?" in url: return {"items": [{"$ref": f"https://injury/{i}"} for i in range(12)], "count": 12}
        if "injury/" in url: return {"athlete": {"$ref": "https://athlete/1"}, "status": "Out"}
        return {"displayName": "Player"}
    with mock.patch.object(scan, "get", side_effect=get):
        assert len(scan.MlbSlateCollector("2026-09-09", 2026).injuries("109")) == 12


def collect_sources(espn, games, **patches):
    from tests.test_mlb_stage2_scan import espn_event
    collector = scan.MlbSlateCollector("2026-09-09", 2026)
    def get(url):
        if "scoreboard" in url:
            if isinstance(espn, Exception): raise espn
            return {"events": espn}
        if "/schedule?" in url:
            if isinstance(games, Exception): raise games
            return {"dates": [{"games": games}], "totalGames": len(games)}
        return feed()
    with mock.patch.object(scan, "get", side_effect=get), \
            mock.patch.object(scan, "team_offense_quality", return_value={"AZ": {"woba": .3, "xwoba": .32}, "KC": {"woba": .3, "xwoba": .32}}), \
            mock.patch.object(scan, "utc_now", return_value=NOW), \
            mock.patch.object(scan.MlbSlateCollector, "team_form", return_value={"n": 7, "w": 4, "l": 3, "rf": 30, "ra": 25}), \
            mock.patch.object(scan.MlbSlateCollector, "bullpen", return_value={"era": 3., "whip": 1., "ip": 20.}), \
            mock.patch.object(scan.MlbSlateCollector, "pitcher_stats", side_effect=patches.get("pitcher", lambda _: {"era": 3., "whip": 1., "fip": 3., "k_bb_pct": 12.})), \
            mock.patch.object(scan.MlbSlateCollector, "injuries", side_effect=patches.get("injuries", lambda _: [])):
        rows = collector.collect()
    return collector, rows


def event():
    from tests.test_mlb_stage2_scan import espn_event
    return espn_event("100", "ARI", "KC", game()["gameDate"])


def test_collector_preserves_independent_data_on_one_field_failure():
    def pitcher(_): raise TimeoutError("stats unavailable")
    g = game()
    for side in ("away", "home"):
        g["teams"][side]["probablePitcher"] = {"id": 1, "fullName": "Starter"}
    collector, rows = collect_sources([event()], [g], pitcher=pitcher)
    assert len(rows) == 1
    assert rows[0]["away_offense"]["woba"] == .3
    assert rows[0]["away_lineup"]["state"] == "confirmed"
    assert rows[0]["away_starter_stats"] is None
    assert collector.coverage["reconciled"]
    assert collector.coverage["source_failure_games"] == 1


def test_schedule_outage_is_unknown_not_an_honest_zero():
    collector, rows = collect_sources([], TimeoutError("schedule down"))
    assert rows == []
    assert collector.coverage["scheduled_games"] is None
    assert not collector.coverage["reconciled"]
    assert collector.coverage["source_failures"]


def test_scoreboard_outage_preserves_mlb_schedule():
    collector, rows = collect_sources(TimeoutError("scoreboard down"), [game()])
    assert [r["game_pk"] for r in rows] == [1]
    assert collector.coverage["scheduled_games"] == 1
    assert collector.coverage["complete_reads"] == 0
    assert collector.coverage["source_failure_games"] == 1


def test_unknown_join_alias_does_not_match_itself():
    g = game()
    g["teams"]["away"]["team"]["abbreviation"] = "ZZZ"
    e = event()
    e["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "ZZZ"
    collector, rows = collect_sources([e], [g])
    assert len(rows) == 2
    assert not collector.coverage["reconciled"]
    assert all(r.get("error") for r in rows)


def test_coverage_sidecar_is_bound_to_scan_bytes(tmp_path):
    import hashlib
    p = tmp_path / "scan.json"
    p.write_text(json.dumps([row()]))
    r = data.coverage([row()], NOW, scheduled_games=1, schedule_verified=True)
    r["scan_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    p.with_suffix(".coverage.json").write_text(json.dumps(r))
    assert data.coverage_for_scan(p, [row()], NOW)["reconciled"]
    from scripts import mlb_slate_writer as writer
    assert writer.load_scan(p)[0][0]["game_pk"] == 1
    changed = row()
    changed["away_offense"]["woba"] = .31
    p.write_text(json.dumps([changed]))
    assert data.coverage([changed], NOW, scheduled_games=1, schedule_verified=True)["reconciled"]
    assert not data.coverage_for_scan(p, [changed], NOW)["reconciled"]
    with pytest.raises(writer.SlateWriteError, match="cannot reconcile"):
        writer.load_scan(p)
    # Rebinding precisely these bytes restores admission: the digest, not
    # changed cardinality or identity, was the sole failing condition.
    r["scan_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    p.with_suffix(".coverage.json").write_text(json.dumps(r))
    assert data.coverage_for_scan(p, [changed], NOW)["reconciled"]
    assert writer.load_scan(p)[0] == [changed]


@pytest.mark.parametrize("disposition", ["candidate", "pass"])
def test_stale_lineup_cannot_be_landed_as_complete(disposition):
    r = row()
    read = {k: r[k] for k in ("game_pk", "event_id", "away", "home")}
    read["disposition"] = disposition
    assert data.read_disposition_errors([read], [r], NOW + dt.timedelta(seconds=1801))


def test_existing_lineup_only_watchlist_exception_is_preserved():
    r = row()
    r["away_lineup"] = None
    read = {k: r[k] for k in ("game_pk", "event_id", "away", "home")}
    read["disposition"] = "lineup_watchlist"
    assert not data.read_disposition_errors([read], [r], NOW)
    r["home_offense"] = None
    assert data.read_disposition_errors([read], [r], NOW)


def test_csv_retries_transient_failure_and_normalizes_savant_keys(tmp_path):
    import io
    import urllib.error
    import urllib.request
    with mock.patch.object(scan, "SAVANT_CACHE", tmp_path), \
            mock.patch.object(urllib.request, "urlopen", side_effect=[urllib.error.URLError("reset"), io.BytesIO(b"team_id,woba,est_woba\nAZ,.310,.320\n")]) as fetch, \
            mock.patch("time.sleep"):
        assert scan.team_offense_quality(2026)["ARI"] == {"woba": .31, "xwoba": .32}
        assert fetch.call_count == 2
        assert scan.team_offense_quality(2026)["ARI"]["woba"] == .31
        assert fetch.call_count == 2  # cache normalized too


def test_csv_permanent_error_is_reported_without_retry(tmp_path):
    import urllib.error
    import urllib.request
    with mock.patch.object(scan, "SAVANT_CACHE", tmp_path), \
            mock.patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError("https://source", 403, "blocked", {}, None)) as fetch:
        with pytest.raises(urllib.error.HTTPError): scan.team_offense_quality(2026)
        assert fetch.call_count == 1


def test_writer_invokes_source_gate_before_schedule_write(tmp_path):
    from scripts import mlb_slate_writer as writer
    r = row()
    read = {k: r[k] for k in ("game_pk", "event_id", "away", "home")}
    read["disposition"] = "pass"
    r["away_lineup"] = None
    path = writer.denominator_output_path("2026-09-09", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([r]))
    import hashlib
    receipt = data.coverage([r], NOW, scheduled_games=1, schedule_verified=True)
    receipt["scan_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".coverage.json").write_text(json.dumps(receipt))
    # Bypass unrelated draft/record validation so only the real source gate
    # can stop this write. Removing its call makes this test reach compose.
    with mock.patch.object(writer, "draft_errors", return_value=[]), \
            mock.patch.object(writer, "load_existing", return_value=(None, None)), \
            mock.patch.object(writer, "compose", return_value={}), \
            mock.patch.object(writer, "record_errors", return_value=[]):
        with pytest.raises(writer.SlateWriteError, match="pass cannot describe"):
            writer.land(tmp_path, "2026-09-09", {"game_reads": [read]})
    assert not (tmp_path / ".picks/execute/2026-09-09-schedule.json").exists()


def test_failed_schedule_cannot_be_landed_as_zero(tmp_path):
    import hashlib
    from scripts import mlb_slate_writer as writer
    path = writer.denominator_output_path("2026-09-09", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    receipt = data.coverage([], NOW)
    receipt["scan_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".coverage.json").write_text(json.dumps(receipt))
    with pytest.raises(writer.SlateWriteError, match="cannot reconcile"):
        writer.skeleton(tmp_path, "2026-09-09")


def test_versioned_scan_requires_coverage_receipt(tmp_path):
    from scripts import mlb_slate_writer as writer
    p = tmp_path / "scan.json"
    p.write_text(json.dumps([row()]))
    with pytest.raises(writer.SlateWriteError, match="cannot reconcile"):
        writer.load_scan(p)


def test_incomplete_disposition_requires_named_rail():
    from scripts import mlb_game_reads
    assert not mlb_game_reads._disposition_errors("read", {"disposition": "incomplete_input_data", "refusing_rails": ["incomplete_input_data"]})
    assert mlb_game_reads._disposition_errors("read", {"disposition": "incomplete_input_data", "refusing_rails": ["starter_floor"]})


@pytest.mark.parametrize("source_status", ["ready_for_evaluation", "not_priced", "incomplete_input_data", "missing_and_unpriced"])
@pytest.mark.parametrize("disposition", ["pass", "not_priced", "incomplete_input_data"])
def test_refusal_classifications_correspond_through_real_writer(tmp_path, source_status, disposition):
    import hashlib
    import vig_policy_state
    from scripts import mlb_slate_writer as writer
    from tests.test_mlb_slate_writer import draft_for, read_for

    r = row()
    if source_status in ("incomplete_input_data", "missing_and_unpriced"):
        r["away_offense"] = None
    if source_status in ("not_priced", "missing_and_unpriced"):
        r["away_fair"] = r["home_fair"] = None
    expected = data.assess(r, NOW)["status"]
    # Keep the recorded summary saying ready: admission must recompute.
    path = writer.denominator_output_path("2026-09-09", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([r]))
    receipt = data.coverage([r], NOW, scheduled_games=1, schedule_verified=True)
    receipt["scan_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".coverage.json").write_text(json.dumps(receipt))
    rails = {"pass": "price_discipline", "not_priced": "no_dk_price", "incomplete_input_data": "incomplete_input_data"}
    read = read_for(r, disposition=disposition, refusing_rails=[rails[disposition]])
    draft = draft_for([r], date="2026-09-09", game_reads=[read])
    accepted = (disposition == "pass" and expected == "ready_for_evaluation") or disposition == expected
    with vig_policy_state.deployed_policy(tmp_path / "state"), \
            mock.patch("mlb_data_completeness.utc_now", return_value=NOW):
        if accepted:
            landed, result = writer.land(tmp_path, "2026-09-09", draft)
            assert landed.exists()
            assert result["game_reads"][0]["disposition"] == disposition
        else:
            with pytest.raises(writer.SlateWriteError, match=f"{disposition} cannot describe {expected}"):
                writer.land(tmp_path, "2026-09-09", draft)
            assert not (tmp_path / ".picks/execute/2026-09-09-schedule.json").exists()
