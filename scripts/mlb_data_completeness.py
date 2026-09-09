"""Source-data validation for MLB scans; no selection policy or execution.

A complete published pregame StatsAPI batting order corroborated by the nine
player records is our explicit confirmation contract, not a vendor boolean.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

try:
    from .numeric_util import is_finite_number
except ImportError:
    from numeric_util import is_finite_number

SCHEMA = "mlb-data-completeness-v1"
MAX_LINEUP_AGE_SECONDS = 1800
CANONICAL_TEAMS = frozenset("ARI ATL BAL BOS CHC CWS CIN CLE COL DET HOU KC LAA LAD MIA MIL MIN NYM NYY ATH PHI PIT SD SF SEA STL TB TEX TOR WSN".split())
ALIASES = {"AZ": "ARI", "WSH": "WSN", "CHW": "CWS", "OAK": "ATH", "KCR": "KC", "SDP": "SD", "SFG": "SF", "TBR": "TB"}


def canonical_team(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("missing team abbreviation")
    value = value.strip().upper()
    value = ALIASES.get(value, value)
    if value not in CANONICAL_TEAMS:
        raise ValueError(f"unknown MLB team abbreviation: {value!r}")
    return value


def canonical_offense(rows: dict) -> dict:
    """Normalize both cached and fetched source keys; conflicting aliases fail."""
    result = {}
    for alias, stats in rows.items():
        team = canonical_team(alias)
        if team in result and result[team] != stats:
            raise ValueError(f"conflicting offense aliases for {team}")
        result[team] = stats
    return result


def timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp needs timezone")
    return parsed


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def lineup_from_feed(feed: dict, game: dict, side: str, retrieved_at: str) -> dict:
    team = game["teams"][side]["team"]
    pk = game["gamePk"]
    record = {"source_url": f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live",
              "retrieved_at_utc": retrieved_at, "game_pk": pk,
              "team_id": team["id"], "team_abbr": canonical_team(team["abbreviation"]),
              "state": "unconfirmed", "players": [],
              "confirmation_method": "published_pregame_order_with_player_slots"}
    try:
        data = feed["gameData"]
        box = feed["liveData"]["boxscore"]["teams"][side]
        if (feed["gamePk"] != pk or data["teams"][side]["id"] != team["id"]
                or box["team"]["id"] != team["id"]
                or canonical_team(data["teams"][side]["abbreviation"]) != record["team_abbr"]):
            raise ValueError("lineup game/team identity mismatch")
        start = timestamp(data["datetime"]["dateTime"])
        if start != timestamp(game["gameDate"]):
            raise ValueError("lineup start time mismatch")
        if (data["status"]["abstractGameState"] != "Preview"
                or data["status"]["detailedState"] not in ("Scheduled", "Pre-Game")
                or timestamp(retrieved_at) >= start):
            raise ValueError("not a pregame lineup observation")
        order = box.get("battingOrder")
        if not isinstance(order, list) or len(order) != 9 or any(type(p) is not int or p <= 0 for p in order) or len(set(order)) != 9:
            raise ValueError("complete nine-player order not published")
        players = []
        for slot, player_id in enumerate(order, 1):
            player = box["players"][f"ID{player_id}"]
            if (player["person"]["id"] != player_id
                    or player.get("parentTeamId") != team["id"]
                    or player.get("battingOrder") != str(slot * 100)
                    or not isinstance(player["person"].get("fullName"), str)
                    or not player["person"]["fullName"].strip()
                    or player.get("gameStatus", {}).get("isSubstitute") is not False):
                raise ValueError("player identity/starting slot not corroborated")
            players.append({"id": player_id, "name": player["person"]["fullName"], "slot": slot})
        record.update(state="confirmed", players=players)
    except (KeyError, TypeError, ValueError) as exc:
        record["reason"] = str(exc)
    return record


def lineup_errors(record: Any, row: dict, side: str, now: dt.datetime) -> list[str]:
    if not isinstance(record, dict):
        return ["missing_lineup"]
    errors = []
    if record.get("state") != "confirmed":
        errors.append("unconfirmed_lineup")
    try:
        pk = row["game_pk"]
        if (record.get("source_url") != f"https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"
                or record.get("game_pk") != pk
                or record.get("team_id") != row[f"{side}_team_id"]
                or record.get("team_id") is None
                or canonical_team(record.get("team_abbr")) != canonical_team(row[f"{side}_abbr"])):
            errors.append("lineup_identity_mismatch")
        retrieved = timestamp(record.get("retrieved_at_utc"))
        age = (now - retrieved).total_seconds()
        if age < 0 or age > MAX_LINEUP_AGE_SECONDS:
            errors.append("stale_lineup")
        if retrieved >= timestamp(row.get("time")) or now >= timestamp(row.get("time")):
            errors.append("not_pregame")
    except (KeyError, TypeError, ValueError):
        errors.append("invalid_lineup_metadata")
    players = record.get("players")
    if (not isinstance(players, list) or len(players) != 9
            or any(not isinstance(p, dict) or type(p.get("id")) is not int or p["id"] <= 0
                   or p.get("slot") != slot or not isinstance(p.get("name"), str) or not p["name"].strip()
                   for slot, p in enumerate(players, 1))
            or len({p["id"] for p in players}) != 9):
        errors.append("invalid_lineup_players")
    return errors


def assess(row: dict, now: dt.datetime | None = None) -> dict:
    """Recompute source readiness, never trust a recorded summary or create pass."""
    now = now or utc_now()
    missing = []
    lineups = {}
    for side in ("away", "home"):
        for field, keys in (("offense", ("woba", "xwoba")), ("starter_stats", ("era", "whip", "fip", "k_bb_pct")), ("bullpen", ("era", "whip", "ip")), ("form", ("n", "w", "l", "rf", "ra"))):
            name = f"{side}_{field}"
            value = row.get(name)
            if not isinstance(value, dict) or any(not is_finite_number(value.get(k)) for k in keys):
                missing.append(name)
            elif field in ("form", "bullpen") and value["n" if field == "form" else "ip"] <= 0:
                missing.append(name)
        if not row.get(f"{side}_starter"):
            missing.append(f"{side}_starter")
        if not isinstance(row.get(f"{side}_injuries"), list):
            missing.append(f"{side}_injuries")
        try:
            canonical_team(row.get(f"{side}_abbr"))
        except ValueError:
            missing.append(f"{side}_identity")
        lineups[side] = lineup_errors(row.get(f"{side}_lineup"), row, side, now)
        if lineups[side]:
            missing.append(f"{side}_lineup")
    if row.get("error"):
        missing.append("game_collection")
    prices = all(is_finite_number(row.get(f"{s}_fair")) and 0 < row[f"{s}_fair"] < 1 for s in ("away", "home"))
    status = "incomplete_input_data" if missing else ("not_priced" if not prices else "ready_for_evaluation")
    return {"schema": SCHEMA, "status": status, "missing_fields": missing,
            "prices_available": prices, "lineup_errors": lineups,
            "source_failures": row.get("source_failures", [])}


def coverage(rows: list, now: dt.datetime | None = None, *, scheduled_games: int | None = None,
             schedule_verified: bool = False) -> dict:
    now = now or utc_now()
    assessments = [assess(r if isinstance(r, dict) else {"error": "invalid row"}, now) for r in rows]
    counts = Counter(a["status"] for a in assessments)
    missing = Counter(f for a in assessments for f in a["missing_fields"])
    ids = [r.get("game_pk") for r in rows if isinstance(r, dict)]
    valid_ids = [i for i in ids if type(i) is int and i > 0]
    return {"schema": SCHEMA, "evaluated_at_utc": now.isoformat(), "scheduled_games": scheduled_games,
            "schedule_verified": schedule_verified, "rows": len(rows),
            "unique_mlb_games": len(set(valid_ids)),
            "reconciled": schedule_verified and scheduled_games == len(rows) == len(set(valid_ids)),
            "complete_reads": sum(not a["missing_fields"] for a in assessments),
            "status_counts": {s: counts[s] for s in ("incomplete_input_data", "not_priced", "ready_for_evaluation")},
            "missing_fields": dict(missing),
            "unpriced_games": sum(not a["prices_available"] for a in assessments),
            "stale_lineup_games": sum(any("stale_lineup" in e for e in a["lineup_errors"].values()) for a in assessments),
            "source_failure_games": sum(bool(a["source_failures"]) for a in assessments)}


def read_disposition_errors(reads: list, rows: list, now: dt.datetime | None = None) -> list[str]:
    """Corroborate recording against versioned acquisition rows, at landing time.

    Historical unversioned scans remain readable; their completeness is unknown.
    This only rejects a mislabeled record and never changes its betting decision.
    """
    errors = []
    sources = {r.get("game_pk"): r for r in rows if isinstance(r, dict)}
    for read in reads:
        if not isinstance(read, dict):
            continue
        row = sources.get(read.get("game_pk"))
        if not row or "data_completeness" not in row:
            continue
        if not isinstance(row["data_completeness"], dict) or row["data_completeness"].get("schema") != SCHEMA:
            errors.append(f"game {read.get('game_pk')}: unsupported data completeness schema")
            continue
        if any(row.get(k) != read.get(k) for k in ("event_id", "away", "home")):
            errors.append(f"game {read.get('game_pk')}: source/read identity mismatch")
            continue
        status = assess(row, now)
        disposition = read.get("disposition")
        if disposition in ("pass", "candidate") and status["status"] != "ready_for_evaluation":
            errors.append(f"game {read.get('game_pk')}: {disposition} cannot describe {status['status']}")
        if disposition == "lineup_watchlist" and (
                not status["prices_available"] or set(status["missing_fields"]) - {"away_lineup", "home_lineup"}):
            errors.append(f"game {read.get('game_pk')}: lineup watchlist has missing non-lineup inputs")
    return errors


def coverage_for_scan(path, rows: list, now: dt.datetime | None = None, *, scan_bytes: bytes | None = None) -> dict:
    """Use the collector's schedule count only when bound to these exact bytes."""
    import hashlib
    import json
    receipt = {}
    try:
        receipt = json.loads(path.with_suffix(".coverage.json").read_text())
        if receipt.get("schema") != SCHEMA or receipt.get("scan_sha256") != hashlib.sha256(scan_bytes if scan_bytes is not None else path.read_bytes()).hexdigest():
            receipt = {}
    except (OSError, ValueError, AttributeError):
        receipt = {}
    count = receipt.get("scheduled_games")
    verified = receipt.get("schedule_verified") is True and type(count) is int and count >= 0
    return coverage(rows, now, scheduled_games=count if verified else None, schedule_verified=verified)
