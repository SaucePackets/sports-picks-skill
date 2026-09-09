#!/usr/bin/env python3
"""Validate NFL assessments and write an immutable, manual-only report bundle.

No network or execution. Evidence supplied by the analyst is checked for schema,
identity, timestamps and consistency, not authenticated as a provider response.
The CLI --verify boundary must run before a cron calls its analysis complete.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

VERSION = 1
UTC = dt.timezone.utc
GATES = (
    "qb_status_gate", "backup_qb_dropoff_unpriced", "injury_cluster_gate",
    "my_defense_late_game_survival", "rest_travel_gate", "weather_gate",
    "week_to_week_overreaction_gate", "price_discipline", "real_winner_conviction",
    "opponent_fade_trap",
)
SECTIONS = ("form", "qb", "trenches_defense", "spot", "market", "the_question")
DRAFT_FIELDS = {"event_id", "side", "confidence", "unit_size", "win_probability",
                "thesis", "market_explanation", "sections", "gates", "evidence"}
KINDS = {"qb_confirmation", "player_availability", "efficiency", "offseason_review",
         "sportsbook", "exchange", "analysis"}
MAX_CONTEXT_AGE = dt.timedelta(hours=6)
MAX_PRICE_AGE = dt.timedelta(minutes=15)


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def stamp(value):
    require(isinstance(value, str), "timestamp must be a string")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Invalid("invalid timestamp") from exc
    require(parsed.tzinfo is not None, "timestamp needs timezone")
    return parsed.astimezone(UTC)


def fresh(value, now, age):
    parsed = stamp(value)
    require(dt.timedelta(0) <= now - parsed <= age, "stale or future evidence timestamp")


def number(value, low, high, label):
    require(not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and low <= value <= high, f"invalid {label}")
    return value


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"missing {label}")
    return value


def odds(value):
    require(type(value) is int and abs(value) >= 100, "price must be signed American-odds integer")
    return value


def fair(away, home):
    def implied(price):
        odds(price)
        return 100 / (price + 100) if price > 0 else -price / (-price + 100)
    a, h = implied(away), implied(home)
    return {"away": a / (a + h), "home": h / (a + h)}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def load_json(raw):
    def pairs(values):
        result = {}
        for key, val in values:
            require(key not in result, "duplicate JSON key")
            result[key] = val
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(Invalid("nonfinite JSON")))
    except (ValueError, TypeError) as exc:
        raise Invalid(str(exc)) from exc


def contexts(scan, now):
    require(isinstance(scan, list) and bool(scan), "scan must contain games")
    result = {}
    for row in scan:
        require(isinstance(row, dict), "malformed scan row")
        require(type(row.get("context_version")) is int and row["context_version"] == VERSION and row.get("collector_only") is True,
                "expected versioned collector-only context; rescan with current collector")
        event = text(row.get("event_id"), "event_id")
        require(event not in result, "duplicate scanned event")
        fresh(row.get("collected_at"), now, MAX_CONTEXT_AGE)
        require(stamp(row.get("time")) > now, "game has started")
        # A partial row remains diagnosable, but can never pass completion.
        if "error" not in row:
            for key in ("away_id", "home_id", "away", "home"):
                text(row.get(key), key)
            require(row["away_id"] != row["home_id"], "same team on both sides")
            require(type(row.get("season")) is int and type(row.get("week")) is int
                    and 1 <= row["week"] <= 23 and type(row.get("seasontype")) is int and row["seasontype"] in (1, 2, 3), "invalid NFL season/week")
        result[event] = row
    return result


def skeleton(scan, run_id, date, now):
    rows = contexts(scan, now)
    return {"version": VERSION, "run_id": run_id, "date": date, "assessments": [
        {"event_id": event, "side": None, "confidence": None, "unit_size": None,
         "win_probability": None, "thesis": "", "market_explanation": "",
         "sections": {key: "" for key in SECTIONS},
         "gates": {key: {"status": "unknown", "reason": "Analysis required", "evidence_refs": []}
                   for key in GATES}, "evidence": []} for event in rows]}


def evidence_index(entries, row, now):
    require(isinstance(entries, list), "evidence must be a list")
    result = {}
    for entry in entries:
        require(isinstance(entry, dict), "invalid evidence entry")
        key = text(entry.get("id"), "evidence id")
        require(not key.startswith("scan.") and key not in result, "duplicate/reserved evidence id")
        require(entry.get("event_id") == row["event_id"]
                and stamp(entry.get("kickoff_utc")) == stamp(row["time"]), "evidence game mismatch")
        teams = entry.get("team_ids")
        require(isinstance(teams, list) and len(teams) in (1, 2) and len(set(teams)) == len(teams)
                and all(team in (row["away_id"], row["home_id"]) for team in teams), "evidence team mismatch")
        kind = entry.get("kind")
        require(kind in KINDS, "unknown evidence kind")
        require(entry.get("status") in ("available", "unavailable"), "unattempted evidence is not retrieved")
        source = urlparse(text(entry.get("source"), "source URL"))
        require(source.scheme == "https" and bool(source.hostname), "evidence requires HTTPS source")
        fresh(entry.get("observed_at"), now, MAX_PRICE_AGE if kind in ("sportsbook", "exchange") else MAX_CONTEXT_AGE)
        text(entry.get("summary"), "evidence summary")
        require(isinstance(entry.get("data"), dict), "evidence data must be an object")
        if entry["status"] == "unavailable":
            text(entry.get("reason"), "unavailable reason")
        result[key] = entry
    return result


def weather_ready(row, now):
    weather = row.get("weather", {})
    indoor = (row.get("venue") or {}).get("indoor")
    if indoor is True:
        return weather.get("status") == "not_applicable"
    if indoor is not False or weather.get("status") != "retrieved":
        return False
    require((weather.get("venue") or {}).get("id") == (row.get("venue") or {}).get("id")
            and bool((row.get("venue") or {}).get("id")), "weather venue identity mismatch")
    require(stamp(weather.get("kickoff")) == stamp(row["time"]), "weather kickoff mismatch")
    fresh(weather.get("retrieved_at"), now, MAX_CONTEXT_AGE)
    number(weather.get("latitude"), -90, 90, "latitude")
    number(weather.get("longitude"), -180, 180, "longitude")
    expected_units = {"temperature_2m": "°F", "wind_speed_10m": "mp/h", "wind_gusts_10m": "mp/h",
                      "precipitation": "inch", "snowfall": "inch"}
    require(all(weather.get("units", {}).get(k) == v for k, v in expected_units.items()), "weather units mismatch")
    samples = weather.get("samples")
    require(isinstance(samples, list) and len(samples) == 5, "incomplete kickoff weather window")
    start = stamp(row["time"]).replace(minute=0, second=0, microsecond=0)
    for i, sample in enumerate(samples):
        require(stamp(sample.get("time_utc")) == start + dt.timedelta(hours=i), "weather hour mismatch")
        for field in expected_units:
            number(sample.get(field), -150 if field == "temperature_2m" else 0, 300, field)
    return True


def assess(row, draft, now):
    require(isinstance(draft, dict) and set(draft) <= DRAFT_FIELDS, "unknown assessment field (producer cannot set decisions)")
    if "error" in row:
        return {"event_id": row["event_id"], "analysis_status": "incomplete", "outstanding": ["collector failure"],
                "decision": "INCOMPLETE", "gates": {}, "context": row, "sections": {}}
    index = evidence_index(draft.get("evidence", []), row, now)
    sections = draft.get("sections", {})
    require(isinstance(sections, dict) and set(sections) <= set(SECTIONS), "invalid sections")
    outstanding = [f"section:{key}" for key in SECTIONS if not isinstance(sections.get(key), str) or not sections[key].strip()]
    raw_gates = draft.get("gates", {})
    require(isinstance(raw_gates, dict) and set(raw_gates) <= set(GATES), "unknown NFL gate")
    gates = {}
    refs = {"scan." + key for key in ("away_form", "home_form", "away_qb", "home_qb",
            "away_injury_evidence", "home_injury_evidence", "weather", "venue", "away_rest_days", "home_rest_days") if key in row}
    for name in GATES:
        gate = raw_gates.get(name, {"status": "unknown", "reason": "Not assessed", "evidence_refs": []})
        require(isinstance(gate, dict) and set(gate) <= {"status", "reason", "evidence_refs", "reason_code"}, "invalid gate")
        require(gate.get("status") in ("pass", "fail", "unknown"), "invalid gate status")
        text(gate.get("reason"), "gate reason")
        links = gate.get("evidence_refs")
        require(isinstance(links, list) and all(isinstance(ref, str) and ref in refs | index.keys() for ref in links), "unknown evidence reference")
        if gate["status"] != "unknown":
            require(bool(links), "completed gate needs evidence")
        else:
            outstanding.append(name)
        gates[name] = dict(gate)

    def linked(name, kind, team=None, available=True):
        return [index[ref] for ref in gates[name]["evidence_refs"] if ref in index
                and index[ref]["kind"] == kind and (team is None or team in index[ref]["team_ids"])
                and (not available or index[ref]["status"] == "available")]

    def positive_requires(name, condition, message):
        if gates[name]["status"] == "pass":
            require(condition, message)

    side = draft.get("side")
    require(side in (None, "away", "home"), "side must be away or home")
    if side is None:
        outstanding.append("side")
    selected_id = row.get(str(side) + "_id")
    qb_entries = linked("qb_status_gate", "qb_confirmation", selected_id) if side else []
    confirmed = []
    for entry in qb_entries:
        data = entry["data"]
        if (entry.get("source_type") in ("official_team", "official_league")
                and data.get("confirmed_start") is True and data.get("position") == "QB"
                and data.get("status_resolved") is True and entry["team_ids"] == [selected_id]):
            athlete_id = text(data.get("athlete_id"), "confirmed QB identity")
            roster = row.get(side + "_qb", {}).get("roster", {})
            if roster.get("status") == "retrieved" and any(p.get("athlete_id") == athlete_id and p.get("position") == "QB"
                   and p.get("team_id") == selected_id for p in roster.get("players", [])):
                confirmed.append(entry)
    positive_requires("qb_status_gate", bool(confirmed), "QB1/Active is not official QB confirmation")
    availability = linked("injury_cluster_gate", "player_availability")
    positive_requires("injury_cluster_gate", all(row.get(s + "_injury_evidence", {}).get("status") == "retrieved"
                      for s in ("away", "home")), "injury collection incomplete")
    positive_requires("injury_cluster_gate", all(any(e.get("source_type") in ("official_team", "official_league")
                and e["team_ids"] == [row[s + "_id"]] and e["data"].get("relevant_status_resolved") is True
                for e in availability) for s in ("away", "home")), "injury gate needs both teams' reviewed availability")
    positive_requires("my_defense_late_game_survival", any(set(e["team_ids"]) == {row["away_id"], row["home_id"]} for e in linked("my_defense_late_game_survival", "efficiency")),
                      "defense gate needs efficiency evidence")
    positive_requires("weather_gate", weather_ready(row, now) and "scan.weather" in gates["weather_gate"]["evidence_refs"],
                      "weather gate lacks retrieved kickoff evidence")
    for s in ("away", "home"):
        form = row.get(s + "_form", {})
        usable_form = (type(form.get("n")) is int and form["n"] > 0
                       and type(form.get("prior_season_games")) is int
                       and 0 <= form["prior_season_games"] <= form["n"])
        positive_requires("week_to_week_overreaction_gate", usable_form, "form evidence incomplete")
        if form.get("prior_season_games", 0):
            number(form.get("weighted_pd"), -10000, 10000, "weighted prior PD")
            require(row["seasontype"] == 2 and 1 <= row["week"] <= 4 and form.get("discounted") is True
                    and form.get("prior_season_weight") == 0.5, "unflagged or invalid prior-season form")
            positive_requires("week_to_week_overreaction_gate", bool(linked("week_to_week_overreaction_gate", "offseason_review", row[s + "_id"])),
                              "prior baseline needs completed offseason review")

    # No evidence is different from an attempted lookup returning unavailable.
    # Unknown acquisition never earns a fully evaluated PASS.
    requirements = {"qb_status_gate": "qb_confirmation", "injury_cluster_gate": "player_availability",
                    "my_defense_late_game_survival": "efficiency", "price_discipline": "sportsbook"}
    for name, kind in requirements.items():
        if not linked(name, kind, available=False):
            outstanding.append("acquire:" + kind)
    if not linked("price_discipline", "exchange", available=False):
        outstanding.append("acquire:exchange")
    if row.get("weather", {}).get("status") in (None, "not_retrieved", "collector_failure"):
        outstanding.append("acquire:weather")

    prices = linked("price_discipline", "sportsbook")
    exchanges = linked("price_discipline", "exchange")
    market = {"price": None, "dk_fair_prob": None, "exchange_ask": None, "net_edge": None,
              "sportsbook_source": None, "exchange_source": None}
    if prices:
        require(len(prices) == 1 and set(prices[0]["team_ids"]) == {row["away_id"], row["home_id"]}, "ambiguous sportsbook evidence")
        data = prices[0]["data"]
        probs = fair(data.get("away_price"), data.get("home_price"))
        if side:
            market.update(price=data[side + "_price"], dk_fair_prob=probs[side],
                          sportsbook_source=prices[0]["source"], price_observed_at=prices[0]["observed_at"])
    if exchanges:
        require(len(exchanges) == 1, "ambiguous exchange evidence")
        e = exchanges[0]
        data = e["data"]
        require(data.get("market_type") == "moneyline" and data.get("side") == side
                and data.get("team_id") == selected_id and set(e["team_ids"]) == {row["away_id"], row["home_id"]}, "exchange contract mismatch")
        text(data.get("contract_id"), "exchange contract")
        market.update(exchange_ask=number(data.get("ask"), 0.000001, 0.999999, "exchange ask"), exchange_source=e["source"])
    prob = draft.get("win_probability")
    if prob is not None:
        number(prob, 0.000001, 0.999999, "win probability")
    if prob is not None and market["exchange_ask"] is not None:
        market["net_edge"] = prob - market["exchange_ask"]
    positive_requires("price_discipline", market["price"] is not None and prob is not None
                      and market["net_edge"] is not None and market["net_edge"] >= 0.02 - 1e-12,
                      "price gate needs refreshed book, matched exchange, own probability and >=2% edge")
    if prob is not None and market["dk_fair_prob"] is not None and abs(prob - market["dk_fair_prob"]) > 0.04 + 1e-12:
        text(draft.get("market_explanation"), "explanation for >4% disagreement with market")
    failed = [name for name in GATES if gates[name]["status"] == "fail"]
    decision = "INCOMPLETE" if outstanding else "PASS" if failed or row["seasontype"] == 1 else "CANDIDATE"
    watch_code = gates[failed[0]].get("reason_code") if len(failed) == 1 else None
    if not outstanding and row["seasontype"] != 1 and (
            failed == ["qb_status_gate"] and watch_code == "qb_status_unconfirmed"
            or failed == ["injury_cluster_gate"] and watch_code == "inactives_unconfirmed"):
        blocked_gate = failed[0]
        kind = "qb_confirmation" if blocked_gate == "qb_status_gate" else "player_availability"
        require(any(e["status"] == "unavailable" and e.get("source_type") in ("official_team", "official_league")
                    for e in linked(blocked_gate, kind, available=False)),
                "watchlist needs attempted official confirmation/inactives evidence")
        decision = "WATCHLIST"
    if decision in ("CANDIDATE", "WATCHLIST"):
        require(side is not None, "candidate side missing")
        require(draft.get("confidence") in ("Medium", "High"), "invalid confidence")
        require(not (row["week"] == 1 and draft["confidence"] == "High"), "Week 1 confidence capped at Medium")
        number(draft.get("unit_size"), 0.000001, 100, "unit size")
        text(draft.get("thesis"), "candidate thesis")
    return {"event_id": row["event_id"], "analysis_status": "incomplete" if outstanding else "complete",
            "decision": decision, "outstanding": sorted(set(outstanding)), "failed_gates": failed,
            "gates": gates, "context": row, "sections": sections, "side": side,
            "market": market, "draft": draft, "watch_code": watch_code}


def compose(scan, draft, now):
    require(isinstance(draft, dict) and set(draft) == {"version", "run_id", "date", "assessments"}, "invalid draft envelope")
    require(type(draft["version"]) is int and draft["version"] == VERSION, "unsupported draft version")
    require(isinstance(draft["run_id"], str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", draft["run_id"]), "invalid run id")
    date = draft["date"]
    require(isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), "invalid Central date")
    require(dt.date.fromisoformat(date) == now.astimezone(ZoneInfo("America/Chicago")).date(), "draft date not current Central day")
    rows = contexts(scan, now)
    require(isinstance(draft["assessments"], list), "assessments must be a list")
    assessments = {}
    for game in draft["assessments"]:
        require(isinstance(game, dict), "invalid assessment")
        event = game.get("event_id")
        require(event in rows and event not in assessments, "unknown/duplicate assessed event")
        assessments[event] = game
    games = []
    for event, row in rows.items():
        if event not in assessments:
            games.append({"event_id": event, "analysis_status": "incomplete", "decision": "INCOMPLETE",
                          "outstanding": ["missing assessment"], "gates": {}, "sections": {}, "context": row})
        else:
            games.append(assess(row, assessments[event], now))
    complete = all(g["analysis_status"] == "complete" for g in games)
    schedule = {"version": VERSION, "run_id": draft["run_id"], "date": date, "sport": "NFL",
                "market_type": "moneyline", "analysis_status": "complete" if complete else "incomplete",
                "candidates": [], "inactives_watchlist": [],
                "game_assessments": [{k: v for k, v in g.items() if k not in ("draft", "context")} for g in games]}
    if complete:
        for game in games:
            if game["decision"] not in ("CANDIDATE", "WATCHLIST"):
                continue
            row, item, market = game["context"], game["draft"], game["market"]
            candidate = {"event_id": row["event_id"], "game": row.get("event"), "side": row[game["side"]],
                         "team_id": row[game["side"] + "_id"], "price": market["price"],
                         "confidence": item["confidence"], "unit_size": item["unit_size"],
                         "win_probability": item["win_probability"], "dk_fair_prob": market["dk_fair_prob"],
                         "net_edge": market["net_edge"], "exchange_ask": market["exchange_ask"],
                         "kickoff_utc": row["time"], "kickoff_ct": stamp(row["time"]).astimezone(ZoneInfo("America/Chicago")).isoformat(),
                         "thesis": item["thesis"], "execution_mode": "manual", "status": "awaiting_jerry",
                         "manual_bet_status": None, "executed": False, "vig_review_needed": True,
                         "vig_approved": None, "vig_notes": None}
            if game["decision"] == "CANDIDATE":
                schedule["candidates"].append(candidate)
            else:
                schedule["inactives_watchlist"].append({
                    "event_id": row["event_id"], "side": candidate["side"], "thesis": item["thesis"],
                    "blocked_only_by": [game["watch_code"]], "kickoff_utc": row["time"],
                    "recheck_due_utc": (stamp(row["time"]) - dt.timedelta(minutes=75)).isoformat(),
                    "original_gate_results": game["gates"], "original_price": candidate["price"],
                    "bettable_to_price": candidate["price"], "status": "pending_inactives_recheck"})
    require(len(schedule["candidates"]) <= 3, "more than three validated candidates: analyst must narrow the card")
    lines = [f"# NFL slate — {date}", "", "Analysis: " + schedule["analysis_status"].upper(), ""]
    lines.append(f"{len(schedule['candidates'])} manual proposed candidates; {len(schedule['inactives_watchlist'])} inactives watchlist entries."
                 if complete else "INCOMPLETE — no completed card; candidates withheld pending all game assessments.")
    for game in games:
        row = game["context"]
        lines += ["", f"### {row.get('event', game['event_id'])}", f"Event: {game['event_id']}; kickoff: {row.get('time')}",
                  "Decision: " + (game["decision"] if complete else "INCOMPLETE CARD — " + game["decision"])]
        for side in ("away", "home"):
            qb = row.get(side + "_qb", {}).get("depth_chart", {}).get("quarterbacks", [])
            if qb:
                lines.append(f"{side} depth-chart QB1: {qb[0].get('name')} (not game-day confirmation).")
            form = row.get(side + "_form", {})
            if form.get("prior_season_games"):
                lines.append(f"{side} prior baseline: weight {form.get('prior_season_weight')}; weighted PD {form.get('weighted_pd')}; discounted.")
        lines.append("Weather collection: " + str(row.get("weather", {}).get("status", "not_retrieved")))
        weather = row.get("weather", {})
        if weather.get("status") == "retrieved":
            first = weather.get("samples", [{}])[0]
            lines.append(f"Kickoff-hour forecast: {first.get('temperature_2m')} F; wind {first.get('wind_speed_10m')} mph; "
                         f"precipitation {first.get('precipitation')} in; snow {first.get('snowfall')} in; "
                         f"retrieved {weather.get('retrieved_at')}; source {weather.get('source', 'see scan evidence')}.")
        for name in SECTIONS:
            lines += ["", name.replace("_", " ").title() + ": " + game.get("sections", {}).get(name, "Not assessed")]
        if game.get("market"):
            m = game["market"]
            lines += [f"Refreshed book price: {m['price']}; fair: {m['dk_fair_prob']}; exchange ask: {m['exchange_ask']}; net edge: {m['net_edge']}"]
            if m["sportsbook_source"]:
                lines += [f"Price source: {m['sportsbook_source']}; observed: {m['price_observed_at']}"]
        lines += [f"{key}: {value['status']} — {value['reason']}" for key, value in game["gates"].items()]
        if game["outstanding"]:
            lines.append("Outstanding: " + ", ".join(game["outstanding"]))
    return schedule, "\n".join(lines) + "\n"


def bundle_path(root, run_id):
    require(isinstance(run_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id), "invalid run id")
    root = Path(root).resolve(strict=True)
    path = root / ".picks" / "nfl" / "runs" / run_id
    # No following symlinks out of the selected output root.
    require(path.resolve().is_relative_to(root), "bundle escapes output root")
    for parent in (path, *path.parents):
        if parent == root:
            break
        require(not parent.is_symlink(), "symlink in bundle path")
    return path


def write_bundle(root, scan_raw, draft_raw, now):
    scan, draft = load_json(scan_raw), load_json(draft_raw)
    schedule, report = compose(scan, draft, now)
    require(schedule["analysis_status"] == "complete", "analysis incomplete: use --diagnose; no completed card written")
    destination = bundle_path(root, draft["run_id"])
    require(not destination.exists(), "run bundle already exists; verify it or use a new run id")
    files = {"scan.json": scan_raw, "assessment.json": draft_raw,
             "schedule.json": encoded(schedule), "slate.md": report.encode()}
    receipt = {"version": VERSION, "run_id": draft["run_id"], "output_root": str(Path(root).resolve()),
               "validated_at": now.isoformat(), "analysis_status": "complete",
               "event_ids": sorted(row["event_id"] for row in scan),
               "sha256": {name: digest(data) for name, data in files.items()}}
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".nfl-stage-", dir=destination.parent))
    try:
        for name, data in files.items():
            (stage / name).write_bytes(data)
        (stage / "receipt.json").write_bytes(encoded(receipt))
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    verify_bundle(root, draft["run_id"], now)
    return destination


def verify_bundle(root, run_id, now):
    path = bundle_path(root, run_id)
    names = {"scan.json", "assessment.json", "schedule.json", "slate.md", "receipt.json"}
    require(path.is_dir() and {p.name for p in path.iterdir()} == names, "missing or extra bundle artifacts")
    require(all((path / name).is_file() and not (path / name).is_symlink() for name in names), "unsafe bundle artifact")
    receipt = load_json((path / "receipt.json").read_bytes())
    require(receipt.get("version") == VERSION and receipt.get("run_id") == run_id
            and receipt.get("output_root") == str(Path(root).resolve())
            and receipt.get("analysis_status") == "complete", "receipt identity/root mismatch")
    fresh(receipt.get("validated_at"), now, MAX_PRICE_AGE)
    files = {name: (path / name).read_bytes() for name in names - {"receipt.json"}}
    require(receipt.get("sha256") == {name: digest(raw) for name, raw in files.items()}, "bundle bytes changed")
    scan, draft = load_json(files["scan.json"]), load_json(files["assessment.json"])
    require(draft.get("run_id") == run_id and receipt.get("event_ids") == sorted(row["event_id"] for row in scan), "receipt event coverage mismatch")
    schedule, report = compose(scan, draft, now)
    require(schedule["analysis_status"] == "complete" and encoded(schedule) == files["schedule.json"]
            and report.encode() == files["slate.md"], "report/schedule differ from validated assessment")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="One canonical existing output root")
    parser.add_argument("--scan", type=Path)
    parser.add_argument("--draft", type=Path)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--skeleton", action="store_true")
    modes.add_argument("--diagnose", action="store_true")
    modes.add_argument("--write", action="store_true")
    modes.add_argument("--verify", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--date")
    args = parser.parse_args()
    now = dt.datetime.now(UTC)
    try:
        if args.verify:
            print(json.dumps(verify_bundle(args.root, args.run_id, now)))
        else:
            require(args.scan is not None, "--scan required")
            scan_raw = args.scan.read_bytes()
            if args.skeleton:
                require(args.run_id is not None and args.date is not None, "--run-id and --date required")
                print(json.dumps(skeleton(load_json(scan_raw), args.run_id, args.date, now), indent=2))
            else:
                require(args.draft is not None, "--draft required")
                draft_raw = args.draft.read_bytes()
                if args.write:
                    print(write_bundle(args.root, scan_raw, draft_raw, now))
                else:
                    schedule, report = compose(load_json(scan_raw), load_json(draft_raw), now)
                    print(json.dumps({"schedule": schedule, "report": report}, indent=2))
                    if schedule["analysis_status"] != "complete":
                        return 2
        return 0
    except (Invalid, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"NFL finalizer refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
