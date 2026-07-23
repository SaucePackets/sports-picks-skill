#!/usr/bin/env python3
"""Select and validate lineup-dependent MLB watchlist rechecks.

The morning slate owns creation of ``lineup_watchlist`` entries. This module
provides the deterministic timing and safety checks used by Vig's conditional
review gate; the LLM reviewer still refreshes the live baseball inputs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from http_util import fetch_json as _retrying_fetch_json  # noqa: E402
from mlb_runtime_policy import standing_authorization_enabled  # noqa: E402

MIN_MINUTES_BEFORE_FIRST_PITCH = 60
MAX_MINUTES_BEFORE_FIRST_PITCH = 90
PENDING_STATUS = "pending_lineup_recheck"
TERMINAL_STATUSES = {"promoted", "passed", "filled_manual"}
VALID_STATUSES = {PENDING_STATUS, *TERMINAL_STATUSES}
FORBIDDEN_EXECUTION_FIELDS = {
    "execution_cron_id",
    "execution_cron_fire_utc",
    "approval_token",
}
REQUIRED_ORIGINAL_GATES = {
    "starter_floor",
    "opposing_starter_shutdown_path",
    "bullpen_close_game_survival",
    "cold_fade_reset",
    "price_discipline",
    "real_winner_conviction",
}


class WatchlistFormatError(ValueError):
    """Raised when persisted lineup-watch state is malformed."""


class LineupLookupError(RuntimeError):
    """Raised when a watchlist game cannot be mapped to an MLB game feed."""


def http_json(url: str) -> dict[str, Any]:
    payload = _retrying_fetch_json(
        url, timeout=30, headers={"User-Agent": "HermesSportsPicks/1.0"}
    )
    if not isinstance(payload, dict):
        raise LineupLookupError("MLB data source returned a non-object response")
    return payload


def _normalized_team_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _entry_teams(entry: dict[str, Any]) -> tuple[str, str]:
    game = str(entry.get("game") or "").strip()
    match = re.match(r"^(.+?)\s+(?:at|@|vs\.?|versus)\s+(.+)$", game, re.IGNORECASE)
    if not match:
        raise LineupLookupError("watchlist game must identify away and home teams")
    return match.group(1).strip(), match.group(2).strip()


def _espn_event_teams(summary: dict[str, Any]) -> tuple[str, str]:
    competitions = summary.get("header", {}).get("competitions", [])
    competitors = competitions[0].get("competitors", []) if competitions else []
    names = {
        competitor.get("homeAway"): competitor.get("team", {}).get("displayName")
        for competitor in competitors
        if isinstance(competitor, dict)
    }
    away_team = names.get("away")
    home_team = names.get("home")
    if not away_team or not home_team:
        raise LineupLookupError("ESPN event did not identify away and home teams")
    return str(away_team), str(home_team)


def resolve_game_pk(schedule: dict[str, Any], away_team: str, home_team: str) -> int:
    wanted = (_normalized_team_name(away_team), _normalized_team_name(home_team))
    for date_block in schedule.get("dates", []):
        if not isinstance(date_block, dict):
            continue
        for game in date_block.get("games", []):
            if not isinstance(game, dict):
                continue
            teams = game.get("teams", {})
            actual = (
                _normalized_team_name(teams.get("away", {}).get("team", {}).get("name")),
                _normalized_team_name(teams.get("home", {}).get("team", {}).get("name")),
            )
            game_pk = game.get("gamePk")
            if actual == wanted and isinstance(game_pk, int):
                return game_pk
    raise LineupLookupError(f"no MLB schedule game matched {away_team} at {home_team}")


def _batting_order(feed: dict[str, Any], side: str) -> list[str]:
    team = feed.get("liveData", {}).get("boxscore", {}).get("teams", {}).get(side, {})
    players = feed.get("gameData", {}).get("players", {})
    names: list[str] = []
    for player_id in team.get("battingOrder", []):
        player = players.get(f"ID{player_id}", {})
        name = player.get("fullName") or player.get("person", {}).get("fullName")
        names.append(str(name or f"player {player_id}"))
    return names


def fetch_lineup_snapshot(
    entry: dict[str, Any],
    fetch_json: Callable[[str], dict[str, Any]] = http_json,
) -> dict[str, Any]:
    """Resolve a watchlist game through the MLB schedule before loading its feed."""
    first_pitch = parse_instant(entry.get("first_pitch_utc"))
    if first_pitch is None:
        raise LineupLookupError("watchlist entry has no valid first pitch")
    query = urllib.parse.urlencode({"sportId": 1, "date": first_pitch.date().isoformat()})
    schedule = fetch_json(f"https://statsapi.mlb.com/api/v1/schedule?{query}")
    try:
        away_team, home_team = _entry_teams(entry)
    except LineupLookupError:
        event_id = entry.get("event_id") or entry.get("espn_event_id")
        if not event_id:
            raise
        event_query = urllib.parse.urlencode({"event": str(event_id)})
        summary = fetch_json(
            "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary?"
            + event_query
        )
        away_team, home_team = _espn_event_teams(summary)
    game_pk = resolve_game_pk(schedule, away_team, home_team)
    feed = fetch_json(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live")
    players = feed.get("gameData", {}).get("players", {})
    return {
        "game_pk": game_pk,
        "away_team": away_team,
        "home_team": home_team,
        "player_count": len(players) if isinstance(players, dict) else 0,
        "away_batting_order": _batting_order(feed, "away"),
        "home_batting_order": _batting_order(feed, "home"),
    }


def parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_entry(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        errors.append("id must be a non-empty string")
    if entry.get("blocked_only_by") != ["lineups_unconfirmed"]:
        errors.append("blocked_only_by must contain only lineups_unconfirmed")
    if parse_instant(entry.get("first_pitch_utc")) is None:
        errors.append("first_pitch_utc must be a valid timestamp")
    if parse_instant(entry.get("recheck_due_utc")) is None:
        errors.append("recheck_due_utc must be a valid timestamp")
    if not _is_number(entry.get("original_price")):
        errors.append("original_price must be numeric")
    if not _is_number(entry.get("bettable_to_price")):
        errors.append("bettable_to_price must be numeric")
    status = entry.get("status")
    if status not in VALID_STATUSES:
        errors.append(f"status must be one of {sorted(VALID_STATUSES)}")
    gates = entry.get("original_gate_results")
    if not isinstance(gates, dict):
        errors.append("original_gate_results must be an object")
    else:
        for gate in sorted(REQUIRED_ORIGINAL_GATES):
            if gates.get(gate) is not True:
                errors.append(f"original_gate_results.{gate} must be true")
        if gates.get("lineups_confirmed") is not False:
            errors.append("original_gate_results.lineups_confirmed must be false")

    if status in TERMINAL_STATUSES and parse_instant(entry.get("rechecked_at_utc")) is None:
        errors.append(f"{status} entry requires rechecked_at_utc")
    if status == "passed":
        notes = entry.get("recheck_notes")
        if not isinstance(notes, str) or not notes.strip():
            errors.append("passed entry requires non-empty recheck_notes")
    if status == "promoted":
        recheck = entry.get("recheck")
        required_refreshes = (
            "lineups_confirmed",
            "key_injuries_refreshed",
            "price_refreshed",
            "all_original_gates_hold",
        )
        if not isinstance(recheck, dict):
            errors.append("promoted entry requires a recheck object")
        else:
            for field in required_refreshes:
                if recheck.get(field) is not True:
                    errors.append(f"recheck.{field} must be true")
        candidate = entry.get("promoted_candidate")
        if not isinstance(candidate, dict):
            errors.append("promoted entry requires promoted_candidate")
        else:
            if candidate.get("watchlist_id") != entry_id:
                errors.append("promoted_candidate.watchlist_id must match entry id")
            authorized = standing_authorization_enabled()
            if authorized:
                if candidate.get("sport") != "MLB":
                    errors.append("promoted_candidate.sport must be MLB")
                if candidate.get("market_type") != "moneyline":
                    errors.append("promoted_candidate.market_type must be moneyline")
                if candidate.get("execution_mode") != "standing_authorized":
                    errors.append("promoted_candidate.execution_mode must be standing_authorized")
                if candidate.get("execution_status") != "pending":
                    errors.append("promoted_candidate.execution_status must be pending")
                if candidate.get("manual_bet_status") == "awaiting_jerry":
                    errors.append("promoted_candidate.manual_bet_status must not be awaiting_jerry")
            else:
                if candidate.get("execution_mode") != "manual":
                    errors.append("promoted_candidate.execution_mode must be manual")
                if candidate.get("manual_bet_status") != "awaiting_jerry":
                    errors.append("promoted_candidate.manual_bet_status must be awaiting_jerry")
            if candidate.get("executed") is not False:
                errors.append("promoted_candidate.executed must be false")
            if authorized:
                max_price = candidate.get("max_polymarket_price")
                numeric_max_price = (
                    float(max_price)
                    if isinstance(max_price, (int, float)) and not isinstance(max_price, bool)
                    else None
                )
                if numeric_max_price is None or not 0 < numeric_max_price < 1:
                    errors.append(
                        "promoted_candidate.max_polymarket_price must be between 0 and 1"
                    )
            present = sorted(FORBIDDEN_EXECUTION_FIELDS.intersection(candidate))
            if present:
                errors.append(f"promoted_candidate has forbidden execution fields: {', '.join(present)}")
    return errors


def validate_watchlist(schedule: dict[str, Any]) -> dict[str, list[str]]:
    raw_entries = schedule.get("lineup_watchlist", [])
    if not isinstance(raw_entries, list):
        return {"lineup_watchlist": ["lineup_watchlist must be a list"]}
    errors: dict[str, list[str]] = {}
    seen: set[str] = set()
    for index, entry in enumerate(raw_entries):
        label = str(index)
        if not isinstance(entry, dict):
            errors[label] = ["entry must be an object"]
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str) and entry_id.strip():
            label = entry_id
            if entry_id in seen:
                errors.setdefault(label, []).append("id must be unique")
            seen.add(entry_id)
        entry_errors = validate_entry(entry)
        if entry_errors:
            errors.setdefault(label, []).extend(entry_errors)
    return errors


def require_valid_watchlist(schedule: dict[str, Any]) -> None:
    errors = validate_watchlist(schedule)
    if errors:
        rendered = "; ".join(f"{key}: {', '.join(value)}" for key, value in errors.items())
        raise WatchlistFormatError(rendered)


def due_entries(schedule: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    require_valid_watchlist(schedule)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_entries = schedule.get("lineup_watchlist", [])
    due: list[dict[str, Any]] = []
    for entry in raw_entries:
        if entry.get("status") != PENDING_STATUS:
            continue
        first_pitch = parse_instant(entry.get("first_pitch_utc"))
        if first_pitch is None:
            continue
        minutes = (first_pitch - current).total_seconds() / 60
        if MIN_MINUTES_BEFORE_FIRST_PITCH <= minutes <= MAX_MINUTES_BEFORE_FIRST_PITCH:
            due.append(entry)
    return due


def _lineup_context(
    entries: list[dict[str, Any]], snapshots: dict[str, dict[str, Any]] | None
) -> str:
    if not snapshots:
        return ""
    sections: list[str] = []
    for entry in entries:
        snapshot = snapshots.get(str(entry.get("id")))
        if not snapshot:
            continue
        away = snapshot.get("away_batting_order", [])
        home = snapshot.get("home_batting_order", [])
        sections.append(
            "\n".join(
                [
                    f"MLB gamePk {snapshot.get('game_pk')} — {snapshot.get('player_count', 0)} roster players",
                    f"{snapshot.get('away_team')} batting order ({len(away)}): {', '.join(away)}",
                    f"{snapshot.get('home_team')} batting order ({len(home)}): {', '.join(home)}",
                ]
            )
        )
    if not sections:
        return ""
    return "\n\nResolved MLB lineup data (schedule-mapped; do not use ESPN event IDs as gamePk):\n" + "\n\n".join(sections)


def build_recheck_prompt(
    schedule_path: Path,
    entries: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]] | None = None,
) -> str:
    entry_ids = ", ".join(str(entry.get("id", "<missing-id>")) for entry in entries)
    if standing_authorization_enabled():
        routing = """A promotion must be copied into candidates with
