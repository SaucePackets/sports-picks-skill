"""Year-aware copy of pinned evidence rules; original 2025 replays stay unchanged."""

from mlb_market_free_checkpoint import require, interrupted_status
from mlb_real_shadow_capture import decode, final_candidate, instant, sha, positive_id
from mlb_bulk_starter_admission import SIDES, PA_EVENTS, K_EVENTS, BB_EVENTS
from mlb_participant_history_experiment import final_identity, COUNTS


def outcome(game, feed):
    result = final_candidate(feed, game)
    require(result["status"] == "structurally_corroborated_only", result["reason"])
    data = decode(feed)
    gd, plays = data["gameData"], data["liveData"]["plays"]["allPlays"]
    require(
        gd["game"]["type"] == game["raw_game"]["gameType"] == "R", "not_regular_season"
    )
    require(gd["game"]["season"] == game["source_date"][:4], "wrong_season")
    require(
        not any(
            "resum" in k.lower() or "suspend" in k.lower()
            for obj in (gd["datetime"], game["raw_game"])
            for k in obj
        ),
        "resumed_or_suspended",
    )
    require(
        not any(
            interrupted_status(s) for s in (gd["status"], game["raw_game"]["status"])
        ),
        "resumed_or_suspended",
    )
    require(
        gd["datetime"].get("originalDate", gd["datetime"]["officialDate"])
        == gd["datetime"]["officialDate"]
        == game["source_date"],
        "official_date_moved",
    )
    require(isinstance(plays, list) and bool(plays), "missing_plays")
    last = plays[-1]
    completion = instant(last["about"]["endTime"])
    require(instant(game["scheduled_start"]) < completion, "completion_before_start")
    previous = None
    for play in plays:
        start, end = instant(play["about"]["startTime"]), instant(
            play["about"]["endTime"]
        )
        require(
            play["about"]["isComplete"] is True
            and start <= end <= completion
            and (previous is None or previous <= end),
            "play_chronology",
        )
        previous = end
    require(
        [last["result"]["awayScore"], last["result"]["homeScore"]]
        == [result["away_score"], result["home_score"]],
        "last_play_score_conflict",
    )
    return {k: game[k] for k in ("game_id", "away_id", "home_id", "source_date")} | {
        "completed_at": last["about"]["endTime"],
        "home_won": int(result["home_score"] > result["away_score"]),
        "away_score": result["away_score"],
        "home_score": result["home_score"],
        "completion_pointer": f"/liveData/plays/allPlays/{len(plays)-1}/about/endTime",
        "score_pointer": "/liveData/linescore/teams",
        "identity_pointer": "/gameData/teams",
    }


