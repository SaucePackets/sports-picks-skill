"""Participant evidence may exclude absence, never fill missing pitch statistics."""

from copy import deepcopy

import pytest
import mlb_participant_history_experiment as m
from test_mlb_bulk_starter_admission import pitching_feed
from test_mlb_market_free_checkpoint import game, raw, schedule
from mlb_market_free_checkpoint import schedule_census
from mlb_bulk_starter_admission import appearances


def fixture():
    g = game("2025-04-21")
    parsed = schedule_census(schedule(g["officialDate"], [g]), g["officialDate"])[0]
    parsed.update(schedule_sha256="s", split="train")
    f = pitching_feed(g)
    for box in f["liveData"]["boxscore"]["teams"].values():
        for p in box["players"].values():
            p["stats"]["pitching"].update(outs=1, numberOfPitches=1, gamesPitched=1)
        box["teamStats"] = {
            "pitching": {
                k: sum(p["stats"]["pitching"][k] for p in box["players"].values())
                for k in m.COUNTS
            }
        }
    return parsed, f


def test_ambiguous_attribution_can_certify_roster_but_not_statistics():
    g, f = fixture()
    p = f["liveData"]["plays"]["allPlays"][0]
    p["playEvents"].append(
        dict(
            isPitch=False,
            details={"eventType": "pitching_substitution"},
            player={"id": 10},
        )
    )
    with pytest.raises(
        ValueError, match="ambiguous_mid_appearance_pitcher_substitution"
    ):
        appearances(g, raw(f))
    cert = m.participants(g, raw(f))
    assert cert["pitcher_ids"] == ["10", "20"]
    assert not cert["statistics_admitted"]
    assert cert["feed_sha256"] == m.sha(raw(f))


@pytest.mark.parametrize(
    "change,reason",
    [
        ("total", "participant_total_conflict"),
        ("unlisted", "participant_player_census_mismatch"),
        ("substitution", "participant_substitution_missing_or_wrong_side"),
        ("matchup", "participant_matchup_missing_or_wrong_side"),
        ("pitch", "participant_pitch_total_conflict"),
        ("incomplete", "participant_play_index_or_completion"),
        ("score", "participant_terminal_score_conflict"),
        ("reversed_time", "participant_play_time"),
        ("team", "participant_box_team"),
        ("boolean_count", "participant_counts"),
    ],
)
def test_contradictions_refuse_absence_certificate(change, reason):
    g, f = fixture()
    b = f["liveData"]["boxscore"]["teams"]["away"]
    p = f["liveData"]["plays"]["allPlays"][0]
    if change == "total":
        b["teamStats"]["pitching"]["battersFaced"] += 1
    if change == "unlisted":
        b["players"]["ID30"] = deepcopy(b["players"]["ID10"])
        b["players"]["ID30"]["person"]["id"] = 30
    if change == "substitution":
        p["playEvents"].append(
            dict(
                isPitch=False,
                details={"eventType": "pitching_substitution"},
                player={"id": 30},
            )
        )
    if change == "matchup":
        p["matchup"]["pitcher"]["id"] = 20
    if change == "pitch":
        p["playEvents"][0]["isPitch"] = False
    if change == "incomplete":
        p["about"]["isComplete"] = False
    if change == "score":
        f["liveData"]["plays"]["allPlays"][-1]["result"]["awayScore"] += 1
    if change == "reversed_time":
        p["about"]["startTime"] = "2025-04-22T23:00:00Z"
    if change == "team":
        b["team"]["id"] = 999
    if change == "boolean_count":
        b["players"]["ID10"]["stats"]["pitching"]["battersFaced"] = True
    with pytest.raises(ValueError, match=reason):
        m.participants(g, raw(f))


def test_zero_bf_substitute_stays_in_participant_set():
    g, f = fixture()
    b = f["liveData"]["boxscore"]["teams"]["away"]
    b["pitchers"].append(30)
    b["players"]["ID30"] = {
        "person": {"id": 30},
        "stats": {"pitching": dict.fromkeys(m.COUNTS, 0) | {"gamesPitched": 1}},
    }
    f["liveData"]["plays"]["allPlays"][0]["playEvents"].append(
        dict(
            isPitch=False,
            details={"eventType": "pitching_substitution"},
            player={"id": 30},
        )
    )
    assert "30" in m.participants(g, raw(f))["pitcher_ids"]


def test_completion_uses_latest_end_not_final_array_position():
    g, f = fixture()
    ps = f["liveData"]["plays"]["allPlays"]
    ps[0]["about"]["endTime"] = "2025-04-21T23:59:00Z"
    assert (
        m.participants(g, raw(f))["completion_upper_bound"]
        == "2025-04-21T23:59:00+00:00"
    )


