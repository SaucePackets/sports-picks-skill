"""Original-year equivalence and refusal-preserving multiseason evidence."""

from copy import deepcopy
import pytest
import mlb_multiseason_evidence as e
import mlb_multiseason_admission as m
import mlb_participant_history_experiment as participant
import mlb_bulk_starter_admission as bulk
from mlb_appearance_census import runner_out
from test_mlb_participant_history_experiment import fixture, v2_fixture
from test_mlb_market_free_checkpoint import raw


def year_fixture(year):
    g, f = fixture()

    def change(v):
        if isinstance(v, str):
            return v.replace("2025", str(year))
        if isinstance(v, list):
            return [change(x) for x in v]
        if isinstance(v, dict):
            return {k: change(x) for k, x in v.items()}
        return v

    return change(g), change(f)


def test_original_year_evidence_matches_pinned_functions():
    g, f = fixture()
    body = raw(f)
    assert e.outcome(g, body) == bulk.outcome(g, body)
    assert e.appearances(g, body, non_pa_classifier=runner_out) == bulk.appearances(
        g, body, non_pa_classifier=runner_out
    )
    assert e.participants(g, body) == participant.participants(
        g, body, evidence_version=2
    )


@pytest.mark.parametrize("year", [2023, 2024])
def test_prior_seasons_admit_only_matching_identity(year):
    g, f = year_fixture(year)
    body = raw(f)
    assert e.appearances(g, body)
    assert e.participants(g, body)["feed_sha256"] == e.sha(body)
    f["gameData"]["game"]["season"] = "2025"
    with pytest.raises(ValueError, match="wrong_season"):
        e.outcome(g, raw(f))
    with pytest.raises(ValueError, match="participant_game_type"):
        e.participants(g, raw(f))


@pytest.mark.parametrize("kind", ["zero", "rain", "runner"])
def test_v2_exceptions_stay_participant_only(kind):
    g, f = v2_fixture(kind)
    with pytest.raises(ValueError):
        e.appearances(g, raw(f), non_pa_classifier=runner_out)
    assert e.participants(g, raw(f))["statistics_admitted"] is False


def test_uncollected_feed_and_repeated_occurrences_remain_gaps():
    g, f = fixture()
    target = dict(game_id=g["game_id"], occurrences=[g])
    assert m.inspect_game(target, None)["refusal"] == "feed_not_collected"
    target["occurrences"].append(deepcopy(g))
    result = m.inspect_game(target, raw(f))
    assert result["refusal"] == "repeated_schedule_occurrence"
    assert result["records"] == [] and result["certificate"] is None


@pytest.mark.parametrize(
    "mutation", ["counts", "pitcher", "chronology", "substitution"]
)
def test_original_statistical_contradictions_remain_refused(mutation):
    g, f = year_fixture(2024)
    p = f["liveData"]["plays"]["allPlays"][0]
    if mutation == "counts":
        f["liveData"]["boxscore"]["teams"]["away"]["players"]["ID10"]["stats"][
            "pitching"
        ]["battersFaced"] += 1
    if mutation == "pitcher":
        p["matchup"]["pitcher"]["id"] = 999
    if mutation == "chronology":
        p["about"]["startTime"] = "2024-04-22T23:59:00Z"
    if mutation == "substitution":
        p["playEvents"].append(
            dict(
                isPitch=False,
                details={"eventType": "pitching_substitution"},
                player={"id": 10},
            )
        )
    with pytest.raises(ValueError):
        e.appearances(g, raw(f), non_pa_classifier=runner_out)


def test_postponed_interval_matches_reviewed_rule_and_rejects_suspension():
    from test_mlb_participant_history_experiment import postponed_fixture

    g, f = postponed_fixture()
    assert e.postponed_interval(g, raw(f)) == participant.postponed_interval(g, raw(f))
    f["gameData"]["datetime"]["resumeDate"] = "2025-04-22"
    with pytest.raises(ValueError, match="postponed_resume_conflict"):
        e.postponed_interval(g, raw(f))


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "present",
        "wrong_bytes",
        "wrong_game",
        "at_cutoff",
        "future",
        "empty",
        "missing",
        "older",
    ],
)
def test_cached_exclusions_exactly_match_pinned_narrowing(mutation):
    from test_mlb_participant_history_experiment import history_fixture

    base, certs = history_fixture()
    c = certs["100"]["certificate"]
    if mutation == "present":
        c["pitcher_ids"].append("99")
    if mutation == "wrong_bytes":
        c["feed_sha256"] = "other"
    if mutation == "wrong_game":
        c["game_id"] = "other"
    if mutation == "at_cutoff":
        c["completion_upper_bound"] = "2025-04-06T18:00:00Z"
    if mutation == "future":
        c["completion_upper_bound"] = "2025-04-07T18:00:00Z"
    if mutation == "empty":
        c["pitcher_ids"] = []
    if mutation == "missing":
        certs = {}
    if mutation == "older":
        c["pitcher_ids"].append("99")
        c["completion_upper_bound"] = "2025-04-01T22:00:00Z"
    expected = participant.narrow(base, certs)["occurrences"][0]["histories"]
    row = base["occurrences"][0]
    cache = m.certificate_cache(certs)
    actual = {
        s: m.narrow_history(row, h, cache, m.instant(row["observation_cutoff"]))
        for s, h in row["histories"].items()
    }
    assert actual == expected