def appearances(game, body, *, non_pa_classifier=None, trace=None):
    """Corroborate all listed pitchers, per-game BF/K/BB, and completed PA identity."""
    if trace is not None:
        trace.update(feed_pointer="", stage="outcome")
    final = outcome(game, body)
    data = decode(body)
    boxes = data["liveData"]["boxscore"]["teams"]
    records = {}
    for side in SIDES:
        box = boxes[side]
        if trace is not None:
            trace.update(feed_pointer=f"/liveData/boxscore/teams/{side}", stage="box")
        require(
            positive_id(box["team"]["id"]) == game[side + "_id"], "box_team_mismatch"
        )
        ids = [positive_id(p) for p in box["pitchers"]]
        require(bool(ids) and len(ids) == len(set(ids)), "box_pitcher_list")
        for pid in ids:
            require(pid not in records, "pitcher_on_both_teams")
            player = box["players"]["ID" + pid]
            require(positive_id(player["person"]["id"]) == pid, "box_pitcher_identity")
            stats = player["stats"]["pitching"]
            require(
                all(
                    type(stats[k]) is int and stats[k] >= 0
                    for k in ("battersFaced", "strikeOuts", "baseOnBalls")
                ),
                "pitching_counts",
            )
            records[pid] = {
                "pitcher_id": pid,
                "team_id": game[side + "_id"],
                "side": side,
                "game_id": game["game_id"],
                "source_date": game["source_date"],
                "scheduled_start": game["scheduled_start"],
                "completed_at": final["completed_at"],
                "feed_sha256": sha(body),
                "schedule_sha256": game["schedule_sha256"],
                "schedule_pointer": game["source_pointer"],
                "completion_pointer": final["completion_pointer"],
                "box_pointer": f"/liveData/boxscore/teams/{side}/players/ID{pid}/stats/pitching",
                "plate_appearances": 0,
                "strikeouts": 0,
                "walks": 0,
                "play_pointers": [],
                "expected": stats,
            }
    for i, play in enumerate(data["liveData"]["plays"]["allPlays"]):
        pointer = f"/liveData/plays/allPlays/{i}"
        if trace is not None:
            trace.update(feed_pointer=pointer, stage="play")
        about = play["about"]
        require(
            type(about["atBatIndex"]) is int
            and about["atBatIndex"] == i
            and type(about["isTopInning"]) is bool,
            "play_index_or_side",
        )
        require(
            instant(game["scheduled_start"]) <= instant(about["startTime"]),
            "play_before_scheduled_start",
        )
        is_pa = (
            play["result"]["type"] == "atBat"
            and play["result"]["eventType"] in PA_EVENTS
        )
        require(
            is_pa or (non_pa_classifier is not None and non_pa_classifier(play)),
            "unsupported_plate_appearance_event",
        )
        pid = positive_id(play["matchup"]["pitcher"]["id"])
        positive_id(play["matchup"]["batter"]["id"])
        events = play["playEvents"]
        require(isinstance(events, list) and bool(events), "missing_play_events")
        pitch_seen = False
        for event in events:
            require(
                type(event["isPitch"]) is bool and isinstance(event["details"], dict),
                "invalid_play_event",
            )
            if event["details"].get("eventType") == "pitching_substitution":
                require(not pitch_seen, "ambiguous_mid_appearance_pitcher_substitution")
                require(
                    positive_id(event["player"]["id"]) == pid,
                    "substitution_pitcher_mismatch",
                )
            pitch_seen = pitch_seen or event["isPitch"]
        require(pid in records, "play_pitcher_not_in_box")
        record = records[pid]
        require(
            record["side"] == ("home" if about["isTopInning"] else "away"),
            "play_pitcher_team_mismatch",
        )
        if not is_pa:
            record.setdefault("non_pa_play_pointers", []).append(pointer)
            continue
        record["plate_appearances"] += 1
        record["strikeouts"] += int(play["result"]["eventType"] in K_EVENTS)
        record["walks"] += int(play["result"]["eventType"] in BB_EVENTS)
        record["play_pointers"].append(f"/liveData/plays/allPlays/{i}")
    for record in records.values():
        if trace is not None:
            trace.update(feed_pointer=record["box_pointer"], stage="counts")
        stats = record.pop("expected")
        require(record["plate_appearances"] > 0, "appearance_without_completed_pa")
        require(
            [record[k] for k in ("plate_appearances", "strikeouts", "walks")]
            == [stats[k] for k in ("battersFaced", "strikeOuts", "baseOnBalls")],
            "pitching_count_conflict",
        )
    return list(records.values())


