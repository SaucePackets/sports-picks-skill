"""Synthetic NFL fixtures prove reachability, not a real betting recommendation."""
import copy
import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts import nfl_card_writer as writer

NOW = dt.datetime(2026, 9, 9, 20, tzinfo=dt.timezone.utc)
KICKOFF = "2026-09-10T00:20Z"


def context():
    row = {"event_id": "game-1", "event": "Synthetic Away at Synthetic Home", "time": KICKOFF,
           "context_version": 1, "collector_only": True, "collected_at": NOW.isoformat(),
           "season": 2026, "week": 1, "seasontype": 2, "away_id": "17", "home_id": "26",
           "away": "Synthetic Away", "home": "Synthetic Home", "away_ml": -400, "home_ml": 300,
           "venue": {"id": "3673", "indoor": True}, "weather": {"status": "not_applicable"}}
    for side, team in (("away", "17"), ("home", "26")):
        row[side + "_form"] = {"n": 5, "prior_season_games": 5, "discounted": True,
                               "prior_season_weight": 0.5, "weighted_pd": 20}
        row[side + "_qb"] = {"roster": {"status": "retrieved", "players": [
            {"athlete_id": team + "-qb", "position": "QB", "team_id": team}]},
            "depth_chart": {"quarterbacks": [{"name": "Synthetic QB", "athlete_id": team + "-qb"}]}}
        row[side + "_injury_evidence"] = {"status": "retrieved", "items": []}
    return row


def fixture():
    row = context()
    evidence = []
    def add(key, kind, teams, data, **extra):
        evidence.append({"id": key, "kind": kind, "event_id": row["event_id"], "team_ids": teams,
                         "kickoff_utc": KICKOFF, "observed_at": NOW.isoformat(),
                         "source": "https://example.org/synthetic-test-only/" + key,
                         "source_type": "official_team", "status": "available", "summary": "Synthetic test evidence",
                         "data": data, **extra})
    add("qb", "qb_confirmation", ["17"], {"athlete_id": "17-qb", "position": "QB", "confirmed_start": True, "status_resolved": True})
    add("away-availability", "player_availability", ["17"], {"relevant_status_resolved": True})
    add("home-availability", "player_availability", ["26"], {"relevant_status_resolved": True})
    add("efficiency", "efficiency", ["17", "26"], {})
    add("offseason-away", "offseason_review", ["17"], {})
    add("offseason-home", "offseason_review", ["26"], {})
    add("book", "sportsbook", ["17", "26"], {"away_price": 100, "home_price": -100})
    add("exchange", "exchange", ["17", "26"], {"market_type": "moneyline", "side": "away", "team_id": "17", "ask": 0.50, "contract_id": "synthetic-contract"})
    gates = {name: {"status": "pass", "reason": "Synthetic completed judgment", "evidence_refs": ["efficiency"]} for name in writer.GATES}
    gates["qb_status_gate"]["evidence_refs"] = ["qb"]
    gates["injury_cluster_gate"]["evidence_refs"] = ["away-availability", "home-availability"]
    gates["weather_gate"]["evidence_refs"] = ["scan.weather"]
    gates["week_to_week_overreaction_gate"]["evidence_refs"] = ["scan.away_form", "scan.home_form", "offseason-away", "offseason-home"]
    gates["price_discipline"]["evidence_refs"] = ["book", "exchange"]
    draft = {"version": 1, "run_id": "synthetic-run", "date": "2026-09-09", "assessments": [
        {"event_id": "game-1", "side": "away", "confidence": "Medium", "unit_size": 1,
         "win_probability": 0.53, "thesis": "Synthetic matchup thesis", "market_explanation": "",
         "sections": {name: "Synthetic completed " + name for name in writer.SECTIONS}, "gates": gates, "evidence": evidence}]}
    return [row], draft


def evidence(draft, key):
    return next(e for e in draft["assessments"][0]["evidence"] if e["id"] == key)


def outdoor(row):
    row["venue"]["indoor"] = False
    row["weather"] = {"status": "retrieved", "venue": {"id": "3673"}, "kickoff": KICKOFF,
        "latitude": 47.59, "longitude": -122.33, "retrieved_at": NOW.isoformat(),
        "units": {"temperature_2m": "°F", "wind_speed_10m": "mp/h", "wind_gusts_10m": "mp/h", "precipitation": "inch", "snowfall": "inch"},
        "samples": [{"time_utc": f"2026-09-10T0{i}:00Z", "temperature_2m": 70,
                     "wind_speed_10m": 7, "wind_gusts_10m": 10, "precipitation": 0, "snowfall": 0} for i in range(5)]}


