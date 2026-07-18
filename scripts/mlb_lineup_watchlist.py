#!/usr/bin/env python3
"""Select and validate lineup-dependent MLB watchlist rechecks.

The morning slate owns creation of ``lineup_watchlist`` entries. This module
provides the deterministic timing and safety checks used by Vig's conditional
review gate; the LLM reviewer still refreshes the live baseball inputs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_runtime_policy import standing_authorization_enabled

MIN_MINUTES_BEFORE_FIRST_PITCH = 60
MAX_MINUTES_BEFORE_FIRST_PITCH = 90
PENDING_STATUS = "pending_lineup_recheck"
TERMINAL_STATUSES = {"promoted", "passed"}
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


def build_recheck_prompt(schedule_path: Path, entries: list[dict[str, Any]]) -> str:
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
    return f"""You are Vig performing the MLB lineup watchlist recheck.
Read and update {schedule_path}. Recheck only these watchlist IDs: {entry_ids}.

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
