"""NFL-only evidence contract tests. No provider access or order execution."""
import datetime as dt
from unittest.mock import patch

import pytest
from scripts import nfl_stage2_scan as scan
from tests.test_nfl_stage2_scan import schedule_event


VENUE = {"fullName": "Lumen Field", "indoor": False, "address": {"city": "Seattle", "state": "WA", "country": "USA"}}
GEO = [{"name": "Lumen Field", "lat": "47.595", "lon": "-122.332", "address": {"city": "Seattle"}}]
KICKOFF = "2026-09-09T19:20:00-05:00"


def forecast():
    return {"utc_offset_seconds": 0, "hourly_units": {
        "temperature_2m": "°F", "wind_speed_10m": "mp/h", "wind_gusts_10m": "mp/h", "precipitation": "inch", "snowfall": "inch"},
        "hourly": {"time": [f"2026-09-10T0{i}:00" for i in range(5)],
                   "temperature_2m": [75, 74, 70, 65, 64], "wind_speed_10m": [6, 10, 15, 10, 6],
                   "wind_gusts_10m": [10]*5, "precipitation": [0]*5, "snowfall": [0]*5}}


def roster():
    return {"season": {"year": 2026}, "timestamp": "2026-09-09T18:00Z", "athletes": [{"items": [
        {"id": str(i), "displayName": f"Player {i}", "position": {"abbreviation": "QB" if i == 1 else "OL"},
         "status": {"name": "Active"}, "injuries": [{"status": "Questionable", "type": "injury",
         "date": "2026-09-09T17:00Z", "details": {"type": "Ankle"}, "longComment": "Limited practice"}]}
        for i in range(1, 13)]}]}


def depth():
    return {"season": {"year": 2026}, "team": {"id": "17"}, "depthchart": [{"positions": {"qb": {
        "position": {"abbreviation": "QB"}, "athletes": [{"id": "1", "displayName": "Player 1"}]}}}]}


def provider(url):
    if "/roster" in url:
        return roster()
    if "/depthcharts" in url:
        return depth()
    if "/summary" in url:
        return {"header": {"id": "game"}}
    raise AssertionError(url)


def test_injury_identity_detail_and_all_rows_survive():
    with patch.object(scan, "get", side_effect=provider):
        result = scan.NflSlateCollector(2026, 1).injury_evidence("17")
    assert result["status"] == "retrieved"
    assert len(result["items"]) == 12
    item = result["items"][0]
    assert (item["athlete_id"], item["position"], item["team_id"]) == ("1", "QB", "17")
    assert item["detail"] == {"type": "Ankle"}
    assert item["source_timestamp"] == "2026-09-09T17:00Z"
    assert item["source"] and item["retrieved_at"]


def test_depth_rank_active_roster_never_confirm_qb():
    with patch.object(scan, "get", side_effect=provider) as get:
        result = scan.NflSlateCollector(2026, 1).qb_evidence("17", "game")
    assert get.call_count == 3
    assert result["depth_chart"]["quarterbacks"][0]["roster_match"] is True
    assert result["confirmation"]["confirmed"] is False
    assert result["confirmation"]["status"] == "unavailable"
    assert result["official_inactives"]["status"] == "not_retrieved"


@pytest.mark.parametrize("payload,status", [
    ({"season": {"year": 2026}, "athletes": []}, "unavailable"),
    ({}, "collector_failure"),
    ({"season": {"year": 2025}, "athletes": []}, "collector_failure"),
    ({"season": {"year": 2026}, "athletes": None}, "collector_failure"),
])
def test_absence_schema_and_stale_roster_distinguished(payload, status):
    with patch.object(scan, "get", return_value=payload):
        assert scan.NflSlateCollector(2026, 1).roster_evidence("17")["status"] == status


def test_transport_failure_not_clean_injury_report():
    with patch.object(scan, "get", side_effect=TimeoutError("timeout")):
        result = scan.NflSlateCollector(2026, 1).injury_evidence("17")
    assert result["status"] == "collector_failure"
    assert "TimeoutError" in result["error"]