def test_complete_assessment_reaches_manual_candidate_using_refreshed_price():
    scan, draft = fixture()
    schedule, report = writer.compose(scan, draft, NOW)
    candidate, = schedule["candidates"]
    assert schedule["analysis_status"] == "complete"
    assert candidate["price"] == 100  # scanner's stale -400 is never used
    assert candidate["dk_fair_prob"] == 0.5 and candidate["net_edge"] == pytest.approx(0.03)
    assert candidate["execution_mode"] == "manual" and candidate["status"] == "awaiting_jerry"
    assert candidate["executed"] is False and candidate["vig_approved"] is None
    assert "Refreshed book price: 100" in report and "weighted PD 20" in report


@pytest.mark.parametrize("gate", writer.GATES)
@pytest.mark.parametrize("state", ["fail", "unknown"])
def test_every_failed_or_unknown_gate_blocks(gate, state):
    scan, draft = fixture()
    draft["assessments"][0]["gates"][gate]["status"] = state
    schedule, _ = writer.compose(scan, draft, NOW)
    assert schedule["candidates"] == []
    assert schedule["analysis_status"] == ("incomplete" if state == "unknown" else "complete")


def test_missing_game_assessment_cannot_render_complete_zero_card(tmp_path):
    scan, draft = fixture(); draft["assessments"] = []
    schedule, report = writer.compose(scan, draft, NOW)
    assert schedule["analysis_status"] == "incomplete" and "INCOMPLETE" in report
    with pytest.raises(writer.Invalid, match="incomplete"):
        writer.write_bundle(tmp_path, writer.encoded(scan), writer.encoded(draft), NOW)
    assert not (tmp_path / ".picks").exists()


def test_legacy_literal_empty_schedule_is_not_a_draft():
    scan, _ = fixture()
    with pytest.raises(writer.Invalid, match="envelope"):
        writer.compose(scan, {"date": "2026-09-09", "sport": "NFL", "candidates": [], "inactives_watchlist": []}, NOW)


def test_skeleton_is_deliberately_incomplete():
    scan, _ = fixture()
    draft = writer.skeleton(scan, "skeleton", "2026-09-09", NOW)
    schedule, report = writer.compose(scan, draft, NOW)
    assert schedule["analysis_status"] == "incomplete" and not schedule["candidates"]
    assert "no completed card" in report


@pytest.mark.parametrize("field,value", [("source_type", "depth_chart"), ("data.confirmed_start", False),
                                         ("data.position", "RB"), ("data.athlete_id", "other-qb")])
def test_depth_or_wrong_identity_never_confirms(field, value):
    scan, draft = fixture(); e = evidence(draft, "qb")
    if field.startswith("data."): e["data"][field[5:]] = value
    else: e[field] = value
    with pytest.raises(writer.Invalid, match="confirmation"):
        writer.compose(scan, draft, NOW)


@pytest.mark.parametrize("field,value", [("event_id", "other-game"), ("team_ids", ["99"]), ("kickoff_utc", "2026-09-11T00:20Z")])
def test_evidence_identity_mismatch_refused(field, value):
    scan, draft = fixture(); evidence(draft, "book")[field] = value
    with pytest.raises(writer.Invalid, match="mismatch"):
        writer.compose(scan, draft, NOW)


@pytest.mark.parametrize("field", ["executed", "vig_approved", "status", "candidates", "execution_mode"])
def test_producer_cannot_supply_execution_or_decision_fields(field):
    scan, draft = fixture(); draft["assessments"][0][field] = True
    with pytest.raises(writer.Invalid, match="unknown assessment"):
        writer.compose(scan, draft, NOW)


def test_missing_exchange_is_incomplete_not_exhaustive_market_absence():
    scan, draft = fixture()
    item = draft["assessments"][0]; item["evidence"] = [e for e in item["evidence"] if e["id"] != "exchange"]
    item["gates"]["price_discipline"] = {"status": "unknown", "reason": "Exchange not retrieved", "evidence_refs": ["book"]}
    schedule, _ = writer.compose(scan, draft, NOW)
    assert schedule["analysis_status"] == "incomplete"
    assert schedule["game_assessments"][0]["market"]["exchange_ask"] is None


