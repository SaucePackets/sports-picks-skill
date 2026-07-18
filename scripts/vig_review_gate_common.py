#!/usr/bin/env python3
"""Conditional Vig review gate shared by MLB and soccer cron wrappers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_lineup_watchlist import (  # noqa: E402
    WatchlistFormatError,
    build_recheck_prompt,
    due_entries,
    validate_watchlist,
)

HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/home/clawdbot/.local/bin/hermes"
MANUAL_NOTICE = "Manual placement required; no bet submitted."


def resolve_root(cwd: Path | None = None, home: Path | None = None) -> Path:
    """Resolve runtime state even when Hermes launches a script from its profile directory."""
    override = os.environ.get("SPORTS_PICKS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = (cwd or Path.cwd()).expanduser().resolve()
    if (current / ".picks").is_dir():
        return current
    default = ((home or Path.home()) / "projects" / "sports-picks-skill").resolve()
    if (default / ".picks").is_dir():
        return default
    return current


ROOT = resolve_root()


class ScheduleFormatError(ValueError):
    """Raised when a review schedule is valid JSON but has no candidate list."""


def parse_candidates(data: object) -> list[dict[str, Any]]:
    """Accept raw candidate arrays and schedule objects with candidates."""
    if isinstance(data, list):
        if not all(isinstance(item, dict) for item in data):
            raise ScheduleFormatError("every candidate must be an object")
        return data
    if not isinstance(data, dict):
        raise ScheduleFormatError(f"expected object or list, got {type(data).__name__}")
    if "candidates" not in data:
        raise ScheduleFormatError("schedule object is missing candidates")
    candidates = data["candidates"]
    if not isinstance(candidates, list):
        raise ScheduleFormatError(f"candidates must be a list, got {type(candidates).__name__}")
    if not all(isinstance(item, dict) for item in candidates):
        raise ScheduleFormatError("every candidate must be an object")
    return candidates


def pending_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [candidate for candidate in candidates if not isinstance(candidate.get("vig_approved"), bool)]


def candidate_identity(candidate: dict[str, Any]) -> str:
    for field in ("id", "watchlist_id", "polymarket_slug", "market_slug", "event_id"):
        value = candidate.get(field)
        if value not in (None, ""):
            return f"{field}:{value}|side:{candidate.get('side', '')}"
    return f"side:{candidate.get('side', '')}|game:{candidate.get('game', '')}"


def manual_candidate_errors(candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if candidate.get("execution_mode") != "manual":
        errors.append("execution_mode must be manual")
    if candidate.get("manual_bet_status") != "awaiting_jerry":
        errors.append("manual_bet_status must be awaiting_jerry")
    if candidate.get("executed") is not False:
        errors.append("executed must be false")
    forbidden = sorted(
        field
        for field in ("execution_cron_id", "execution_cron_fire_utc", "approval_token")
        if field in candidate
    )
    if forbidden:
        errors.append(f"forbidden execution fields present: {', '.join(forbidden)}")
    return errors


def validate_review_transition(
    before: dict[str, Any],
    after: dict[str, Any],
    candidate_ids: list[str],
    watchlist_ids: list[str],
) -> list[str]:
    errors: list[str] = []
    watch_errors = validate_watchlist(after)
    for entry_id, entry_errors in watch_errors.items():
        errors.extend(f"watchlist {entry_id}: {message}" for message in entry_errors)
    try:
        before_candidates = parse_candidates(before)
        after_candidates = parse_candidates(after)
    except ScheduleFormatError as exc:
        return [str(exc), *errors]

    before_identities = [candidate_identity(item) for item in before_candidates]
    after_identities = [candidate_identity(item) for item in after_candidates]
    before_by_id = dict(zip(before_identities, before_candidates))
    after_by_id = dict(zip(after_identities, after_candidates))
    for identity in sorted(set(before_identities)):
        if before_identities.count(identity) > 1:
            errors.append(f"candidate identity {identity} is duplicated before review")
    for identity in sorted(set(after_identities)):
        if after_identities.count(identity) > 1:
            errors.append(f"candidate identity {identity} is duplicated after review")
    targeted_candidates = set(candidate_ids)
    for identity in targeted_candidates:
        candidate = after_by_id.get(identity)
        if candidate is None:
            errors.append(f"candidate {identity} missing after review")
            continue
        if not isinstance(candidate.get("vig_approved"), bool):
            errors.append(f"candidate {identity} has no boolean decision")
        notes = candidate.get("vig_notes")
        if not isinstance(notes, str) or not notes.strip():
            errors.append(f"candidate {identity} has empty vig_notes")
        if candidate.get("vig_approved") is True:
            errors.extend(f"candidate {identity}: {message}" for message in manual_candidate_errors(candidate))
        else:
            if candidate.get("execution_mode") not in (None, "manual"):
                errors.append(f"candidate {identity}: execution_mode must remain manual")
            if candidate.get("executed") is not False:
                errors.append(f"candidate {identity}: executed must be false")
            forbidden = sorted(
                field
                for field in ("execution_cron_id", "execution_cron_fire_utc", "approval_token")
                if field in candidate
            )
            if forbidden:
                errors.append(
                    f"candidate {identity}: forbidden execution fields present: "
                    f"{', '.join(forbidden)}"
                )

    for identity, candidate in before_by_id.items():
        if identity not in targeted_candidates and after_by_id.get(identity) != candidate:
            errors.append(f"untargeted candidate {identity} changed")

    before_watch = {
        item.get("id"): item
        for item in before.get("lineup_watchlist", [])
        if isinstance(item, dict) and item.get("id")
    }
    after_watch = {
        item.get("id"): item
        for item in after.get("lineup_watchlist", [])
        if isinstance(item, dict) and item.get("id")
    }
    targeted_watch = set(watchlist_ids)
    for entry_id in targeted_watch:
        entry = after_watch.get(entry_id)
        if entry is None:
            errors.append(f"watchlist {entry_id} missing after review")
            continue
        status = entry.get("status")
        if status not in ("promoted", "passed"):
            errors.append(f"watchlist {entry_id} did not reach promoted or passed")
            continue
        if status == "promoted":
            matches = [item for item in after_candidates if item.get("watchlist_id") == entry_id]
            if len(matches) != 1:
                errors.append(f"watchlist {entry_id} must map to exactly one candidate")
            elif matches[0] != entry.get("promoted_candidate"):
                errors.append(f"watchlist {entry_id} promoted_candidate differs from candidates entry")
            candidate = entry.get("promoted_candidate")
            if isinstance(candidate, dict):
                if candidate.get("vig_approved") is not True:
                    errors.append(f"watchlist {entry_id} promoted candidate must be vig_approved")
                notes = candidate.get("vig_notes")
                if not isinstance(notes, str) or not notes.strip():
                    errors.append(f"watchlist {entry_id} promoted candidate has empty vig_notes")

    for entry_id, entry in before_watch.items():
        if entry_id not in targeted_watch and after_watch.get(entry_id) != entry:
            errors.append(f"untargeted watchlist {entry_id} changed")
    for entry_id in after_watch:
        if entry_id not in before_watch:
            errors.append(f"unexpected watchlist {entry_id} added during review")

    allowed_promotions = {
        candidate_identity(entry["promoted_candidate"])
        for entry_id, entry in after_watch.items()
        if entry_id in targeted_watch
        and entry.get("status") == "promoted"
        and isinstance(entry.get("promoted_candidate"), dict)
    }
    for identity in after_by_id:
        if identity not in before_by_id and identity not in allowed_promotions:
            errors.append(f"unexpected candidate {identity} added during review")
    return errors


def review_work(
    schedule: dict[str, Any], sport: str, now: datetime | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = pending_candidates(parse_candidates(schedule))
    watchlist = due_entries(schedule, now) if sport == "MLB" else []
    return candidates, watchlist


def build_regular_review_prompt(
    sport: str, day: str, schedule_path: Path, candidates: list[dict[str, Any]]
) -> str:
    sides = ", ".join(str(candidate.get("side", "<unknown>")) for candidate in candidates)
    return f"""You are Vig performing the independent {sport} card review for {day}.