def participants(game, body, *, evidence_version=2):
    """Corroborate a complete final participant superset, not per-pitcher BF/K/BB.

    Completion bound is the latest completed play end, allowing nonmonotonic
    play ordering only for exclusion. Original appearance/outcome gates persist.
    """
    require(evidence_version in (1, 2), "participant_evidence_version")
    final = final_identity(game, body)
    data = decode(body)
    gd, live = data["gameData"], data["liveData"]
    require(
        gd["game"]["type"] == game["raw_game"]["gameType"] == "R"
        and gd["game"]["season"] == game["source_date"][:4],
        "participant_game_type",
    )
    require(
        gd["datetime"].get("originalDate", gd["datetime"]["officialDate"])
        == gd["datetime"]["officialDate"]
        == game["source_date"],
        "participant_date_changed",
    )
    boxes = live["boxscore"]["teams"]
    roster, pointers, totals = {}, [], {}
    zero_activity, anomalies = set(), []
    for side in SIDES:
        box = boxes[side]
        base = f"/liveData/boxscore/teams/{side}"
        require(
            positive_id(box["team"]["id"]) == game[side + "_id"], "participant_box_team"
        )
        ids = [positive_id(p) for p in box["pitchers"]]
        require(bool(ids) and len(ids) == len(set(ids)), "participant_pitcher_list")
        pitching_players = {}
        for key, player in box["players"].items():
            pid = positive_id(player["person"]["id"])
            require(key == "ID" + pid, "participant_player_key")
            stats = player["stats"]["pitching"]
            if stats:
                require(pid not in pitching_players, "participant_duplicate_player")
                pitching_players[pid] = stats
        require(set(ids) == set(pitching_players), "participant_player_census_mismatch")
        total = box["teamStats"]["pitching"]
        require(
            all(type(total[k]) is int and total[k] >= 0 for k in COUNTS),
            "participant_team_counts",
        )
        for pid in ids:
            require(pid not in roster, "participant_on_both_teams")
            stats = pitching_players[pid]
            require(
                type(stats["gamesPitched"]) is int
                and stats["gamesPitched"] in (0, 1)
                and (stats["gamesPitched"] == 1 or all(stats[k] == 0 for k in COUNTS))
                and all(type(stats[k]) is int and stats[k] >= 0 for k in COUNTS),
                "participant_counts",
            )
            if stats["gamesPitched"] == 0 and all(stats[k] == 0 for k in COUNTS):
                zero_activity.add(pid)
            roster[pid] = side
            pointers.append(f"{base}/players/ID{pid}/stats/pitching")
        require(
            all(
                sum(p[k] for p in pitching_players.values()) == total[k] for k in COUNTS
            ),
            "participant_total_conflict",
        )
        totals[side] = {k: total[k] for k in COUNTS}
        pointers.extend([base + "/pitchers", base + "/teamStats/pitching"])
    plays = live["plays"]["allPlays"]
    require(isinstance(plays, list) and bool(plays), "participant_plays_missing")
    seen, ends, pitch_counts = set(), [], {s: 0 for s in SIDES}
    for i, play in enumerate(plays):
        about = play["about"]
        terminal_rain = (
            evidence_version == 2
            and i == len(plays) - 1
            and about["isComplete"] is False
            and gd["status"]["detailedState"] == "Completed Early: Rain"
            and play["result"].get("eventType") == "game_advisory"
            and play["result"].get("isOut") is False
            and about.get("isScoringPlay") is False
        )
        require(
            type(about["atBatIndex"]) is int
            and about["atBatIndex"] == i
            and (about["isComplete"] is True or terminal_rain)
            and type(about["isTopInning"]) is bool,
            "participant_play_index_or_completion",
        )
        start, end = instant(about["startTime"]), instant(about["endTime"])
        reversed_runner = False
        if evidence_version == 2 and start > end:
            from mlb_appearance_census import runner_out

            reversed_runner = runner_out(play)
        require(start <= end or reversed_runner, "participant_play_time")
        if terminal_rain or reversed_runner:
            events = play["playEvents"]
            require(
                isinstance(events, list) and bool(events), "participant_events_missing"
            )
            event_ends = []
            for j, event in enumerate(events):
                a, b = instant(event["startTime"]), instant(event["endTime"])
                require(
                    type(event["index"]) is int
                    and event["index"] == j
                    and a <= b <= end,
                    "participant_exception_event_time",
                )
                event_ends.append(b)
            require(max(event_ends) == end, "participant_exception_end_mismatch")
            if terminal_rain:
                require(
                    all(instant(e["startTime"]) >= start for e in events),
                    "participant_exception_event_time",
                )
            anomalies.append(
                dict(
                    pointer=f"/liveData/plays/allPlays/{i}",
                    kind=(
                        "terminal_rain_unfinished_pa"
                        if terminal_rain
                        else "runner_out_reversed_outer_time"
                    ),
                )
            )
        ends.append(max(start, end))
        side = "home" if about["isTopInning"] else "away"
        pid = positive_id(play["matchup"]["pitcher"]["id"])
        require(roster.get(pid) == side, "participant_matchup_missing_or_wrong_side")
        seen.add(pid)
        pointers.append(f"/liveData/plays/allPlays/{i}/matchup/pitcher")
        events = play["playEvents"]
        require(isinstance(events, list) and bool(events), "participant_events_missing")
        for j, event in enumerate(events):
            require(type(event["isPitch"]) is bool, "participant_pitch_flag")
            pitch_counts[side] += int(event["isPitch"])
            if event["details"].get("eventType") == "pitching_substitution":
                sub = positive_id(event["player"]["id"])
                require(
                    roster.get(sub) == side,
                    "participant_substitution_missing_or_wrong_side",
                )
                seen.add(sub)
                pointers.append(f"/liveData/plays/allPlays/{i}/playEvents/{j}/player")
    unseen = set(roster) - seen
    require(
        seen == set(roster) or (evidence_version == 2 and unseen <= zero_activity),
        "participant_play_box_census_mismatch",
    )
    if evidence_version == 2 and unseen:
        anomalies.append(
            dict(
                kind="listed_zero_activity_superset",
                pitcher_ids=sorted(unseen, key=int),
            )
        )
    require(
        all(pitch_counts[s] == totals[s]["numberOfPitches"] for s in SIDES),
        "participant_pitch_total_conflict",
    )
    last = plays[-1]
    require(
        [last["result"]["awayScore"], last["result"]["homeScore"]]
        == [final["away_score"], final["home_score"]],
        "participant_terminal_score_conflict",
    )
    bound = max(ends)
    require(
        max(instant(game["scheduled_start"]), instant(gd["datetime"]["dateTime"]))
        < bound,
        "participant_completion_before_start",
    )
    certificate = dict(
        game_id=game["game_id"],
        source_date=game["source_date"],
        feed_sha256=sha(body),
        schedule_sha256=game["schedule_sha256"],
        schedule_pointer=game["source_pointer"],
        pitcher_ids=sorted(roster, key=int),
        completion_upper_bound=bound.isoformat(),
        pointers=pointers,
        status="structurally_corroborated_only",
        statistics_admitted=False,
    )

    if evidence_version == 2:
        certificate.update(evidence_version=2, anomalies=anomalies)
    return certificate


