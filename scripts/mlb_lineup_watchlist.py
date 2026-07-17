#!/usr/bin/env python3
"""Select and validate lineup-dependent MLB watchlist rechecks.

The morning slate owns creation of ``lineup_watchlist`` entries. This module
provides the deterministic timing and safety checks used by Vig's conditional
review gate; the LLM reviewer still refreshes the live baseball inputs.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIN_MINUTES_BEFORE_FIRST_PITCH = 60
MAX_MINUTES_BEFORE_FIRST_PITCH = 90
PENDING_STATUS = "pending_lineup_recheck"
REQUIRED_ORIGINAL_GATES = {
    "starter_floor",
    "opposing_starter_shutdown_path",
    "bullpen_close_game_survival",
    "cold_fade_reset",
    "price_discipline",
    "real_winner_conviction",
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


def validate_entry(entry: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if entry.get("blocked_only_by") != ["lineups_unconfirmed"]:
        errors.append("blocked_only_by must contain only lineups_unconfirmed")
    if parse_instant(entry.get("first_pitch_utc")) is None:
        errors.append("first_pitch_utc must be a valid timestamp")
    gates = entry.get("original_gate_results")
    if not isinstance(gates, dict):
        errors.append("original_gate_results must be an object")
    else:
        for gate in sorted(REQUIRED_ORIGINAL_GATES):
            if gates.get(gate) is not True:
                errors.append(f"original_gate_results.{gate} must be true")
        if gates.get("lineups_confirmed") is not False:
            errors.append("original_gate_results.lineups_confirmed must be false")

    if entry.get("status") == "promoted":
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
            if candidate.get("execution_mode") != "manual":
                errors.append("promoted_candidate.execution_mode must be manual")
            if candidate.get("manual_bet_status") != "awaiting_jerry":
                errors.append("promoted_candidate.manual_bet_status must be awaiting_jerry")
            if candidate.get("executed") is not False:
                errors.append("promoted_candidate.executed must be false")
    return errors


def due_entries(schedule: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_entries = schedule.get("lineup_watchlist", [])
    if not isinstance(raw_entries, list):
        return []
    due: list[dict[str, Any]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict) or entry.get("status") != PENDING_STATUS:
            continue
        if validate_entry(entry):
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
    return f"""You are Vig performing the MLB lineup watchlist recheck.
Read and update {schedule_path}. Recheck only these watchlist IDs: {entry_ids}.

For each entry, refresh from live sources:
- confirmed batting lineups for both teams;
- key injury status and late scratches;
- current supported-market price and the stored bettable-to threshold.

Re-run every original gate using the refreshed facts. Promote only when lineups
are confirmed, injury and price refreshes succeeded, and every original gate
still holds. A promotion must be copied into candidates with
execution_mode=manual, manual_bet_status=awaiting_jerry, executed=false,
vig_review_needed=false, vig_approved=true, and no execution cron fields.
It is only a reminder for Jerry and must never place or schedule a bet.

If any refresh is unavailable, the price is too expensive, a lineup/injury
change weakens the thesis, or any original gate fails, set the watchlist entry
status to passed and write a concise recheck_notes reason. For a promotion, set
status=promoted and record recheck.lineups_confirmed,
recheck.key_injuries_refreshed, recheck.price_refreshed, and
recheck.all_original_gates_hold as true, plus the manual promoted_candidate.
Always set rechecked_at_utc. Never auto-execute, create an approval token, call
a trading endpoint, or create an execution cron.
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
        errors: dict[str, list[str]] = {}
        for index, entry in enumerate(schedule.get("lineup_watchlist", [])):
            if not isinstance(entry, dict):
                errors[str(index)] = ["entry must be an object"]
                continue
            entry_errors = validate_entry(entry)
            if entry_errors:
                errors[str(entry.get("id", index))] = entry_errors
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