@pytest.mark.parametrize("change", ["team", "season", "roster_id", "missing_qb"])
def test_depth_identity_mismatch_or_absence_never_confirms(change):
    def get(url):
        if "depthcharts" not in url:
            return provider(url)
        d = depth()
        if change == "team": d["team"]["id"] = "99"
        if change == "season": d["season"]["year"] = 2025
        if change == "roster_id": d["depthchart"][0]["positions"]["qb"]["athletes"][0]["id"] = "99"
        if change == "missing_qb": d["depthchart"] = []
        return d
    with patch.object(scan, "get", side_effect=get):
        r = scan.NflSlateCollector(2026, 1).qb_evidence("17", "game")
    assert r["confirmation"]["confirmed"] is False
    if change == "roster_id": assert r["depth_chart"]["quarterbacks"][0]["roster_match"] is False
    elif change == "missing_qb": assert r["depth_chart"]["status"] == "unavailable"
    else: assert r["depth_chart"]["status"] == "collector_failure"


def test_stadium_kickoff_weather_and_units():
    with patch.object(scan, "get", side_effect=[GEO, forecast()]) as get:
        r = scan.NflSlateCollector(2026, 1).weather_evidence(VENUE, KICKOFF)
    assert r["status"] == "retrieved"
    assert r["latitude"] == 47.595
    assert len(r["samples"]) == 5
    assert r["samples"][0]["time_utc"] == "2026-09-10T00:00Z"
    assert "start_hour=2026-09-10T00%3A00" in get.call_args.args[0]
    assert r["wind_at_least_15_mph"] is True
    assert r["forecast_issued_at"] is None  # retrieval is not model issuance
    assert r["weather_thesis_review_required"] is True


@pytest.mark.parametrize("indoor,status", [(True, "not_applicable"), (None, "unavailable"), ("false", "unavailable")])
def test_roof_status_is_explicit(indoor, status):
    with patch.object(scan, "get") as get:
        r = scan.NflSlateCollector(2026, 1).weather_evidence({**VENUE, "indoor": indoor}, KICKOFF)
    get.assert_not_called()
    assert r["status"] == status


@pytest.mark.parametrize("geo", [[], GEO * 2, [{**GEO[0], "name": "Seattle"}], [{**GEO[0], "address": {"city": "Other"}}]])
def test_geocoder_no_city_or_ambiguous_fallback(geo):
    with patch.object(scan, "get", return_value=geo) as get:
        r = scan.NflSlateCollector(2026, 1).weather_evidence(VENUE, KICKOFF)
    assert r["status"] == "unavailable"
    assert get.call_count == 1


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, -1])
def test_invalid_wind_fails_closed(bad):
    f = forecast(); f["hourly"]["wind_speed_10m"][2] = bad
    with patch.object(scan, "get", side_effect=[GEO, f]):
        assert scan.NflSlateCollector(2026, 1).weather_evidence(VENUE, KICKOFF)["status"] == "collector_failure"


@pytest.mark.parametrize("change", ["hours", "units", "timezone", "null_hour", "short_array"])
def test_missing_or_mislabelled_weather_does_not_clear(change):
    f = forecast()
    if change == "hours": f["hourly"]["time"] = f["hourly"]["time"][:-1]
    if change == "units": f["hourly_units"]["wind_speed_10m"] = "km/h"
    if change == "timezone": f["utc_offset_seconds"] = -25200
    if change == "null_hour": f["hourly"]["precipitation"][1] = None
    if change == "short_array": f["hourly"]["precipitation"] = []
    with patch.object(scan, "get", side_effect=[GEO, f]):
        assert scan.NflSlateCollector(2026, 1).weather_evidence(VENUE, KICKOFF)["status"] in ("unavailable", "collector_failure")


def test_form_discount_cannot_cancel_with_prior_only_denominator():
    prior = [schedule_event("2025-12-01T00:00Z", 17, 26, 30, 10)]
    with patch.object(scan.NflSlateCollector, "team_schedule_events", side_effect=[[], prior]):
        f = scan.NflSlateCollector(2026, 1).team_form(17, dt.date(2026, 9, 9))
    assert f["n"] == f["prior_season_games"] == 1
    assert f["current_season_games"] == 0
    assert f["pd"] == 20
    assert f["discounted_pd_per_game"] == 10
    assert f["effective_n"] == 0.5
    assert f["discounted"] and f["offseason_adjustment_required"]
    assert "discounted" in f["form_label"] and f["confidence_cap"] == "Medium"


