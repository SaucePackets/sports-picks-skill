#!/usr/bin/env python3
"""Bounded pregame research follow-ups, separate from executable watchlists.

Writes only .picks/research. A ready packet is a producer handoff, not a pick.
Normalized provider responses are retained as diagnostics, never model-admission
receipts. This worker cannot write a schedule, approve a card, or place an order.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import urllib.request
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import time
import os
from zoneinfo import ZoneInfo

import mlb_data_completeness as completeness
import mlb_stage2_scan as scanner

SCHEMA = "mlb-research-queue-v1"
INTERVAL = timedelta(minutes=15)
WINDOW = timedelta(hours=3)
CUTOFF = timedelta(minutes=15)
MAX_ATTEMPTS = 12
BATCH_SIZE = 4
TERMINAL = {"expired", "exhausted", "invalid_identity", "occupied", "decision_recorded"}


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encode(value))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def retain(directory, value):
    raw = encode(value)
    path = directory / "objects" / (digest(raw) + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError("retained object differs from its digest")
    return str(path.relative_to(directory))


def identity(row):
    if type(row.get("game_pk")) is not int or row["game_pk"] <= 0:
        raise ValueError("missing numeric MLB game identity")
    for field in ("event_id", "away", "home", "time"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"missing {field}")
    instant(row["time"])
    return tuple(row[k] for k in ("game_pk", "event_id", "away", "home", "time"))


def refresh(row, now, *, fetch=None):
    """Re-fetch missing inputs for one game; preserve timestamps on retained data.

    Feed identity and start must agree before using any refreshed fields. Lineup
    refresh always uses the live feed; other data is fetched only when missing.
    A producer must still refresh its prices and all final card evidence.
    """
    result = copy.deepcopy(row)
    responses = []
    original_get = scanner.get
    started = time.monotonic()

    def get(url, *, as_text=False):
        remaining = 60 - (time.monotonic() - started)
        if remaining <= 0 or len(responses) >= 12:
            raise TimeoutError("per-game research request/time budget exhausted")
        # One request per attempt. Retrying belongs to the durable queue, not
        # nested HTTP retries that can consume the entire pregame window.
        timeout = min(10, remaining)
        if fetch is not None:
            value = fetch(url, timeout)
        elif as_text:
            request = urllib.request.Request(
                url, headers={"User-Agent": "HermesSportsPicks/1.0"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("research CSV response exceeds 1 MiB limit")
            value = raw.decode("utf-8-sig")
        else:
            value = scanner.fetch_json(url, timeout=timeout, attempts=1)
        responses.append(
            {
                "url": url,
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "normalized_response": value,
            }
        )
        return value

    missing = set(completeness.assess(row, now)["missing_fields"])
    feed = get(f"https://statsapi.mlb.com/api/v1.1/game/{row['game_pk']}/feed/live")
    data = feed["gameData"]
    if feed["gamePk"] != row["game_pk"] or instant(
        data["datetime"]["dateTime"]
    ) != instant(row["time"]):
        raise ValueError(
            "provider game/start identity changed; producer rescan required"
        )
    if data["status"]["abstractGameState"] != "Preview":
        raise ValueError("provider game is no longer pregame")
    for side in ("away", "home"):
        if data["teams"][side]["id"] != row[f"{side}_team_id"]:
            raise ValueError("provider team identity changed")
    game = {
        "gamePk": row["game_pk"],
        "gameDate": row["time"],
        "teams": {s: {"team": data["teams"][s]} for s in ("away", "home")},
    }
    collector = scanner.MlbSlateCollector(
        now.astimezone(ZoneInfo("America/Chicago")).date().isoformat(), now.year
    )
    stamps = result.setdefault("research_field_observations", {})
    # The collector is synchronous. Restore its module dependency even on errors.
    scanner.get = get
    try:
        for side in ("away", "home"):
            if f"{side}_lineup" in missing:
                observed = responses[0]["retrieved_at_utc"]
                result[f"{side}_lineup"] = completeness.lineup_from_feed(
                    feed, game, side, observed
                )
                stamps[f"{side}_lineup"] = observed
            for suffix, method in (
                ("form", collector.team_form),
                ("bullpen", collector.bullpen),
            ):
                field = f"{side}_{suffix}"
                if field in missing:
                    result[field] = method(row[f"{side}_team_id"])
                    stamps[field] = datetime.now(timezone.utc).isoformat()
            probable = data.get("probablePitchers", {}).get(side, {})
            if (
                probable.get("fullName") != row.get(f"{side}_starter")
                or f"{side}_starter_stats" in missing
            ):
                result[f"{side}_starter"] = probable.get("fullName")
                result[f"{side}_starter_stats"] = collector.pitcher_stats(
                    probable.get("id")
                )
                stamps[f"{side}_starter_stats"] = datetime.now(timezone.utc).isoformat()
        if {"away_injuries", "home_injuries"} & missing or not completeness.assess(
            row, now
        )["prices_available"]:
            day = now.astimezone(ZoneInfo("America/Chicago")).date().isoformat()
            scoreboard = get(
                "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates="
                + day.replace("-", "")
                + "&limit=100"
            )
            events = [
                e
                for e in scoreboard.get("events", [])
                if str(e.get("id")) == row["event_id"]
            ]
            if len(events) != 1 or instant(events[0]["date"]) != instant(row["time"]):
                raise ValueError("ESPN event/start identity mismatch")
            competition = events[0]["competitions"][0]
            teams = {c["homeAway"]: c["team"] for c in competition["competitors"]}
            for side in ("away", "home"):
                if completeness.canonical_team(
                    teams[side]["abbreviation"]
                ) != completeness.canonical_team(row[f"{side}_abbr"]):
                    raise ValueError("ESPN team identity mismatch")
                if f"{side}_injuries" in missing:
                    result[f"{side}_injuries"] = collector.injuries(
                        str(teams[side]["id"])
                    )
                    stamps[f"{side}_injuries"] = datetime.now(timezone.utc).isoformat()
            if not completeness.assess(row, now)["prices_available"]:
                moneyline = (competition.get("odds") or [{}])[0].get("moneyline") or {}
                odds = [
                    moneyline.get(side, {}).get("close", {}).get("odds")
                    for side in ("away", "home")
                ]
                fair = scanner.devig(*odds)
                for side, price, probability in zip(("away", "home"), odds, fair):
                    result[f"{side}_ml"] = price
                    result[f"{side}_fair"] = probability
                    stamps[f"{side}_fair"] = datetime.now(timezone.utc).isoformat()
        if {"away_offense", "home_offense"} & missing:
            # Fetch once through this attempt's budget. The scanner's daily
            # cache cannot be relabeled as a newly observed source response.
            text = get(
                "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
                f"?type=batter-team&year={now.year}&position=&team=&csv=true",
                as_text=True,
            )
            offense = completeness.canonical_offense(
                {
                    r["team_id"]
                    .strip()
                    .upper(): {"woba": float(r["woba"]), "xwoba": float(r["est_woba"])}
                    for r in csv.DictReader(io.StringIO(text))
                    if r.get("team_id", "").strip()
                }
            )
            if not offense:
                raise ValueError("no usable refreshed offense rows")
            for side in ("away", "home"):
                field = f"{side}_offense"
                if field in missing:
                    result[field] = offense.get(
                        completeness.canonical_team(row[f"{side}_abbr"])
                    )
                    stamps[field] = datetime.now(timezone.utc).isoformat()
    finally:
        scanner.get = original_get
    # Unresolvable identities/collection gaps stay explicit. Never infer ESPN
    # identity from an MLB numeric ID or relabel retained observations fresh.
    return result, responses


def run_cycle(root, day, *, now=None, refresh_fn=refresh):
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None or datetime.fromisoformat(day).date().isoformat() != day:
        raise ValueError("canonical day and timezone-aware clock required")
    directory = root / ".picks" / "research" / day
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "cycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}
        return _cycle(root, day, directory, now, refresh_fn)


def _cycle(root, day, directory, now, refresh_fn):
    path = directory / "queue.json"
    state = (
        json.loads(path.read_text())
        if path.exists()
        else {"schema": SCHEMA, "day": day, "games": {}}
    )
    if (
        state.get("schema") != SCHEMA
        or state.get("day") != day
        or not isinstance(state.get("games"), dict)
    ):
        raise ValueError("invalid research queue")
    source = root / ".picks" / "tmp" / f"stage2-{day}.json"
    if not source.exists() or not source.with_suffix(".coverage.json").exists():
        # Existing work must still expire when its source disappears.
        for item in state["games"].values():
            if item["status"] not in TERMINAL and now >= instant(item["deadline"]):
                item.update(
                    status="expired", reason="pregame deadline reached; source missing"
                )
        state.update(
            as_of_utc=now.isoformat(),
            source_status="missing_scan" if not source.exists() else "unverified_scan",
        )
        atomic(path, state)
        atomic(
            directory / "producer-handoff.json",
            {
                "schema": SCHEMA,
                "day": day,
                "execution_enabled": False,
                "games": [],
                "reason": "source missing or unverified",
            },
        )
        return state
    raw = source.read_bytes()
    rows = json.loads(raw)
    if (
        not isinstance(rows, list)
        or not completeness.coverage_for_scan(source, rows, now, scan_bytes=raw)[
            "reconciled"
        ]
    ):
        raise ValueError("research requires a reconciled, byte-bound scan")
    identities = [identity(row) for row in rows]
    if len({x[0] for x in identities}) != len(rows):
        raise ValueError("duplicate game identity")
    if any(
        instant(row["time"]).astimezone(ZoneInfo("America/Chicago")).date().isoformat()
        != day
        for row in rows
    ):
        raise ValueError("scan game outside Central slate day")
    schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
    schedule = json.loads(schedule_path.read_text()) if schedule_path.exists() else {}
    occupied = set()
    for kind in ("candidates", "lineup_watchlist"):
        for card in schedule.get(kind, []):
            # Identity resolution here is conservative: either ID claims the
            # game, so ambiguous cards block research rather than get replaced.
            for row in rows:
                if (
                    card.get("game_pk") == row["game_pk"]
                    or str(card.get("event_id")) == row["event_id"]
                ):
                    occupied.add(str(row["game_pk"]))
    current_ids = {str(row["game_pk"]) for row in rows}
    for key, item in state["games"].items():
        if key not in current_ids and item["status"] not in TERMINAL:
            item.update(
                status="invalid_identity",
                reason="game absent from current source roster",
            )
    # A schedule decision is acknowledged only when the shared recorder accepts
    # it and the read is bound to this scan. This is recorded completion, not
    # proof of independent provider timing or executable candidate eligibility.
    decisions = {}
    if schedule:
        import mlb_game_reads
        from mlb_runtime_policy import load_mlb_selection_policy
        from mlb_slate_receipt import read_scan_bindings

        if not mlb_game_reads.validate_with_denominator(
            schedule_path, schedule, load_mlb_selection_policy()
        ):
            current = set(read_scan_bindings(schedule, digest(raw))["current_scan"])
            decisions = {
                str(r["game_pk"]): r
                for r in schedule.get("game_reads", [])
                if r.get("game_pk") in current
                and r.get("disposition") in {"pass", "candidate", "lineup_watchlist"}
            }
    for row in rows:
        key = str(row["game_pk"])
        item = state["games"].get(key)
        assessment = completeness.assess(row, now)
        if item is None:
            first_pitch = instant(row["time"])
            item = {
                "identity": list(identity(row)),
                "first_seen": now.isoformat(),
                "status": "pending",
                "deadline": (first_pitch - CUTOFF).isoformat(),
                "attempts": [],
                "next_retry": max(now, first_pitch - WINDOW).isoformat(),
                "row": row,
                "initial_scan_sha256": digest(raw),
                "initial_row": retain(directory, row),
            }
            state["games"][key] = item
        elif list(identity(row)) != item["identity"]:
            item.update(
                status="invalid_identity",
                reason="scan identity/start changed; producer rescan/review required",
            )
        if key in decisions and assessment["status"] == "ready_for_evaluation":
            item.update(
                status="decision_recorded",
                disposition=decisions[key]["disposition"],
                schedule_sha256=digest(schedule_path.read_bytes()),
                reason="current-scan decision recorded",
            )
        if key in occupied:
            item.update(
                status="occupied", reason="existing candidate/watchlist owns this game"
            )
        if item["status"] in TERMINAL:
            continue
        if now >= instant(item["deadline"]):
            item.update(status="expired", reason="pregame research deadline reached")
            continue
        # A fresh producer scan can resolve gaps; it never resets attempt caps.
        if digest(raw) != item.get("latest_scan_sha256"):
            item["row"] = row
            item["latest_scan_sha256"] = digest(raw)
        item["missing_fields"] = completeness.assess(item["row"], now)["missing_fields"]
        if assessment["status"] == "ready_for_evaluation":
            item.update(
                status="ready_for_producer",
                reason="source data ready; handicap and current prices still required",
            )
        elif item["status"] == "ready_for_producer" and item["missing_fields"]:
            item.update(status="pending", next_retry=now.isoformat())
    due = sorted(
        (
            (key, item)
            for key, item in state["games"].items()
            if item["status"] == "pending" and instant(item["next_retry"]) <= now
        ),
        key=lambda pair: (pair[1]["next_retry"], pair[1]["deadline"], pair[0]),
    )[:BATCH_SIZE]
    for key, item in due:
        if len(item["attempts"]) >= MAX_ATTEMPTS:
            item.update(
                status="exhausted", reason="bounded research retry budget exhausted"
            )
            continue
        attempt = {
            "started_at": now.isoformat(),
            "input": retain(directory, item["row"]),
        }
        # Claim durably before I/O: a crash counts as an attempt, not an unlimited retry.
        item["attempts"].append(attempt)
        item["next_retry"] = (now + INTERVAL).isoformat()
        atomic(path, state)
        try:
            updated, responses = refresh_fn(copy.deepcopy(item["row"]), now)
            if identity(updated) != tuple(item["identity"]):
                raise ValueError("refresh identity mismatch")
            completed = (
                max(now, datetime.now(timezone.utc)) if refresh_fn is refresh else now
            )
            attempt["evidence"] = retain(
                directory, {"row": updated, "responses": responses}
            )
            item["row"] = updated
            assessment = completeness.assess(updated, completed)
            item["missing_fields"] = assessment["missing_fields"]
            attempt.update(
                completed_at=completed.isoformat(), status=assessment["status"]
            )
            if completed >= instant(item["deadline"]):
                item.update(
                    status="expired", reason="refresh completed after pregame deadline"
                )
            elif assessment["status"] == "ready_for_evaluation":
                item.update(
                    status="ready_for_producer",
                    reason="research recovered inputs; current handicap/price required",
                )
            else:
                item["reason"] = "inputs still missing: " + ", ".join(
                    assessment["missing_fields"]
                )
        except Exception as exc:
            attempt.update(status="error", error=f"{type(exc).__name__}: {exc}")
            item["reason"] = attempt["error"]
        if item["status"] == "pending" and len(item["attempts"]) >= MAX_ATTEMPTS:
            item.update(
                status="exhausted", reason="bounded research retry budget exhausted"
            )
        atomic(path, state)
    state.update(
        as_of_utc=now.isoformat(), source_status="reconciled", scan_sha256=digest(raw)
    )
    atomic(path, state)
    ready = [
        {
            "game_pk": int(key),
            "deadline": item["deadline"],
            "reason": item["reason"],
            "row": item["row"],
            "attempts": item["attempts"],
        }
        for key, item in state["games"].items()
        if item["status"] == "ready_for_producer"
    ]
    atomic(
        directory / "producer-handoff.json",
        {
            "schema": SCHEMA,
            "day": day,
            "as_of_utc": now.isoformat(),
            "execution_enabled": False,
            "instructions": "Research only. Re-fetch current prices and card evidence. Use nonce-bound full-slate writer; never approve or execute. Preserve occupied cards. Data-ready is not an official pick.",
            "games": ready,
        },
    )
    return state


def expire_previous(root, day, *, now=None):
    """Close abandoned prior-day work without reading/fetching its old scan."""
    now = now or datetime.now(timezone.utc)
    for old in (root / ".picks" / "research").glob("????-??-??"):
        try:
            valid = datetime.fromisoformat(old.name).date().isoformat() == old.name
        except ValueError:
            continue
        path = old / "queue.json"
        if not valid or old.name >= day or not path.exists():
            continue
        with (old / "cycle.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            state = json.loads(path.read_text())
            if state.get("schema") != SCHEMA or state.get("day") != old.name:
                raise ValueError("invalid previous research queue")
            for item in state["games"].values():
                if item["status"] not in TERMINAL:
                    item.update(
                        status="expired",
                        reason="slate day ended without a recorded decision",
                    )
            state["as_of_utc"] = now.isoformat()
            atomic(path, state)
            atomic(
                old / "producer-handoff.json",
                {
                    "schema": SCHEMA,
                    "day": old.name,
                    "execution_enabled": False,
                    "games": [],
                    "reason": "slate day ended",
                },
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--day", required=True)
    args = parser.parse_args()
    state = run_cycle(args.root, args.day)
    print(
        json.dumps(
            {
                "status": state.get("source_status", state.get("status")),
                "games": {k: v["status"] for k, v in state.get("games", {}).items()},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
