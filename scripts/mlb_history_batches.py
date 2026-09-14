#!/usr/bin/env python3
"""Retained monthly inventory and bounded resumable history batches; no fitting."""

import argparse
import calendar
import fcntl
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import mlb_history_expansion_probe as probe
from mlb_chronological_checkpoint import encoded
from mlb_real_shadow_capture import decode, instant, sha
from mlb_market_free_checkpoint import require

PLAN = Path(__file__).resolve().parents[1] / "docs/mlb-history-batches-plan.json"
PLAN_SHA = "139f3ad2dae5d0c0a4d3a24f3aa86bbb14dc4ca31bd838507102bc2de3853559"
ERRORS = (ValueError, KeyError, TypeError, OSError)


def spec():
    raw = PLAN.read_bytes()
    require(sha(raw) == PLAN_SHA, "plan_changed")
    return decode(raw)


def seal(path, value):
    raw = encoded(value)
    require(not path.is_symlink(), "seal_symlink")
    if path.exists():
        require(path.read_bytes() == raw, "sealed_bytes_changed")
    else:
        with path.open("xb") as f:
            f.write(raw)


def initialize(root, plan):
    require(
        not root.is_symlink() and not (root / "objects").is_symlink(), "root_symlink"
    )
    root.mkdir(parents=True, exist_ok=True)
    if not (root / "plan.json").exists():
        require(not list(root.iterdir()), "unplanned_existing_files")
    seal(root / "plan.json", plan)
    (root / "objects").mkdir(exist_ok=True)


def read_source(root, kind, key, url, acquire):
    path = root / f"{kind}-{key}.json"
    if acquire and not path.exists():
        probe.fetch(root, kind, key, url)
    receipts = []
    body = probe.retained(root, kind, key, url, False, receipts)
    return body


def months(start, end):
    current = date.fromisoformat(start)
    last = date.fromisoformat(end)
    while current <= last:
        stop = min(
            last,
            date(
                current.year,
                current.month,
                calendar.monthrange(current.year, current.month)[1],
            ),
        )
        yield current.isoformat(), stop.isoformat()
        current = stop + timedelta(days=1)


def monthly_games(body, start, end):
    data = decode(body)
    result = []
    seen = set()
    require(isinstance(data["dates"], list), "schedule_dates")
    for i, block in enumerate(data["dates"]):
        day = block["date"]
        require(start <= day <= end and day not in seen, "monthly_date")
        seen.add(day)
        # Reuse year-aware structural checks; retain pointers to ORIGINAL bytes.
        rows = probe.schedule_census(
            encoded(dict(dates=[block], totalGames=block["totalGames"])), day
        )
        for row in rows:
            row["source_pointer"] = row["source_pointer"].replace(
                "/dates/0/", f"/dates/{i}/"
            )
            row["schedule_sha256"] = sha(body)
        result.extend(rows)
    require(
        type(data["totalGames"]) is int and data["totalGames"] == len(result),
        "monthly_count",
    )
    return result


def inventory(root, acquire=False):
    p = spec()
    plan = dict(
        schema="mlb-history-inventory-plan-v1",
        spec_sha256=PLAN_SHA,
        months=list(months(p["start"], p["end"])),
    )
    if acquire:
        initialize(root, plan)
    else:
        require(
            (root / "plan.json").read_bytes() == encoded(plan), "inventory_plan_changed"
        )
    # Validate all existing monthly receipts before any missing request is fetched.
    allowed = {f"schedule-{start[:7]}.json" for start, _ in plan["months"]}
    require(
        all(
            f.name in allowed | {"plan.json", "inventory.json"}
            for f in root.glob("*.json")
        ),
        "inventory_extra_receipt",
    )
    for start, end in plan["months"]:
        key = start[:7]
        if (root / f"schedule-{key}.json").exists():
            read_source(
                root,
                "schedule",
                key,
                f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={start}&endDate={end}&hydrate=linescore",
                False,
            )
    games = []
    sources = []
    for start, end in plan["months"]:
        key = start[:7]
        url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={start}&endDate={end}&hydrate=linescore"
        body = read_source(root, "schedule", key, url, acquire)
        found = monthly_games(body, start, end)
        sources.append(
            dict(
                month=key,
                sha256=sha(body),
                receipt_sha256=sha((root / f"schedule-{key}.json").read_bytes()),
            )
        )
        games.extend(g for g in found if g["raw_game"]["gameType"] == "R")
    by_id = {}
    for game in sorted(games, key=lambda g: (g["source_date"], int(g["game_id"]))):
        by_id.setdefault(game["game_id"], []).append(game)
    targets = []
    for gid, occurrences in by_id.items():
        require(
            len({(g["away_id"], g["home_id"]) for g in occurrences}) == 1,
            "occurrence_team_conflict",
        )
        targets.append(
            dict(
                game_id=gid,
                occurrences=occurrences,
                snapshot_refusal=(
                    "repeated_schedule_occurrence" if len(occurrences) != 1 else None
                ),
            )
        )
    result = dict(
        schema="mlb-history-inventory-v1",
        plan=plan,
        sources=sources,
        regular_occurrences=len(games),
        unique_games=len(targets),
        targets=targets,
        max_game_requests=3 * len(targets),
        batches=(len(targets) + p["batch_games"] - 1) // p["batch_games"],
        eligible=False,
    )
    if acquire:
        seal(root / "inventory.json", result)
    else:
        require(
            (root / "inventory.json").read_bytes() == encoded(result),
            "inventory_replay_changed",
        )
    return result