watchlist_id equal to the source watchlist entry id,
execution_mode=standing_authorized, execution_status=pending, executed=false,
sport=MLB, market_type=moneyline, an explicit max_polymarket_price between 0 and 1,
vig_review_needed=false, vig_approved=true, and no execution cron fields.
The recurring MLB execution poller will refresh all gates and handle execution."""
    else:
        routing = """A promotion must remain manual-only with execution_mode=manual,
manual_bet_status=awaiting_jerry, executed=false, vig_review_needed=false, and
vig_approved=true. It must never place or schedule a bet."""
    lineup_context = _lineup_context(entries, snapshots)
    return f"""You are Vig performing the MLB lineup watchlist recheck.
Read and update {schedule_path}. Recheck only these watchlist IDs: {entry_ids}.
{lineup_context}

For each entry, refresh from live sources:
- confirmed batting lineups for both teams;
- key injury status and late scratches;
- current supported-market price and the stored bettable-to threshold.

Re-run every original gate using the refreshed facts. Promote only when lineups
are confirmed, injury and price refreshes succeeded, and every original gate
still holds. {routing}

If any refresh is unavailable, the price is too expensive, a lineup/injury
change weakens the thesis, or any original gate fails, set the watchlist entry
status to passed and write a concise recheck_notes reason. For a promotion, set
status=promoted and record recheck.lineups_confirmed,
recheck.key_injuries_refreshed, recheck.price_refreshed, and
recheck.all_original_gates_hold as true, plus the promoted_candidate. Always set
rechecked_at_utc. Do not execute here, create an approval token, call a trading
endpoint, or create a cron job; route through the recurring MLB execution poller.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect MLB lineup-dependent watchlist entries.")
    parser.add_argument("schedule", type=Path)
    parser.add_argument("--now", help="UTC/offset timestamp override")
    parser.add_argument("--validate", action="store_true", help="validate all watchlist entries")
    args = parser.parse_args(argv)

    try:
        schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if not isinstance(schedule, dict):
        parser.error("schedule must be a JSON object")

    if args.validate:
        errors = validate_watchlist(schedule)
        print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
        return 1 if errors else 0

    now = parse_instant(args.now) if args.now else None
    if args.now and now is None:
        parser.error("--now must be a valid timestamp")
    due = due_entries(schedule, now)
    print(json.dumps({"due": due}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
