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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from http_util import fetch_json as _retrying_fetch_json  # noqa: E402
from mlb_runtime_policy import (  # noqa: E402
    MlbSelectionPolicy,
    load_mlb_selection_policy,
    standing_authorization_enabled,
)
from mlb_baseball_evidence import review_prompt_evidence_section  # noqa: E402

MIN_MINUTES_BEFORE_FIRST_PITCH = 35
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

# A watchlist entry defers to a pre-pitch recheck. The morning slate scans at
# 10:30am CT, before that day's later probable-pitcher announcements and before
# batting orders post — so a real price edge can be blocked purely by inputs that
# are not yet PUBLISHED (not inputs that are known-bad). Only those two
# not-yet-published blockers are deferrable; any other blocker (a known-bad
# starter, an efficient price, a thesis-breaking injury, a park cap) is a normal
# pass, never a watchlist entry.
LINEUP_BLOCKER = "lineups_unconfirmed"
STARTER_BLOCKER = "starter_unannounced"
ALLOWED_BLOCKERS = (LINEUP_BLOCKER, STARTER_BLOCKER)

# When the opposing starter is not yet announced at slate time, the two gates that
# require handicapping that starter cannot be evaluated. They are left null in the
# entry and RE-DERIVED at recheck with the real announced starter; the morning
# win_probability / net_edge / price ceiling are provisional and are recomputed
# then. Every other gate must already hold at slate time.
STARTER_DEFERRED_GATES = {
    "opposing_starter_shutdown_path",
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


def resolve_game_pk(
    schedule: dict[str, Any],
    away_team: str,
    home_team: str,
    first_pitch: datetime | None = None,
) -> int:
    """Map team names to a gamePk; doubleheaders resolve by nearest gameDate."""
    wanted = (_normalized_team_name(away_team), _normalized_team_name(home_team))
    matches: list[tuple[int, datetime | None]] = []
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
            if actual == wanted and isinstance(game_pk, int) and not isinstance(game_pk, bool):
                matches.append((game_pk, parse_instant(game.get("gameDate"))))
    if not matches:
        raise LineupLookupError(f"no MLB schedule game matched {away_team} at {home_team}")
    if len(matches) == 1 or first_pitch is None:
        return matches[0][0]

    def seconds_from_first_pitch(match: tuple[int, datetime | None]) -> float:
        game_date = match[1]
        if game_date is None:
            return float("inf")
        return abs((game_date - first_pitch).total_seconds())

    return min(matches, key=seconds_from_first_pitch)[0]


def _batting_order(feed: dict[str, Any], side: str) -> list[str]:
    team = feed.get("liveData", {}).get("boxscore", {}).get("teams", {}).get(side, {})
    players = feed.get("gameData", {}).get("players", {})
    names: list[str] = []
    for player_id in team.get("battingOrder", []):
        player = players.get(f"ID{player_id}", {})
        name = player.get("fullName") or player.get("person", {}).get("fullName")
        names.append(str(name or f"player {player_id}"))
    return names


def _probable_name(entry: Any) -> str:
    """Announced probable pitcher name from an MLB feed probablePitchers side.

    Returns "" when the starter has not been posted yet — the deterministic
    signal the recheck uses to tell "not announced" from a real announced arm.
    """
    if not isinstance(entry, dict):
        return ""
    name = entry.get("fullName") or entry.get("person", {}).get("fullName")
    return str(name).strip() if name else ""


def _stamped_game_pk(entry: dict[str, Any]) -> int | None:
    game_pk = entry.get("game_pk")
    if isinstance(game_pk, int) and not isinstance(game_pk, bool) and game_pk > 0:
        return game_pk
    return None


def fetch_lineup_snapshot(
    entry: dict[str, Any],
    fetch_json: Callable[[str], dict[str, Any]] = http_json,
) -> dict[str, Any]:
    """Load a watchlist game's live feed.

    A stamped ``game_pk`` on the entry is used directly (doubleheader-proof).
    Otherwise the game resolves through the MLB schedule; when several
    schedule games match the team names, the one whose gameDate is nearest
    the entry's first pitch wins.
    """
    first_pitch = parse_instant(entry.get("first_pitch_utc"))
    if first_pitch is None:
        raise LineupLookupError("watchlist entry has no valid first pitch")
    away_team: str | None = None
    home_team: str | None = None
    game_pk = _stamped_game_pk(entry)
    if game_pk is None:
        # MLB keys games by ballpark-LOCAL officialDate. A late West-Coast first
        # pitch lands on the NEXT UTC calendar day, so a single UTC-date query can
        # miss the game entirely — and, when the same two teams also play the next
        # day, silently resolve to the WRONG game (whose lineup is not yet posted),
        # producing a false "0 of 9 confirmed" pass. Query a +/-1 day window and let
        # resolve_game_pk pick the game nearest the entry's first pitch.
        window_start = (first_pitch - timedelta(days=1)).date().isoformat()
        window_end = (first_pitch + timedelta(days=1)).date().isoformat()
        query = urllib.parse.urlencode(
            {"sportId": 1, "startDate": window_start, "endDate": window_end}
        )
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
        game_pk = resolve_game_pk(schedule, away_team, home_team, first_pitch=first_pitch)
    feed = fetch_json(f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live")
    if away_team is None or home_team is None:
        feed_teams = feed.get("gameData", {}).get("teams", {})
        away_team = str(feed_teams.get("away", {}).get("name") or "")
        home_team = str(feed_teams.get("home", {}).get("name") or "")
        if not away_team or not home_team:
            try:
                away_team, home_team = _entry_teams(entry)
            except LineupLookupError:
                away_team = away_team or "unknown away team"
                home_team = home_team or "unknown home team"
    players = feed.get("gameData", {}).get("players", {})
    probables = feed.get("gameData", {}).get("probablePitchers", {})
    if not isinstance(probables, dict):
        probables = {}
    return {
        "game_pk": game_pk,
        "away_team": away_team,
        "home_team": home_team,
        "player_count": len(players) if isinstance(players, dict) else 0,
        "away_batting_order": _batting_order(feed, "away"),
        "home_batting_order": _batting_order(feed, "home"),
        "away_probable_pitcher": _probable_name(probables.get("away")),
        "home_probable_pitcher": _probable_name(probables.get("home")),
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


def entry_is_manual_only(entry: dict[str, Any]) -> bool:
    """True when a pick must NEVER auto-execute — it routes to Jerry for manual
    confirmation regardless of the global standing-authorization toggle.

    The control is an explicit ``manual_only: true`` flag, never prose. Following
    mlb_runtime_policy's flag-file philosophy, thesis wording is deliberately NOT
    consulted: "manual-only" appears as boilerplate in slate theses, so matching it
    would over-match — retroactively invalidating already-executed standing-authorized
    bets and failing as a real signal. A slate that means manual-only sets the flag.
    """
    return entry.get("manual_only") is True


def validate_entry(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        errors.append("id must be a non-empty string")
    blockers = entry.get("blocked_only_by")
    blocker_set: set[str] = set()
    if (
        not isinstance(blockers, list)
        or not blockers
        or any(b not in ALLOWED_BLOCKERS for b in blockers)
        or len(set(blockers)) != len(blockers)
    ):
        errors.append(
            "blocked_only_by must be a non-empty list drawn only from "
            f"{list(ALLOWED_BLOCKERS)} with no duplicates"
        )
    else:
        blocker_set = set(blockers)
    starter_pending = STARTER_BLOCKER in blocker_set
    lineup_pending = LINEUP_BLOCKER in blocker_set
    if parse_instant(entry.get("first_pitch_utc")) is None:
        errors.append("first_pitch_utc must be a valid timestamp")
    if parse_instant(entry.get("recheck_due_utc")) is None:
        errors.append("recheck_due_utc must be a valid timestamp")
    game_pk = entry.get("game_pk")
    if game_pk is not None and _stamped_game_pk(entry) is None:
        errors.append("game_pk must be a positive integer when present")
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
        # When the opposing starter is not yet announced, the two gates that require
        # handicapping it are deferred (left null) and re-derived at recheck; every
        # other gate must already hold. Otherwise all six must be true, as before.
        deferred = STARTER_DEFERRED_GATES if starter_pending else set()
        for gate in sorted(REQUIRED_ORIGINAL_GATES - deferred):
            if gates.get(gate) is not True:
                errors.append(f"original_gate_results.{gate} must be true")
        for gate in sorted(deferred):
            if gates.get(gate) is True:
                errors.append(
                    f"original_gate_results.{gate} must be null while "
                    "starter_unannounced (it is re-derived at recheck)"
                )
        # A lineup-blocked entry has not seen confirmed orders yet; a starter-only
        # entry may already have confirmed lineups, so only constrain when relevant.
        if lineup_pending and gates.get("lineups_confirmed") is not False:
            errors.append("original_gate_results.lineups_confirmed must be false")

    if status in TERMINAL_STATUSES and parse_instant(entry.get("rechecked_at_utc")) is None:
        errors.append(f"{status} entry requires rechecked_at_utc")
    if status == "passed":
        notes = entry.get("recheck_notes")
        if not isinstance(notes, str) or not notes.strip():
            errors.append("passed entry requires non-empty recheck_notes")
    if status == "promoted":
        # Starter-pending promotions are disabled during the hardening rollout:
        # an entry blocked by starter_unannounced may never promote while the
        # shared policy switch is off (the deterministic default).
        if starter_pending:
            policy = load_mlb_selection_policy()
            if policy is None or not policy.starter_pending_promotions_enabled:
                errors.append(
                    "starter_unannounced entries cannot be promoted: "
                    "starter_pending_promotions_enabled is false in the shared "
                    "MLB selection policy"
                )
        recheck = entry.get("recheck")
        required_refreshes = (
            "lineups_confirmed",
            "key_injuries_refreshed",
            "price_refreshed",
            "all_original_gates_hold",
        )
        # A starter-pending entry may only promote after the real announced starter
        # was handicapped and the net edge recomputed against the live ask — the
        # morning number was provisional. These flags are the audit that it happened.
        if starter_pending:
            required_refreshes += ("starter_confirmed", "net_edge_recomputed")
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
            manual_only = entry_is_manual_only(entry)
            # A manual-only pick is never auto-authorized, even when the global
            # standing-authorization flag is on — it must route to manual/awaiting_jerry.
            authorized = standing_authorization_enabled() and not manual_only
            if manual_only and candidate.get("manual_only") is not True:
                errors.append(
                    "manual-only entry's promoted_candidate must set manual_only=true"
                )
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
        player_count = snapshot.get("player_count", 0)
        away_n, home_n = len(away), len(home)
        # Distinguish a genuine pre-lineup state from a data/resolution failure so a
        # future resolver bug shows up loudly instead of masquerading as a clean pass.
        # A loaded feed carries the full roster (~40-60 players) well before lineups
        # post; a near-empty feed means the game/feed did not resolve.
        if away_n >= 9 and home_n >= 9:
            state = "STATE: both batting orders CONFIRMED (9 and 9)."
        elif player_count >= 20:
            state = (
                f"STATE: feed loaded ({player_count} roster players) but batting orders NOT "
                f"yet posted ({away_n}/9, {home_n}/9) — this is a genuine pre-lineup state; a "
                f"pass here is legitimate, NOT a data error."
            )
        else:
            state = (
                f"STATE: feed SPARSE ({player_count} players, orders {away_n}/9 and {home_n}/9) "
                f"— likely a game-resolution or feed FAILURE, not a real 'no lineup'. Do NOT "
                f"treat this as a clean pass; fail the gate as a data error."
            )
        away_sp = snapshot.get("away_probable_pitcher") or ""
        home_sp = snapshot.get("home_probable_pitcher") or ""
        starter_line = (
            "PROBABLE STARTERS: "
            f"{snapshot.get('away_team')} — {away_sp or 'NOT YET ANNOUNCED'}; "
            f"{snapshot.get('home_team')} — {home_sp or 'NOT YET ANNOUNCED'}. "
            "For a starter_unannounced entry, an arm shown here is the authoritative "
            "announcement — re-handicap against it; 'NOT YET ANNOUNCED' means the "
            "starter blocker is unresolved, so do NOT promote yet."
        )
        sections.append(
            "\n".join(
                [
                    f"MLB gamePk {snapshot.get('game_pk')} — {player_count} roster players",
                    state,
                    starter_line,
                    f"{snapshot.get('away_team')} batting order ({away_n}): {', '.join(away)}",
                    f"{snapshot.get('home_team')} batting order ({home_n}): {', '.join(home)}",
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
    starter_ids = [
        str(e.get("id"))
        for e in entries
        if STARTER_BLOCKER in (e.get("blocked_only_by") or [])
    ]
    policy = load_mlb_selection_policy()
    edge_floor = policy.min_conservative_edge if policy is not None else 0.05
    starter_block = ""
    if starter_ids and (policy is None or not policy.starter_pending_promotions_enabled):
        # Fail closed: a missing policy means promotions are disabled too — the
        # prompt must match the validator, which refuses starter-pending
        # promotions whenever the policy does not explicitly enable them.
        reason = (
            "no loadable shared MLB selection policy"
            if policy is None
            else "the shared MLB selection policy sets "
            "starter_pending_promotions_enabled=false"
        )
        starter_block = (
            "\n\nSTARTER-PENDING PROMOTIONS DISABLED — entries " + ", ".join(starter_ids)
            + f" are blocked by starter_unannounced, and {reason}, so these entries "
            "MUST NOT be promoted under any circumstances this cycle: set status=passed "
            "with the reason, regardless of the announced starter or recomputed edge."
        )
    elif starter_ids:
        starter_block = (
            "\n\nSTARTER-PENDING RE-HANDICAP — entries " + ", ".join(starter_ids) + " were "
            "carded off a PROVISIONAL win_probability because the opposing starter had "
            "not been announced at slate time. For each of these you MUST, before any "
            "promotion:\n"
            "- Read the announced PROBABLE STARTERS provided above. If your side's or the "
            "opponent's starter still shows NOT YET ANNOUNCED, keep status "
            "pending_lineup_recheck (do not pass, do not promote) — it is simply not "
            "resolvable yet this cycle.\n"
            "- Re-handicap the game against the real announced opposing starter and "
            "recompute win_probability from the full read. Use mlb_pitcher_season "
            "(mcp-sports-data) for that starter's line if you need it — never curl/web.\n"
            "- Recompute net_edge = win_probability - current_ask (no fee). Promote ONLY "
            f"if net_edge >= {edge_floor} with the real starter AND the two deferred gates "
            "(opposing_starter_shutdown_path, real_winner_conviction) now genuinely pass. "
            "If the announced starter erases the edge or fails a gate, set status=passed "
            "with the reason — this is the safety hinge; the morning number is discarded.\n"
            "- Set the promoted_candidate max_polymarket_price to your recomputed "
            f"win_probability minus {edge_floor} (the true break-even-plus-floor ceiling), NOT the "
            "provisional slate ceiling, so the execution poller cannot chase past the edge.\n"
            "- In the recheck object, set starter_confirmed=true and "
            "net_edge_recomputed=true to record that the re-handicap happened."
        )
    manual_only_ids = [str(e.get("id")) for e in entries if entry_is_manual_only(e)]
    if manual_only_ids:
        routing += (
            "\n\nMANUAL-ONLY OVERRIDE — entries " + ", ".join(manual_only_ids) + " are "
            "manual-only and MUST NOT auto-execute even if standing authorization is enabled. "
            "For each of these, the promoted_candidate MUST use execution_mode=manual, "
            "manual_bet_status=awaiting_jerry, manual_only=true, vig_approved=true, "
            "executed=false, and no execution cron fields. They await Jerry's explicit "
            "confirmation and are never sent to the execution poller."
        )
    lineup_context = _lineup_context(entries, snapshots)
    return f"""You are Vig performing the MLB lineup watchlist recheck.
Read and update {schedule_path}. Recheck only these watchlist IDs: {entry_ids}.
{lineup_context}

Use ONLY the deterministic data provided above — do NOT web-search, curl, or
web-extract for lineups or price (those tools are unreliable in this sandbox;
the data above was fetched in-process and is authoritative). For each entry,
validate:
- confirmed batting lineups for both teams: a side is confirmed when its
  provided batting order lists 9 hitters. The confirmed order IS the
  authoritative late-scratch signal — a scratched hitter is simply absent, so
  do not separately refresh injuries from external sources. Note an injury
  concern only if a missing name materially breaks the stated thesis; if
  you need the IL list, use the mlb_injuries MCP tool (mcp-sports-data) with the
  team id — never curl or web-search for injuries.
- current price: use the provided Polymarket ask for your side (match your team
  to the long/YES or NO-side ask by comparing to the slate-captured ask in the
  thesis). The current ask must be no worse than the entry's bettable_to ceiling.

Re-run every original gate against these facts. Promote when both lineups are
confirmed, no starting pitcher changed, the provided current ask is within the
ceiling, and every original gate still holds. If the provided price is
unavailable but lineups are confirmed and gates hold, still promote and carry
the stored ceiling as max_polymarket_price — the recurring execution poller
enforces the live price deterministically at order time. {routing}{starter_block}

{review_prompt_evidence_section()}

Set status=passed with a concise recheck_notes reason ONLY for a real signal
failure: lineups genuinely unconfirmed at recheck time, a scratch/injury that
breaks the thesis, the provided current ask exceeding the ceiling, or an
original gate that no longer holds. Do NOT pass merely because an external
refresh tool was unavailable. For a promotion, set status=promoted and record
recheck.lineups_confirmed, recheck.key_injuries_refreshed,
recheck.price_refreshed, and recheck.all_original_gates_hold as true, plus the
promoted_candidate. Always set rechecked_at_utc. Do not execute here, create an
approval token, call a trading endpoint, or create a cron job; route through the
recurring MLB execution poller.
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