def collect_one(root, target, acquire):
    gid = target["game_id"]
    g = target["occurrences"][0]
    url = f"https://statsapi.mlb.com/api/v1.1/game/{gid}/feed/live"
    row = dict(game_id=gid, feed_available=False, starter_candidate=False, refusal=None)
    try:
        body = read_source(root, "feed", gid, url, acquire)
        require(str(decode(body)["gamePk"]) == gid, "feed_identity")
        row.update(feed_available=True, feed_sha256=sha(body))
        require(target["snapshot_refusal"] is None, target["snapshot_refusal"])
        cutoff = instant(g["scheduled_start"]) - timedelta(minutes=60)
        codes = read_source(root, "timestamps", gid, url + "/timestamps", acquire)
        code = probe.selected_code(codes, cutoff.isoformat())
        body = read_source(root, "snapshot", gid, url + "?timecode=" + code, acquire)
        row.update(
            pitcher_ids=probe.check_snapshot(g, decode(body), code),
            starter_candidate=True,
            snapshot_sha256=sha(body),
            cutoff=cutoff.isoformat(),
        )
    except ERRORS as exc:
        row["refusal"] = str(exc)
    return row


def _batch(inventory_root, root, number, acquire=False):
    p = spec()
    source = inventory(inventory_root, False)
    require(type(number) is int and 0 <= number < source["batches"], "batch_number")
    targets = source["targets"][
        number * p["batch_games"] : (number + 1) * p["batch_games"]
    ]
    plan = dict(
        schema="mlb-history-batch-plan-v1",
        inventory_sha256=sha(encoded(source)),
        spec_sha256=PLAN_SHA,
        number=number,
        targets=targets,
    )
    if acquire:
        initialize(root, plan)
    else:
        require(
            (root / "plan.json").read_bytes() == encoded(plan), "batch_plan_changed"
        )
    # Refuse receipts outside this batch before any network request.
    allowed = {
        f'{kind}-{r["game_id"]}.json'
        for r in targets
        for kind in ("feed", "timestamps", "snapshot")
    }
    require(
        all(
            f.name in allowed | {"plan.json", "report.json"}
            for f in root.glob("*.json")
        ),
        "receipt_outside_batch",
    )
    # Verify every cached receipt/object, including failures, before resuming.
    for path in root.glob("*.json"):
        if path.name not in allowed:
            continue
        r = decode(path.read_bytes())
        gid = r["key"]
        kind = r["kind"]
        require(
            path.name == f"{kind}-{gid}.json" and path.name in allowed,
            "cached_identity",
        )
        url = f"https://statsapi.mlb.com/api/v1.1/game/{gid}/feed/live"
        if kind == "timestamps":
            url += "/timestamps"
        if kind == "snapshot":
            target = next(t for t in targets if t["game_id"] == gid)
            cutoff = instant(target["occurrences"][0]["scheduled_start"]) - timedelta(
                minutes=60
            )
            codes = read_source(root, "timestamps", gid, url + "/timestamps", False)
            url += "?timecode=" + probe.selected_code(codes, cutoff.isoformat())
        try:
            probe.retained(root, kind, gid, url, False, [])
        except ValueError as exc:
            if str(exc) != "source_unavailable":
                raise
    sealed = (root / "report.json").exists()
    with ThreadPoolExecutor(max_workers=p["max_workers"]) as pool:
        rows = list(
            pool.map(lambda t: collect_one(root, t, acquire and not sealed), targets)
        )
    receipts = [
        dict(file=f.name, sha256=sha(f.read_bytes()))
        for f in sorted(root.glob("*.json"))
        if f.name in allowed
    ]
    require(len(receipts) <= p["max_requests_per_batch"], "batch_budget")
    result = dict(
        schema="mlb-history-batch-result-v1",
        plan_sha256=sha(encoded(plan)),
        rows=rows,
        receipts=receipts,
        summary=dict(
            games=len(rows),
            feeds=sum(r["feed_available"] for r in rows),
            starter_candidates=sum(r["starter_candidate"] for r in rows),
            refusals=dict(Counter(r["refusal"] for r in rows if r["refusal"])),
        ),
        complete_training_rows=None,
        training_coverage_status="not_evaluated_requires_season_general_history_admission",
        eligible=False,
        fitting_enabled=False,
        scoring_enabled=False,
    )
    if acquire and not sealed:
        seal(root / "report.json", result)
    else:
        require(
            (root / "report.json").read_bytes() == encoded(result),
            "batch_replay_changed",
        )
    return result


def batch(inventory_root, root, number, acquire=False):
    root = Path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root.with_name(root.name + ".lock")
    require(not lock_path.is_symlink(), "batch_lock_symlink")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _batch(inventory_root, root, number, acquire)


if __name__ == "__main__":
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("mode", choices=("inventory", "batch"))
    a.add_argument("--root", type=Path, required=True)
    a.add_argument("--inventory", type=Path)
    a.add_argument("--number", type=int)
    a.add_argument("--acquire", action="store_true")
    args = a.parse_args()
    try:
        result = (
            inventory(args.root, args.acquire)
            if args.mode == "inventory"
            else batch(args.inventory, args.root, args.number, args.acquire)
        )
        print(encoded(result).decode(), end="")
    except ERRORS as exc:
        a.exit(2, str(exc) + "\n")