def test_prior_counts_only_valid_scores_and_excludes_preseason_future():
    valid = schedule_event("2025-12-01T00:00Z", 17, 26, 30, 10)
    preseason = {**valid, "seasonType": {"type": 1}}
    bad = schedule_event("2025-12-02T00:00Z", 17, 26, None, 10)
    future = schedule_event("2027-12-01T00:00Z", 17, 26, 70, 0)
    with patch.object(scan.NflSlateCollector, "team_schedule_events", side_effect=[[], [preseason, bad, future, valid]]):
        f = scan.NflSlateCollector(2026, 1).team_form(17, dt.date(2026, 9, 9))
    assert f["prior_season_games"] == f["n"] == 1


@pytest.mark.parametrize("week,seasontype", [(5, 2), (1, 1), (19, 3)])
def test_fallback_limited_to_early_regular_season(week, seasontype):
    with patch.object(scan.NflSlateCollector, "team_schedule_events", return_value=[]) as schedule:
        f = scan.NflSlateCollector(2026, week, seasontype).team_form(17, dt.date(2026, 9, 9))
    assert schedule.call_count == 1
    assert f["prior_season_games"] == 0


def test_diagnostics_distinguish_three_causes_and_preserve_exchange_null():
    r = {"away_injury_evidence": {"status": "collector_failure"},
         "home_injury_evidence": {"status": "unavailable"},
         "exchange": {"status": "not_retrieved", "ask": None, "net_edge": None}}
    b = {x["component"]: x for x in scan.readiness_blockers(r)}
    assert b["away_injuries"]["category"] == "collector_failure"
    assert b["home_injuries"]["category"] == "upstream_missing"
    assert b["exchange"]["category"] == "hard_gate"
    assert r["exchange"]["ask"] is None


def test_build_row_wires_all_collectors_and_stays_pass():
    event = {"id": "game", "name": "Away at Home", "date": KICKOFF, "competitions": [{
        "venue": VENUE, "competitors": [{"homeAway": side, "team": {"id": tid, "displayName": side}}
        for side, tid in [("away", "17"), ("home", "26")]]}]}
    def get(url):
        if "/schedule" in url: return {"events": []}
        if "nominatim" in url: return GEO
        if "open-meteo" in url: return forecast()
        if "/26/depthcharts" in url:
            d = depth(); d["team"]["id"] = "26"; return d
        return provider(url)
    with patch.object(scan, "get", side_effect=get):
        r = scan.NflSlateCollector(2026, 1).build_row(event)
    assert r["weather"]["status"] == "retrieved"
    assert r["away_injuries"][0]["position"] == "QB"
    assert r["home_qb"]["depth_chart"]["status"] == "retrieved"
    assert r["candidates"] == [] and r["assessment"] == "PASS" and r["official_pick_allowed"] is False
    assert any(b["component"] == "exchange" for b in r["blockers"])


def test_mixed_current_prior_form_retains_current_weight():
    current = [schedule_event("2026-09-01T00:00Z", 17, 26, 30, 10)]
    prior = [schedule_event("2025-12-01T00:00Z", 17, 26, 10, 30)]
    with patch.object(scan.NflSlateCollector, "team_schedule_events", side_effect=[current, prior]):
        f = scan.NflSlateCollector(2026, 2).team_form(17, dt.date(2026, 9, 9))
    assert f["current_season_games"] == f["prior_season_games"] == 1
    assert f["pd"] == 0 and f["weighted_pd"] == 10
    assert f["discounted_pd_per_game"] == 5 and f["effective_n"] == 1.5


def test_missing_injury_field_is_not_successful_empty_report():
    payload = roster()
    del payload["athletes"][0]["items"][0]["injuries"]
    with patch.object(scan, "get", return_value=payload):
        r = scan.NflSlateCollector(2026, 1).injury_evidence("17")
    assert r["status"] == "collector_failure"


def test_geocoding_cache_avoids_repeated_stadium_requests():
    c = scan.NflSlateCollector(2026, 1)
    with patch.object(scan, "get", side_effect=[GEO, forecast(), forecast()]) as get:
        first = c.weather_evidence(VENUE, KICKOFF)
        second = c.weather_evidence(VENUE, KICKOFF)
    assert first["status"] == second["status"] == "retrieved"
    assert sum("nominatim" in call.args[0] for call in get.call_args_list) == 1