def postponed_interval(game, body):
    """Retrospective no-play interval, corroborated by postponed schedule + full feed.

    Only excludes before the explicit later reschedule start. Never treats a
    suspended game, a moved-date label alone, or a future participant roster as
    evidence that a pitcher was absent.
    """
    d = decode(body)
    gd = d["gameData"]
    raw_game = game["raw_game"]
    require(
        raw_game["status"]["detailedState"] == "Postponed"
        and raw_game["status"]["codedGameState"] == "D",
        "not_explicitly_postponed",
    )
    require(
        not interrupted_status(gd["status"])
        and not interrupted_status(raw_game["status"])
        and not any(
            "resum" in k.lower() or "suspend" in k.lower()
            for obj in (gd["datetime"], raw_game)
            for k in obj
        ),
        "postponed_resume_conflict",
    )
    require(
        positive_id(d["gamePk"]) == positive_id(gd["game"]["pk"]) == game["game_id"]
        and all(
            positive_id(gd["teams"][side]["id"]) == game[side + "_id"] for side in SIDES
        ),
        "postponed_identity_conflict",
    )
    require(
        gd["game"]["type"] == raw_game["gameType"] == "R"
        and gd["game"]["season"] == game["source_date"][:4],
        "postponed_game_type",
    )
    later = instant(raw_game["rescheduleDate"])
    require(
        gd["datetime"]["originalDate"]
        == gd["datetime"]["officialDate"]
        == raw_game["officialDate"]
        and game["source_date"] < raw_game["officialDate"]
        and instant(gd["datetime"]["dateTime"])
        == later
        > instant(game["scheduled_start"]),
        "postponed_reschedule_conflict",
    )
    require(
        gd["status"]["abstractGameState"] == "Final"
        and gd["status"]["detailedState"] == "Final",
        "postponed_feed_not_final",
    )
    plays = d["liveData"]["plays"]["allPlays"]
    require(isinstance(plays, list) and bool(plays), "postponed_plays_missing")
    for i, play in enumerate(plays):
        a = play["about"]
        require(
            type(a["atBatIndex"]) is int
            and a["atBatIndex"] == i
            and a["isComplete"] is True
            and later <= instant(a["startTime"]) <= instant(a["endTime"]),
            "postponed_prior_or_incomplete_play",
        )
    return dict(
        game_id=game["game_id"],
        source_date=game["source_date"],
        feed_sha256=sha(body),
        schedule_sha256=game["schedule_sha256"],
        schedule_pointer=game["source_pointer"],
        before_rescheduled_start=later.isoformat(),
        status="structurally_corroborated_only",
        kind="postponed_no_play_interval",
        asof_verified=False,
    )
