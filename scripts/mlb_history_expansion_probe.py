#!/usr/bin/env python3
"""Fixed earlier-season source probe. Never acquires evaluation dates or fits."""

import argparse
from datetime import date, timedelta
from pathlib import Path

from mlb_bulk_starter_snapshots import fetch, selected_code, LIMITS
from mlb_chronological_checkpoint import encoded
from mlb_market_free_checkpoint import require, interrupted_status
from mlb_real_shadow_capture import decode, instant, positive_id, sha

PLAN = Path(__file__).resolve().parents[1] / "docs/mlb-history-expansion-plan.json"
PLAN_HASH = "3c5798d3dcc0c6e485c74de7d2f92d1d2cd6058d3137cf8330e0f41df8c1082c"


def schedule_census(raw, day):
    data = decode(raw)
    require(
        isinstance(data["dates"], list) and len(data["dates"]) <= 1, "schedule_dates"
    )
    games = []
    for block in data["dates"]:
        require(
            block["date"] == day
            and type(block["totalGames"]) is int
            and block["totalGames"] == len(block["games"]),
            "schedule_date_or_count",
        )
        for i, g in enumerate(block["games"]):
            gid = positive_id(g["gamePk"])
            a, h = [positive_id(g["teams"][s]["team"]["id"]) for s in ("away", "home")]
            require(a != h, "same_team")
            instant(g["gameDate"])
            require(
                date.fromisoformat(g["officialDate"]).year
                == date.fromisoformat(day).year,
                "official_date",
            )
            require(isinstance(g["gameType"], str) and bool(g["gameType"]), "game_type")
            games.append(
                dict(
                    game_id=gid,
                    away_id=a,
                    home_id=h,
                    scheduled_start=g["gameDate"],
                    source_date=day,
                    source_pointer=f"/dates/0/games/{i}",
                    raw_game=g,
                )
            )
    require(
        type(data["totalGames"]) is int and data["totalGames"] == len(games),
        "schedule_count",
    )
    require(
        len({g["game_id"] for g in games}) == len(games), "duplicate_game_within_date"
    )
    return games


def check_snapshot(game, data, code):
    gd = data["gameData"]
    require(data["metaData"]["timeStamp"] == code, "snapshot_time_mismatch")
    require(
        positive_id(data["gamePk"]) == positive_id(gd["game"]["pk"]) == game["game_id"],
        "snapshot_game_mismatch",
    )
    require(
        all(
            positive_id(gd["teams"][s]["id"]) == game[s + "_id"]
            for s in ("away", "home")
        ),
        "snapshot_team_mismatch",
    )
    dt = gd["datetime"]
    require(
        dt.get("originalDate", dt["officialDate"])
        == dt["officialDate"]
        == game["source_date"]
        == game["raw_game"]["officialDate"]
        and instant(dt["dateTime"]) == instant(game["scheduled_start"]),
        "snapshot_schedule_mismatch",
    )
    require(
        gd["game"]["type"] == game["raw_game"]["gameType"] == "R"
        and gd["game"]["season"] == game["source_date"][:4],
        "snapshot_season_mismatch",
    )
    require(
        gd["status"]["abstractGameState"] == "Preview"
        and not interrupted_status(gd["status"])
        and not interrupted_status(game["raw_game"]["status"])
        and not any(
            "resum" in k.lower() or "suspend" in k.lower()
            for obj in (dt, game["raw_game"])
            for k in obj
        ),
        "snapshot_not_uninterrupted_pregame",
    )
    pitchers = {
        s: positive_id(gd["probablePitchers"][s]["id"]) for s in ("away", "home")
    }
    require(pitchers["away"] != pitchers["home"], "same_probable_pitcher")
    return pitchers


def retained(root, kind, key, url, acquire, receipts):
    path = root / f"{kind}-{key}.json"
    if acquire:
        fetch(root, kind, key, url)
    require(path.is_file() and not path.is_symlink(), "receipt_missing_or_symlink")
    raw = path.read_bytes()
    r = decode(raw)
    require((r["kind"], r["key"], r["url"]) == (kind, key, url), "receipt_identity")
    require(
        instant(r["retrieved_at_local"]) <= instant(r["completed_at_local"]),
        "receipt_time_order",
    )
    require(
        type(r["size"]) is int and 0 <= r["size"] <= LIMITS["response_bytes"] + 1,
        "receipt_size",
    )
    receipts.append(
        dict(
            file=path.name,
            sha256=sha(raw),
            size=r["size"],
            http_status=r["http_status"],
            failure=r["failure"],
        )
    )
    body = None
    if r["body_sha256"] is not None:
        digest = r["body_sha256"]
        require(
            isinstance(digest, str)
            and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest),
            "object_digest",
        )
        p = root / "objects" / digest
        require(p.is_file() and not p.is_symlink(), "object_missing_or_symlink")
        body = p.read_bytes()
        require(len(body) == r["size"] and sha(body) == digest, "object_integrity")
    require(
        r["http_status"] == 200
        and r["failure"] is None
        and body is not None
        and len(body) <= LIMITS["response_bytes"],
        "source_unavailable",
    )
    return body