def history_fixture():
    rows = [
        dict(
            pitcher_id="99",
            game_id=str(i),
            source_date=f"2025-04-0{i}",
            completed_at=f"2025-04-0{i}T23:00:00Z",
            plate_appearances=10,
            strikeouts=2,
            walks=1,
        )
        for i in (1, 2, 3)
    ]
    gap = dict(
        game_id="100", source_date="2025-04-04", feed_sha256="f", refusal="ambiguous"
    )
    row = dict(
        game_id="200",
        source_date="2025-04-06",
        split="train",
        observation_cutoff="2025-04-06T18:00:00Z",
    )
    h = m.history(row, "99", rows, [gap], m.instant(row["observation_cutoff"]), True)
    row.update(
        histories={"away": deepcopy(h), "home": deepcopy(h)},
        features_admitted=False,
        refusal="pitcher_history_refused",
    )
    base = dict(
        occurrences=[row],
        census_complete=True,
        summary={"train": {"starter_candidates": 1, "feature_pairs_after": 0}},
    )
    cert = dict(
        game_id="100",
        feed_sha256="f",
        pitcher_ids=["10", "20"],
        completion_upper_bound="2025-04-04T23:00:00Z",
        status="structurally_corroborated_only",
    )
    return base, {"100": {"certificate": cert, "refusal": None}}


def test_only_corroborated_nonparticipant_gaps_are_excluded():
    base, certs = history_fixture()
    original = deepcopy(base)
    out = m.narrow(base, certs)
    assert base == original
    assert out["summary"]["train"]["feature_pairs_after"] == 1
    h = out["occurrences"][0]["histories"]["home"]
    assert h["strikeout_fraction"] == 0.2
    assert len(h["appearances"]) == 3
    assert h["excluded_gaps"][0]["certificate_sha256"] == m.sha(
        m.encoded(certs["100"]["certificate"])
    )


@pytest.mark.parametrize(
    "change",
    ["present", "wrong_bytes", "wrong_game", "at_cutoff", "future", "empty", "missing"],
)
def test_relevant_or_unverified_gap_keeps_history_blocked(change):
    base, certs = history_fixture()
    c = certs["100"]["certificate"]
    if change == "present":
        c["pitcher_ids"].append("99")
    if change == "wrong_bytes":
        c["feed_sha256"] = "other"
    if change == "wrong_game":
        c["game_id"] = "other"
    if change == "at_cutoff":
        c["completion_upper_bound"] = "2025-04-06T18:00:00Z"
    if change == "future":
        c["completion_upper_bound"] = "2025-04-07T18:00:00Z"
    if change == "empty":
        c["pitcher_ids"] = []
    if change == "missing":
        certs = {}
    out = m.narrow(base, certs)
    assert out["summary"]["train"]["feature_pairs_after"] == 0
    assert (
        out["occurrences"][0]["histories"]["home"]["refusal"]
        == "prior_appearance_census_unresolved"
    )


def test_duplicate_date_requires_one_consistent_final_occurrence():
    g, f = fixture()
    old = deepcopy(g)
    old["source_date"] = "2025-04-20"
    old["raw_game"]["status"] = {
        "abstractGameState": "Preview",
        "detailedState": "Postponed",
    }
    sources = {("feed", g["game_id"]): ({"body_sha256": m.sha(raw(f))}, raw(f))}
    gaps = [{"game_id": g["game_id"]}]
    assert m.certify([old, g], sources, gaps)[g["game_id"]]["certificate"] is not None
    old["away_id"] = "999"
    assert (
        m.certify([old, g], sources, gaps)[g["game_id"]]["refusal"]
        == "participant_schedule_occurrence_identity_conflict"
    )
    assert (
        m.certify([g, deepcopy(g)], sources, gaps)[g["game_id"]]["refusal"]
        == "participant_unique_final_schedule_unavailable"
    )


def postponed_fixture():
    g, f = fixture()
    g["source_date"] = "2025-04-20"
    g["scheduled_start"] = "2025-04-20T17:00:00Z"
    g["raw_game"]["gameDate"] = g["scheduled_start"]
    g["raw_game"]["rescheduleDate"] = f["gameData"]["datetime"]["dateTime"]
    g["raw_game"]["status"] = {"detailedState": "Postponed", "codedGameState": "D"}
    return g, f


def test_postponement_requires_reschedule_and_no_earlier_play():
    g, f = postponed_fixture()
    cert = m.postponed_interval(g, raw(f))
    assert cert["kind"] == "postponed_no_play_interval" and not cert["asof_verified"]
    assert cert["source_date"] == "2025-04-20"
    f["liveData"]["plays"]["allPlays"][0]["about"]["startTime"] = "2025-04-20T20:00:00Z"
    with pytest.raises(ValueError, match="postponed_prior_or_incomplete_play"):
        m.postponed_interval(g, raw(f))


@pytest.mark.parametrize(
    "change",
    ["resume", "nested_resume", "wrong_id", "missing_reschedule", "not_postponed"],
)
def test_moved_date_alone_or_suspension_never_proves_nonappearance(change):
    g, f = postponed_fixture()
    if change == "resume":
        f["gameData"]["datetime"]["resumeDate"] = "2025-04-21"
    if change == "nested_resume":
        f["gameData"]["status"]["detail"] = {"previous": "Suspended"}
    if change == "wrong_id":
        f["gamePk"] = 999
    if change == "missing_reschedule":
        del g["raw_game"]["rescheduleDate"]
    if change == "not_postponed":
        g["raw_game"]["status"]["detailedState"] = "Final"
    with pytest.raises((ValueError, KeyError)):
        m.postponed_interval(g, raw(f))


