#!/usr/bin/env python3
"""Offline participant-exclusion experiment; never repairs appearance statistics."""

import argparse
from collections import Counter
from copy import deepcopy
from pathlib import Path

from mlb_bulk_starter_admission import SIDES, history
from mlb_bulk_starter_snapshots import plan
from mlb_chronological_admission import ERRORS
from mlb_chronological_checkpoint import encoded
from mlb_market_free_checkpoint import interrupted_status, require
from mlb_real_shadow_capture import decode, instant, positive_id, sha
from mlb_recovered_history_experiment import experiment as recovered_experiment

COUNTS = ("battersFaced", "strikeOuts", "baseOnBalls", "outs", "numberOfPitches")


def final_identity(game, body):
    """Final identity for absence only, including early endings and linked resumes."""
    d = decode(body)
    gd, original = d["gameData"], game["raw_game"]
    require(
        positive_id(d["gamePk"]) == positive_id(gd["game"]["pk"]) == game["game_id"]
        and all(
            positive_id(gd["teams"][side]["id"]) == game[side + "_id"] for side in SIDES
        ),
        "participant_final_identity_mismatch",
    )
    dt = gd["datetime"]
    require(
        dt["officialDate"] == original["officialDate"] == game["source_date"]
        and dt.get("originalDate", dt["officialDate"]) == dt["officialDate"],
        "participant_final_date_mismatch",
    )
    if "resumeDateTime" in dt:
        require(
            instant(dt["resumedFromDateTime"]) == instant(game["scheduled_start"])
            and dt["resumedFromDate"] == game["source_date"]
            and instant(dt["resumeDateTime"])
            == instant(dt["dateTime"])
            > instant(game["scheduled_start"])
            and dt["resumeDate"] > dt["officialDate"],
            "participant_resume_link_mismatch",
        )
    else:
        require(
            not any(
                "resum" in k.lower() or "suspend" in k.lower()
                for obj in (dt, original)
                for k in obj
            )
            and instant(dt["dateTime"]) == instant(game["scheduled_start"]),
            "participant_final_schedule_mismatch",
        )
    statuses = (gd["status"], original["status"])
    require(
        all(
            v.get("abstractGameState") == "Final"
            and v.get("detailedState")
            in ("Final", "Completed Early", "Completed Early: Rain")
            and not interrupted_status(v)
            for v in statuses
        ),
        "participant_not_terminal_in_both_sources",
    )
    require(
        (statuses[0]["detailedState"] == "Final")
        == (statuses[1]["detailedState"] == "Final"),
        "participant_terminal_status_conflict",
    )
    if statuses[0]["detailedState"] != "Final":
        require(
            all(
                v.get("statusCode") == "FR" and v.get("reason") == "Rain"
                for v in statuses
            ),
            "participant_early_end_not_corroborated",
        )
    scores = [d["liveData"]["linescore"]["teams"][side]["runs"] for side in SIDES]
    expected = [original["teams"][side]["score"] for side in SIDES]
    require(
        all(type(v) is int and v >= 0 for v in scores + expected)
        and scores == expected
        and scores[0] != scores[1],
        "participant_final_score_conflict",
    )
    return dict(away_score=scores[0], home_score=scores[1])


def participants(game, body, *, evidence_version=1):
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
        and gd["game"]["season"] == "2025",
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
        and gd["game"]["season"] == "2025",
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


def certify(games, sources, gaps, *, evidence_version=1):
    """Use the unique matching final occurrence even for postponed duplicate dates."""
    certificates = {}
    for gid in sorted({g["game_id"] for g in gaps}, key=int):
        row = dict(
            game_id=gid,
            certificate=None,
            refusal=None,
            postponed_intervals=[],
            postponed_refusals=[],
        )
        try:
            pair = sources.get(("feed", gid))
            require(
                pair is not None and pair[1] is not None, "participant_feed_unavailable"
            )
            for game in games:
                if (
                    game["game_id"] == gid
                    and game["raw_game"]["status"].get("detailedState") == "Postponed"
                ):
                    try:
                        row["postponed_intervals"].append(
                            postponed_interval(game, pair[1])
                        )
                    except ERRORS as exc:
                        row["postponed_refusals"].append(
                            dict(source_date=game["source_date"], refusal=str(exc))
                        )
            matching = []
            for game in games:
                if game["game_id"] != gid:
                    continue
                try:
                    final_identity(game, pair[1])
                    matching.append(game)
                except ERRORS:
                    continue
            require(len(matching) == 1, "participant_unique_final_schedule_unavailable")
            require(
                all(
                    all(game[key] == matching[0][key] for key in ("away_id", "home_id"))
                    and game["raw_game"]["gameType"] == "R"
                    for game in games
                    if game["game_id"] == gid
                ),
                "participant_schedule_occurrence_identity_conflict",
            )
            row["certificate"] = participants(
                matching[0], pair[1], evidence_version=evidence_version
            )
        except ERRORS as exc:
            row["refusal"] = str(exc)
        certificates[gid] = row
    return certificates