def test_attempted_exchange_unavailable_supports_failed_price_gate():
    scan, draft = fixture(); e = evidence(draft, "exchange")
    e.update(status="unavailable", reason="Exact contract lookup found no match", data={})
    draft["assessments"][0]["gates"]["price_discipline"]["status"] = "fail"
    schedule, _ = writer.compose(scan, draft, NOW)
    assert schedule["analysis_status"] == "complete" and schedule["candidates"] == []
    assert schedule["game_assessments"][0]["market"]["net_edge"] is None


@pytest.mark.parametrize("team_ids", [["26"], ["17", "26"]])
def test_qb_watchlist_rejects_unavailable_evidence_for_other_teams(team_ids):
    scan, draft = fixture()
    evidence(draft, "qb").update(status="unavailable", reason="Official confirmation pending",
                                 data={}, team_ids=team_ids)
    draft["assessments"][0]["gates"]["qb_status_gate"].update(
        status="fail", reason_code="qb_status_unconfirmed")
    with pytest.raises(ValueError, match="watchlist needs attempted official"):
        writer.compose(scan, draft, NOW)


def test_only_qb_blocker_becomes_watchlist_without_awaiting_jerry():
    scan, draft = fixture()
    evidence(draft, "qb").update(status="unavailable", reason="Official confirmation pending", data={})
    gate = draft["assessments"][0]["gates"]["qb_status_gate"]
    gate.update(status="fail", reason_code="qb_status_unconfirmed")
    schedule, _ = writer.compose(scan, draft, NOW)
    watch, = schedule["inactives_watchlist"]
    assert not schedule["candidates"] and watch["status"] == "pending_inactives_recheck"
    assert watch["blocked_only_by"] == ["qb_status_unconfirmed"]
    assert writer.stamp(watch["recheck_due_utc"]) == writer.stamp(KICKOFF) - dt.timedelta(minutes=75)
    assert type(watch["original_price"]) is int
    draft["assessments"][0]["gates"]["rest_travel_gate"]["status"] = "fail"
    schedule, _ = writer.compose(scan, draft, NOW)
    assert not schedule["inactives_watchlist"]


def test_week_one_prior_form_can_pass_but_requires_offseason_review():
    scan, draft = fixture()
    assert writer.compose(scan, draft, NOW)[0]["candidates"]
    draft["assessments"][0]["gates"]["week_to_week_overreaction_gate"]["evidence_refs"] = ["scan.away_form"]
    with pytest.raises(writer.Invalid, match="offseason review"):
        writer.compose(scan, draft, NOW)


def test_week_one_high_confidence_refused_and_preseason_never_candidates():
    scan, draft = fixture(); draft["assessments"][0]["confidence"] = "High"
    with pytest.raises(writer.Invalid, match="Medium"):
        writer.compose(scan, draft, NOW)
    draft["assessments"][0]["confidence"] = "Medium"
    scan[0]["seasontype"] = 1
    for side in ("away", "home"): scan[0][side + "_form"]["prior_season_games"] = 0
    schedule, _ = writer.compose(scan, draft, NOW)
    assert schedule["candidates"] == [] and schedule["inactives_watchlist"] == []


def test_outdoor_weather_used_and_rendered():
    scan, draft = fixture(); outdoor(scan[0])
    schedule, report = writer.compose(scan, draft, NOW)
    assert schedule["candidates"] and "Weather collection: retrieved" in report


@pytest.mark.parametrize("change", ["venue", "kickoff", "hour", "nan", "stale", "missing"])
def test_invalid_weather_cannot_pass(change):
    scan, draft = fixture(); outdoor(scan[0]); w = scan[0]["weather"]
    if change == "venue": w["venue"]["id"] = "wrong"
    if change == "kickoff": w["kickoff"] = "2026-09-11T00:20Z"
    if change == "hour": w["samples"][1]["time_utc"] = w["samples"][0]["time_utc"]
    if change == "nan": w["samples"][1]["wind_speed_10m"] = float("nan")
    if change == "stale": w["retrieved_at"] = "2026-09-08T00:00Z"
    if change == "missing": w["status"] = "unavailable"
    with pytest.raises(writer.Invalid): writer.compose(scan, draft, NOW)