def probe(root, acquire=False, schedule_cache=None):
    plan_raw = PLAN.read_bytes()
    require(sha(plan_raw) == PLAN_HASH, "plan_changed")
    plan = decode(plan_raw)
    root = Path(root)
    require(
        not root.is_symlink() and not (root / "objects").is_symlink(), "root_symlink"
    )
    if acquire:
        root.mkdir(parents=True, exist_ok=False)
        (root / "objects").mkdir()
        (root / "plan.json").write_bytes(plan_raw)
        if schedule_cache is not None:
            schedule_cache = Path(schedule_cache)
            require(
                not schedule_cache.is_symlink()
                and not (schedule_cache / "objects").is_symlink(),
                "cache_symlink",
            )
            for day in plan["sample_dates"]:
                url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={day}&hydrate=linescore"
                retained(schedule_cache, "schedule", day, url, False, [])
                raw = (schedule_cache / f"schedule-{day}.json").read_bytes()
                digest = decode(raw)["body_sha256"]
                (root / "objects" / digest).write_bytes(
                    (schedule_cache / "objects" / digest).read_bytes()
                )
                (root / f"schedule-{day}.json").write_bytes(raw)

    require((root / "plan.json").read_bytes() == plan_raw, "retained_plan_changed")
    receipts, samples, dates = [], [], []
    errors = (ValueError, KeyError, TypeError, OSError)
    for day in plan["sample_dates"]:
        try:
            body = retained(
                root,
                "schedule",
                day,
                f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={day}&hydrate=linescore",
                acquire,
                receipts,
            )
            games = [
                g
                for g in schedule_census(body, day)
                if g["raw_game"]["gameType"] == "R"
            ]
            dates.append(dict(date=day, regular_occurrences=len(games), refusal=None))
        except errors as exc:
            dates.append(dict(date=day, regular_occurrences=None, refusal=str(exc)))
            continue
        for game in sorted(games, key=lambda g: int(g["game_id"]))[
            : plan["games_per_date"]
        ]:
            row = dict(
                date=day,
                game_id=game["game_id"],
                schedule_sha256=sha(body),
                schedule_pointer=game["source_pointer"],
                starter_candidate=False,
                final_feed_available=False,
                refusal=None,
            )
            url = f"https://statsapi.mlb.com/api/v1.1/game/{game['game_id']}/feed/live"
            try:
                final = retained(root, "feed", game["game_id"], url, acquire, receipts)
                d = decode(final)
                require(positive_id(d["gamePk"]) == game["game_id"], "final_identity")
                row.update(final_feed_available=True, final_feed_sha256=sha(final))
                cutoff = instant(game["scheduled_start"]) - timedelta(
                    minutes=plan["prediction_cutoff_minutes"]
                )
                codes = retained(
                    root,
                    "timestamps",
                    game["game_id"],
                    url + "/timestamps",
                    acquire,
                    receipts,
                )
                code = selected_code(codes, cutoff.isoformat())
                snapshot = retained(
                    root,
                    "snapshot",
                    game["game_id"],
                    url + "?timecode=" + code,
                    acquire,
                    receipts,
                )
                pitchers = check_snapshot(game, decode(snapshot), code)
                row.update(
                    starter_candidate=True,
                    probable_pitcher_ids=pitchers,
                    cutoff=cutoff.isoformat(),
                    timecode=code,
                    snapshot_sha256=sha(snapshot),
                )
            except errors as exc:
                row["refusal"] = str(exc)
            samples.append(row)
    require(len(receipts) <= plan["max_requests"], "request_budget")
    result = dict(
        schema="mlb-history-expansion-probe-v1",
        plan_sha256=sha(plan_raw),
        implementation_sha256=sha(Path(__file__).read_bytes()),
        transport_sha256=sha(
            Path(__file__).with_name("mlb_bulk_starter_snapshots.py").read_bytes()
        ),
        dates=dates,
        samples=samples,
        receipts=receipts,
        request_count=len(receipts),
        retained_response_bytes=sum(r["size"] for r in receipts),
        bulk_coverage_established=False,
        final_statistics_admitted=False,
        fitting_enabled=False,
        scoring_enabled=False,
        eligible=False,
        independent_pregame_timing_verified=False,
    )
    if acquire:
        (root / "report.json").write_bytes(encoded(result))
    else:
        require(
            (root / "report.json").read_bytes() == encoded(result), "replay_changed"
        )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--acquire", action="store_true")
    p.add_argument("--schedule-cache", type=Path)
    args = p.parse_args()
    try:
        print(
            encoded(probe(args.root, args.acquire, args.schedule_cache)).decode(),
            end="",
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        p.exit(2, str(exc) + "\n")