def narrow(base, certificates):
    result = deepcopy(base)
    for row in result["occurrences"]:
        if row["histories"] is None:
            continue
        cutoff = instant(row["observation_cutoff"])
        for side, old in row["histories"].items():
            pid = old["pitcher_id"]
            retained, excluded = [], []
            for gap in old["unresolved_games"]:
                entry = certificates.get(gap["game_id"], {})
                interval = next(
                    (
                        c
                        for c in entry.get("postponed_intervals", [])
                        if c.get("kind") == "postponed_no_play_interval"
                        and c.get("status") == "structurally_corroborated_only"
                        and c["game_id"] == gap["game_id"]
                        and c["source_date"] == gap["source_date"]
                        and c["feed_sha256"] == gap["feed_sha256"]
                        and cutoff < instant(c["before_rescheduled_start"])
                    ),
                    None,
                )
                if interval is not None:
                    excluded.append(
                        dict(
                            game_id=gap["game_id"],
                            source_date=gap["source_date"],
                            certificate_sha256=sha(encoded(interval)),
                            reason="postponed_before_reschedule",
                        )
                    )
                    continue
                cert = entry.get("certificate")
                if (
                    cert is not None
                    and cert.get("status") == "structurally_corroborated_only"
                    and bool(cert.get("pitcher_ids"))
                    and cert["feed_sha256"] == gap["feed_sha256"]
                    and cert["game_id"] == gap["game_id"]
                    and instant(cert["completion_upper_bound"]) < cutoff
                ):
                    prior = old["appearances"]
                    older = len(prior) == 3 and instant(
                        cert["completion_upper_bound"]
                    ) < min(instant(a["completed_at"]) for a in prior)
                    if pid not in cert["pitcher_ids"] or older:
                        excluded.append(
                            dict(
                                game_id=gap["game_id"],
                                source_date=gap["source_date"],
                                certificate_sha256=sha(encoded(cert)),
                                reason=(
                                    "corroborated_nonparticipant"
                                    if pid not in cert["pitcher_ids"]
                                    else "completed_before_selected_three"
                                ),
                            )
                        )
                        continue
                retained.append(gap)
            new = history(
                row, pid, old["appearances"], retained, cutoff, base["census_complete"]
            )
            new["excluded_gaps"] = excluded
            row["histories"][side] = new
        admitted = all(row["histories"][s]["refusal"] is None for s in SIDES)
        row.update(
            features_admitted=admitted,
            refusal=None if admitted else "pitcher_history_refused",
        )
        for feature in ("strikeout_fraction", "walk_fraction"):
            row[feature + "_difference"] = (
                row["histories"]["home"][feature] - row["histories"]["away"][feature]
                if admitted
                else None
            )
    result["summary"] = {}
    for split, before in base["summary"].items():
        rows = [r for r in result["occurrences"] if r["split"] == split]
        result["summary"][split] = dict(
            starter_candidates=before["starter_candidates"],
            feature_pairs_before=before["feature_pairs_after"],
            feature_pairs_after=sum(r["features_admitted"] for r in rows),
            history_refusals=dict(
                Counter(
                    h["refusal"]
                    for r in rows
                    if r["histories"] is not None
                    for h in r["histories"].values()
                    if h["refusal"]
                )
            ),
        )
    result.update(
        schema="mlb-participant-history-experiment-v1",
        history_scope="Original last-three selection and cutoffs; exclude certified absence, certified older completions, or corroborated postponed intervals",
        participant_certificates=certificates,
        certificate_summary=dict(
            accepted=sum(r["certificate"] is not None for r in certificates.values()),
            postponed_intervals=sum(
                len(r.get("postponed_intervals", [])) for r in certificates.values()
            ),
            refused=dict(
                Counter(r["refusal"] for r in certificates.values() if r["refusal"])
            ),
        ),
    )
    return result


def experiment(bundle, snapshots, baseline, *, evidence_version=1):
    base = recovered_experiment(bundle, snapshots, baseline)
    planned, games, sources = plan(bundle)
    require(
        planned["bundle_sha256"] == base["bundle_sha256"], "participant_bundle_mismatch"
    )
    certificates = certify(
        games, sources, base["remaining_gaps"], evidence_version=evidence_version
    )
    result = narrow(base, certificates)
    result["schema"] = f"mlb-participant-history-experiment-v{evidence_version}"
    result.update(
        parent_replay_sha256=sha(encoded(base)),
        participant_adapter_sha256=sha(Path(__file__).read_bytes()),
    )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bundle", required=True, type=Path)
    p.add_argument("--snapshot-dir", required=True, type=Path)
    p.add_argument("--baseline-replay", required=True, type=Path)
    p.add_argument("--evidence-version", type=int, choices=(1, 2), default=1)
    args = p.parse_args()
    try:
        print(
            encoded(
                experiment(
                    args.bundle,
                    args.snapshot_dir,
                    args.baseline_replay,
                    evidence_version=args.evidence_version,
                )
            ).decode(),
            end="",
        )
    except (OSError, *ERRORS) as exc:
        p.exit(2, str(exc) + "\n")