@pytest.mark.parametrize("key,value", [("ask", 0), ("ask", float("nan")), ("team_id", "26"), ("side", "home"), ("contract_id", "")])
def test_exchange_invalid_values_cannot_pass(key, value):
    scan, draft = fixture(); evidence(draft, "exchange")["data"][key] = value
    with pytest.raises(writer.Invalid): writer.compose(scan, draft, NOW)


def test_edge_floor_and_disagreement_are_computed():
    scan, draft = fixture(); draft["assessments"][0]["win_probability"] = 0.52
    assert writer.compose(scan, draft, NOW)[0]["candidates"]
    draft["assessments"][0]["win_probability"] = 0.519
    with pytest.raises(writer.Invalid, match="2% edge"): writer.compose(scan, draft, NOW)
    draft["assessments"][0]["win_probability"] = 0.60
    with pytest.raises(writer.Invalid, match="explanation"): writer.compose(scan, draft, NOW)


@pytest.mark.parametrize("kind", ["book", "exchange"])
def test_stale_price_evidence_refused(kind):
    scan, draft = fixture(); evidence(draft, kind)["observed_at"] = "2026-09-09T19:44Z"
    with pytest.raises(writer.Invalid, match="stale"): writer.compose(scan, draft, NOW)


def test_bundle_writes_and_readback_revalidates_content(tmp_path):
    scan, draft = fixture()
    path = writer.write_bundle(tmp_path, writer.encoded(scan), writer.encoded(draft), NOW)
    assert writer.verify_bundle(tmp_path, "synthetic-run", NOW)["event_ids"] == ["game-1"]
    assert json.loads((path / "schedule.json").read_text())["candidates"][0]["executed"] is False
    with pytest.raises(writer.Invalid, match="already exists"):
        writer.write_bundle(tmp_path, writer.encoded(scan), writer.encoded(draft), NOW)
    (path / "slate.md").write_text("PASS. Zero candidates")
    with pytest.raises(writer.Invalid, match="bytes changed"):
        writer.verify_bundle(tmp_path, "synthetic-run", NOW)


def test_forged_hashes_cannot_replace_recomputed_schedule(tmp_path):
    scan, draft = fixture(); path = writer.write_bundle(tmp_path, writer.encoded(scan), writer.encoded(draft), NOW)
    schedule = json.loads((path / "schedule.json").read_text()); schedule["candidates"] = []
    data = writer.encoded(schedule); (path / "schedule.json").write_bytes(data)
    receipt = json.loads((path / "receipt.json").read_text()); receipt["sha256"]["schedule.json"] = writer.digest(data)
    (path / "receipt.json").write_bytes(writer.encoded(receipt))
    with pytest.raises(writer.Invalid, match="differ"):
        writer.verify_bundle(tmp_path, "synthetic-run", NOW)


def test_missing_receipt_and_wrong_root_rejected(tmp_path):
    scan, draft = fixture(); path = writer.write_bundle(tmp_path, writer.encoded(scan), writer.encoded(draft), NOW)
    receipt = json.loads((path / "receipt.json").read_text()); receipt["output_root"] = "/wrong-root"
    (path / "receipt.json").write_bytes(writer.encoded(receipt))
    with pytest.raises(writer.Invalid, match="root mismatch"): writer.verify_bundle(tmp_path, "synthetic-run", NOW)
    (path / "receipt.json").unlink()
    with pytest.raises(writer.Invalid, match="missing"): writer.verify_bundle(tmp_path, "synthetic-run", NOW)


def test_symlink_output_escape_refused(tmp_path):
    root = tmp_path / "root"; root.mkdir(); outside = tmp_path / "outside"; outside.mkdir()
    (root / ".picks").symlink_to(outside, target_is_directory=True)
    scan, draft = fixture()
    with pytest.raises(writer.Invalid): writer.write_bundle(root, writer.encoded(scan), writer.encoded(draft), NOW)


def test_collector_failure_never_complete(tmp_path):
    scan, draft = fixture(); scan[0]["error"] = "Provider failed"
    schedule, _ = writer.compose(scan, draft, NOW)
    assert schedule["analysis_status"] == "incomplete"
    with pytest.raises(writer.Invalid, match="incomplete"):
        writer.write_bundle(tmp_path, writer.encoded(scan), writer.encoded(draft), NOW)