def test_postponed_exclusion_ends_at_reschedule_start_and_is_occurrence_bound():
    base, certs = history_fixture()
    c = dict(
        game_id="100",
        source_date="2025-04-04",
        feed_sha256="f",
        before_rescheduled_start="2025-04-07T18:00:00Z",
        kind="postponed_no_play_interval",
        status="structurally_corroborated_only",
    )
    certs = {
        "100": {
            "certificate": None,
            "refusal": "no final schedule",
            "postponed_intervals": [c],
        }
    }
    assert m.narrow(base, certs)["summary"]["train"]["feature_pairs_after"] == 1
    c["before_rescheduled_start"] = base["occurrences"][0]["observation_cutoff"]
    assert m.narrow(base, certs)["summary"]["train"]["feature_pairs_after"] == 0
    c["before_rescheduled_start"] = "2025-04-07T18:00:00Z"
    c["source_date"] = "2025-04-03"
    assert m.narrow(base, certs)["summary"]["train"]["feature_pairs_after"] == 0


def test_corroborated_early_rain_final_excludes_participants_only():
    g, f = fixture()
    g["raw_game"]["status"].update(
        detailedState="Completed Early", statusCode="FR", reason="Rain"
    )
    f["gameData"]["status"].update(
        detailedState="Completed Early: Rain", statusCode="FR", reason="Rain"
    )
    assert m.participants(g, raw(f))["pitcher_ids"] == ["10", "20"]
    with pytest.raises(ValueError, match="final_not_complete_in_both_sources"):
        appearances(g, raw(f))
    f["gameData"]["status"]["reason"] = "Other"
    with pytest.raises(ValueError, match="participant_early_end_not_corroborated"):
        m.participants(g, raw(f))


def test_resumed_game_requires_explicit_original_link_and_full_completion():
    g, f = fixture()
    dt = f["gameData"]["datetime"]
    dt.update(
        resumeDate="2025-04-22",
        resumeDateTime="2025-04-22T17:00:00Z",
        resumedFromDate="2025-04-21",
        resumedFromDateTime=g["scheduled_start"],
        dateTime="2025-04-22T17:00:00Z",
    )
    p = f["liveData"]["plays"]["allPlays"][-1]
    p["about"].update(startTime="2025-04-22T17:10:00Z", endTime="2025-04-22T17:11:00Z")
    cert = m.participants(g, raw(f))
    assert cert["completion_upper_bound"] == "2025-04-22T17:11:00+00:00"
    assert cert["pitcher_ids"] == ["10", "20"]
    with pytest.raises(ValueError):
        appearances(g, raw(f))
    dt["resumedFromDateTime"] = "2025-04-20T17:00:00Z"
    with pytest.raises(ValueError, match="participant_resume_link_mismatch"):
        m.participants(g, raw(f))


def test_present_pitcher_gap_can_only_age_out_before_all_three_selected_appearances():
    base, certs = history_fixture()
    c = certs["100"]["certificate"]
    c["pitcher_ids"].append("99")
    c["completion_upper_bound"] = "2025-03-31T23:00:00Z"
    out = m.narrow(base, certs)
    assert out["summary"]["train"]["feature_pairs_after"] == 1
    assert (
        out["occurrences"][0]["histories"]["home"]["excluded_gaps"][0]["reason"]
        == "completed_before_selected_three"
    )
    c["completion_upper_bound"] = "2025-04-01T23:00:00Z"
    assert m.narrow(base, certs)["summary"]["train"]["feature_pairs_after"] == 0
    c["completion_upper_bound"] = "2025-03-31T23:00:00Z"
    for h in base["occurrences"][0]["histories"].values():
        h["appearances"].pop()
    assert m.narrow(base, certs)["summary"]["train"]["feature_pairs_after"] == 0


def test_zero_game_zero_count_listed_substitute_is_kept_conservatively():
    g, f = fixture()
    b = f["liveData"]["boxscore"]["teams"]["away"]
    b["pitchers"].append(30)
    b["players"]["ID30"] = {
        "person": {"id": 30},
        "stats": {"pitching": dict.fromkeys(m.COUNTS, 0) | {"gamesPitched": 0}},
    }
    f["liveData"]["plays"]["allPlays"][0]["playEvents"].append(
        dict(
            isPitch=False,
            details={"eventType": "pitching_substitution"},
            player={"id": 30},
        )
    )
    assert "30" in m.participants(g, raw(f))["pitcher_ids"]


def test_resumed_final_cannot_reuse_pre_resume_completion_bound():
    g, f = fixture()
    dt = f["gameData"]["datetime"]
    dt.update(
        resumeDate="2025-04-22",
        resumeDateTime="2025-04-22T17:00:00Z",
        resumedFromDate="2025-04-21",
        resumedFromDateTime=g["scheduled_start"],
        dateTime="2025-04-22T17:00:00Z",
    )
    with pytest.raises(ValueError, match="participant_completion_before_start"):
        m.participants(g, raw(f))