Read {schedule_path}. Review only pending candidates: {sides}. Refresh decisive
inputs and current supported-market prices, then apply every original hard gate.
Update each reviewed candidate with boolean vig_approved and concise vig_notes.

Every approval is manual-only: set execution_mode=manual,
manual_bet_status=awaiting_jerry, and executed=false. Include no execution cron,
approval token, or trading command. An approved candidate is only a reminder for
Jerry and must never place or schedule a bet.

Return a concise card review with approved/rejected count, decisive reason per
candidate, and total proposed exposure.
"""


def _schedule_path(sport: str, day: str) -> Path:
    if sport == "MLB":
        return ROOT / ".picks" / "execute" / f"{day}-schedule.json"
    return ROOT / ".picks" / "execute" / "intl-soccer" / f"{day}-schedule.json"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def write_latest_action(sport: str, day: str, schedule: dict[str, Any]) -> Path:
    candidates = parse_candidates(schedule)
    approved = sum(candidate.get("vig_approved") is True for candidate in candidates)
    rejected = sum(candidate.get("vig_approved") is False for candidate in candidates)
    pending_watch = sum(
        isinstance(entry, dict) and entry.get("status") == "pending_lineup_recheck"
        for entry in schedule.get("lineup_watchlist", [])
    )
    label = sport.upper()
    text = (
        f"{day}: {label} review complete. {approved} approved manual-only "
        f"{_plural(approved, 'candidate')} awaiting Jerry; {rejected} rejected. "
    )
    if label == "MLB":
        text += (
            f"{pending_watch} lineup watchlist {_plural(pending_watch, 'recheck')} pending. "
        )
    text += "No bet placed or scheduled.\n"

    path = ROOT / ".picks" / "latest-action.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return path


def _american_price(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"+{value:g}" if value > 0 else f"{value:g}"
    text = str(value).strip()
    return text if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text) else "not recorded"


def _size_status(candidate: dict[str, Any]) -> str:
    size = candidate.get("unit_size")
    rendered_size = f"${size:g}" if isinstance(size, (int, float)) else "size not recorded"
    return f"{rendered_size}; awaiting Jerry"


def _concise_reason(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "No reason recorded.").split())
    text = re.sub(r"\*{3}\s*(?:Begin|End) Patch\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\*{3}\s*Update File:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{[^{}]*\}", "[structured data omitted]", text)
    text = re.sub(r"(?<!\w)/(?:[\w.-]+/)+[\w.-]+", "[path omitted]", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    shortened = text[: limit - 3].rsplit(" ", 1)[0]
    return f"{shortened or text[: limit - 3]}..."


def build_lineup_recheck_report(schedule: dict[str, Any], watchlist_ids: list[str]) -> str:
    entries = {
        str(entry.get("id")): entry
        for entry in schedule.get("lineup_watchlist", [])
        if isinstance(entry, dict)
    }
    sections: list[str] = []
    for entry_id in watchlist_ids:
        entry = entries[entry_id]
        approved = entry.get("status") == "promoted"
        candidate = entry.get("promoted_candidate") if approved else {}
        if not isinstance(candidate, dict):
            candidate = {}
        side = _concise_reason(candidate.get("side") or entry.get("side") or "not recorded", 80)
        current_price = candidate.get("price", entry.get("current_price", "not recorded"))
        bettable_to = candidate.get("bettable_to_price", entry.get("bettable_to_price"))
        reason = entry.get("recheck_notes") or candidate.get("vig_notes") or "No reason recorded."
        lines = [
            f"MLB lineup recheck — {'APPROVED' if approved else 'REJECTED'}",
            f"Pick: {side}",
            f"Current price: {_american_price(current_price)}",
            f"Bettable to: {_american_price(bettable_to)}",
            f"Reason: {_concise_reason(reason)}",
        ]
        if approved:
            lines.append(f"Size/status: {_size_status(candidate)}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def build_regular_review_report(
    schedule: dict[str, Any], sport: str, candidate_ids: list[str]
) -> str:
    candidates = {candidate_identity(item): item for item in parse_candidates(schedule)}
    reviewed = [candidates[identity] for identity in candidate_ids]
    approved = sum(candidate.get("vig_approved") is True for candidate in reviewed)
    rejected = sum(candidate.get("vig_approved") is False for candidate in reviewed)
    lines = [f"{sport} card review — {approved} approved, {rejected} rejected"]
    for candidate in reviewed:
        decision = "APPROVED" if candidate.get("vig_approved") is True else "REJECTED"
        side = _concise_reason(candidate.get("side") or candidate.get("game") or "not recorded", 80)
        reason = _concise_reason(candidate.get("vig_notes"))
        line = f"- {decision} {side}: {reason}"
        if decision == "APPROVED":
            line += f" Size/status: {_size_status(candidate)}"
        lines.append(line)
    return "\n".join(lines)


def build_validated_review_report(
    schedule: dict[str, Any],
    sport: str,
    candidate_ids: list[str],
    watchlist_ids: list[str],
) -> str:
    sections: list[str] = []
    if candidate_ids:
        sections.append(build_regular_review_report(schedule, sport, candidate_ids))
    if watchlist_ids:
        sections.append(build_lineup_recheck_report(schedule, watchlist_ids))
    body = "\n\n".join(sections)
    return f"{body}\n{MANUAL_NOTICE}"


def run_gate(sport: str) -> int:
    day = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    schedule_path = _schedule_path(sport, day)
    if not schedule_path.exists():
        return 0
    try:
        data = json.loads(schedule_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"{sport} review gate ERROR: invalid schedule JSON: {exc}")
        return 1
    if isinstance(data, list):
        if not data:
            return 0
        print(f"{sport} review gate ERROR: non-empty legacy array schedule requires migration")
        return 1
    elif isinstance(data, dict):
        schedule = data
    else:
        print(f"{sport} review gate ERROR: expected object or list, got {type(data).__name__}")
        return 1
    try:
        candidates, watchlist = review_work(schedule, sport)
    except (ScheduleFormatError, WatchlistFormatError) as exc:
        print(f"{sport} review gate ERROR: {exc}")
        return 1
    if not candidates and not watchlist:
        return 0

    candidate_ids = [candidate_identity(candidate) for candidate in candidates]
    watchlist_ids = [str(entry["id"]) for entry in watchlist]

    prompts: list[str] = []
    if candidates:
        prompts.append(build_regular_review_prompt(sport, day, schedule_path, candidates))
    if watchlist:
        prompts.append(build_recheck_prompt(schedule_path, watchlist))
    prompt = "\n\n".join(prompts)
    cmd = [
        HERMES,
        "--profile",
        "vig",
        "--skills",
        "sports-betting-markets,sports-data-apis",
        "chat",
        "-q",
        prompt,
        "-t",
        "file,web,skills",
        "--quiet",
    ]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print(
            f"{sport} review gate ERROR: child reviewer timed out; reviewed state was not "
            "accepted. Retry the job and inspect Vig session logs."
        )
        return 1
    except OSError:
        print(
            f"{sport} review gate ERROR: child reviewer could not start; reviewed state was "
            "not accepted. Verify the Hermes CLI and retry the job."
        )
        return 1
    if proc.returncode:
        print(
            f"{sport} review gate ERROR: child reviewer exited {proc.returncode}; "
            "reviewed state was not accepted. Retry the job and inspect Vig session logs."
        )
        return proc.returncode

    try:
        updated = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{sport} review gate ERROR: could not validate reviewed state: {exc}")
        return 1
    if not isinstance(updated, dict):
        print(f"{sport} review gate ERROR: reviewed schedule must remain an object")
        return 1
    transition_errors = validate_review_transition(schedule, updated, candidate_ids, watchlist_ids)
    if transition_errors:
        print(f"{sport} review gate ERROR: invalid review transition: {'; '.join(transition_errors)}")
        return 1
    try:
        write_latest_action(sport, day, updated)
    except (OSError, ScheduleFormatError) as exc:
        print(f"{sport} review gate ERROR: could not update latest-action.md: {exc}")
        return 1

    print(build_validated_review_report(updated, sport, candidate_ids, watchlist_ids))
    return 0