def test_cli_skipping_finalizer_cannot_verify(tmp_path):
    with patch("sys.argv", ["nfl_card_writer", "--root", str(tmp_path), "--verify", "--run-id", "no-finalizer"]):
        assert writer.main() == 2


def test_legacy_scan_and_duplicate_event_refused():
    scan, draft = fixture(); del scan[0]["context_version"]
    with pytest.raises(writer.Invalid, match="versioned"): writer.compose(scan, draft, NOW)
    scan, draft = fixture(); scan.append(copy.deepcopy(scan[0]))
    with pytest.raises(writer.Invalid, match="duplicate scanned"): writer.compose(scan, draft, NOW)


def test_duplicate_json_key_refused():
    with pytest.raises(writer.Invalid, match="duplicate"):
        writer.load_json(b'{"a":1,"a":2}')


@pytest.mark.parametrize("field", ["away_form", "home_form", "away_injury_evidence", "home_injury_evidence"])
def test_missing_form_or_injury_collection_cannot_clear_gate(field):
    scan, draft = fixture(); scan[0][field] = {}
    with pytest.raises(writer.Invalid, match="incomplete"):
        writer.compose(scan, draft, NOW)


def test_watchlist_reason_code_alone_is_insufficient():
    scan, draft = fixture()
    draft["assessments"][0]["gates"]["qb_status_gate"].update(status="fail", reason_code="qb_status_unconfirmed")
    with pytest.raises(writer.Invalid, match="attempted official"):
        writer.compose(scan, draft, NOW)


def test_one_inactives_blocker_and_two_blocker_rule():
    scan, draft = fixture()
    evidence(draft, "home-availability").update(status="unavailable", reason="Inactives unresolved", data={})
    draft["assessments"][0]["gates"]["injury_cluster_gate"].update(status="fail", reason_code="inactives_unconfirmed")
    schedule, _ = writer.compose(scan, draft, NOW)
    assert schedule["inactives_watchlist"][0]["blocked_only_by"] == ["inactives_unconfirmed"]
    draft["assessments"][0]["gates"]["price_discipline"]["status"] = "fail"
    assert not writer.compose(scan, draft, NOW)[0]["inactives_watchlist"]


def test_committed_prompt_requires_finalizer_and_independent_readback():
    prompt = (Path(__file__).parents[1] / "scripts/nfl_weekly_prompt.txt").read_text()
    assert "--write" in prompt and "--verify --run-id RUN_ID" in prompt
    assert "REQUIRED TERMINAL CHECK" in prompt and "Consumers/reviewers must repeat step 8" in prompt
    assert "incomplete" in prompt and "do not improvise a replacement script" in prompt


def test_cli_write_then_verify_uses_same_root_and_final_inputs(tmp_path):
    import subprocess
    import sys
    scan, draft = fixture()
    now = dt.datetime.now(dt.timezone.utc)
    kickoff = (now + dt.timedelta(hours=4)).isoformat()
    scan[0].update(collected_at=now.isoformat(), time=kickoff)
    draft['date'] = now.astimezone(writer.ZoneInfo('America/Chicago')).date().isoformat()
    for item in draft['assessments'][0]['evidence']:
        item.update(observed_at=now.isoformat(), kickoff_utc=kickoff)
    scan_file, draft_file = tmp_path / 'input-scan.json', tmp_path / 'input-draft.json'
    scan_file.write_bytes(writer.encoded(scan)); draft_file.write_bytes(writer.encoded(draft))
    script = str(Path(writer.__file__).resolve())
    command = [sys.executable, script, '--root', str(tmp_path)]
    write = subprocess.run(command + ['--scan', str(scan_file), '--draft', str(draft_file), '--write'], capture_output=True, text=True)
    assert write.returncode == 0, write.stderr
    verify = subprocess.run(command + ['--verify', '--run-id', draft['run_id']], capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)['analysis_status'] == 'complete'
    path = tmp_path / '.picks/nfl/runs' / draft['run_id']
    candidate, = json.loads((path / 'schedule.json').read_text())['candidates']
    assert candidate['status'] == 'awaiting_jerry' and candidate['executed'] is False
