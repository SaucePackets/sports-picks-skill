#!/usr/bin/env python3
"""Conditional Vig review gate shared by MLB and soccer cron wrappers."""

from __future__ import annotations

import json
import os
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
from mlb_runtime_policy import standing_authorization_enabled  # noqa: E402

HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/home/clawdbot/.local/bin/hermes"


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


def _strict_price(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 < value < 1
    )


def _strict_polymarket_ask(
    candidate: dict[str, Any], original: dict[str, Any] | None
) -> int | float | None:
    prices = [
        candidate[field]
        for field in ("approved_polymarket_ask", "captured_polymarket_ask")
        if _strict_price(candidate.get(field))
    ]
    if original is not None:
        for field in ("approved_polymarket_ask", "captured_polymarket_ask", "polymarket_ask"):
            value = original.get(field)
            if _strict_price(value):
                prices.append(value)
    elif _strict_price(candidate.get("polymarket_ask")):
        prices.append(candidate["polymarket_ask"])
    return min(prices) if prices else None


def normalize_review_routing(
    before: dict[str, Any],
    after: dict[str, Any],
    sport: str,
    mlb_standing_authorized: bool = False,
) -> list[str]:
    """Deterministically route newly approved MLB candidates after child review."""
    if sport.upper() != "MLB" or not mlb_standing_authorized:
        return []

    try:
        before_candidates = parse_candidates(before)
        after_candidates = parse_candidates(after)
    except ScheduleFormatError as exc:
        return [str(exc)]
    seen_identities: set[str] = set()
    duplicate_errors: list[str] = []
    for candidate in after_candidates:
        identity = candidate_identity(candidate)
        if identity in seen_identities:
            duplicate_errors.append(
                f"candidate {identity} appears more than once after review"
            )
        seen_identities.add(identity)
    if duplicate_errors:
        return duplicate_errors
    before_by_id = {candidate_identity(item): item for item in before_candidates}
    before_watchlist_ids = {
        entry.get("id")
        for entry in before.get("lineup_watchlist", [])
        if isinstance(entry, dict) and entry.get("id")
    }
    promoted_watchlist_ids = {
        entry.get("id")
        for entry in after.get("lineup_watchlist", [])
        if isinstance(entry, dict)
        and entry.get("status") == "promoted"
        and entry.get("id") in before_watchlist_ids
    }
    newly_approved = [
        candidate
        for candidate in after_candidates
        if candidate.get("vig_approved") is True
        and before_by_id.get(candidate_identity(candidate), {}).get("vig_approved") is not True
    ]
    prices: list[tuple[dict[str, Any], int | float]] = []
    errors: list[str] = []
    for candidate in newly_approved:
        identity = candidate_identity(candidate)
        original = before_by_id.get(identity)
        if original is None and candidate.get("watchlist_id") not in promoted_watchlist_ids:
            errors.append(
                f"candidate {identity} was not a targeted candidate or watchlist promotion"
            )
            continue
        ask = _strict_polymarket_ask(candidate, original)
        if ask is None:
            errors.append(
                f"candidate {candidate_identity(candidate)} has no strict numeric "
                "approved Polymarket ask"
            )
        else:
            prices.append((candidate, ask))
    if errors:
        return errors

    after["sport"] = "MLB"
    after["market_type"] = "moneyline"
    for candidate, ask in prices:
        candidate.update(
            sport="MLB",
            market_type="moneyline",
            execution_mode="standing_authorized",
            execution_status="pending",
            executed=False,
            max_polymarket_price=ask,
        )
        candidate.pop("manual_bet_status", None)

    normalized_by_watchlist_id = {
        candidate.get("watchlist_id"): candidate
        for candidate, _ in prices
        if candidate.get("watchlist_id")
    }
    for entry in after.get("lineup_watchlist", []):
        if not isinstance(entry, dict):
            continue
        normalized = normalized_by_watchlist_id.get(entry.get("id"))
        if normalized is not None and entry.get("status") == "promoted":
            entry["promoted_candidate"] = dict(normalized)
    return []


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


def approved_candidate_errors(
    candidate: dict[str, Any], sport: str, mlb_standing_authorized: bool = False
) -> list[str]:
    """Validate the post-review routing state for an approved candidate."""
    if sport.upper() != "MLB" or not mlb_standing_authorized:
        return manual_candidate_errors(candidate)

    errors: list[str] = []
    if candidate.get("sport") != "MLB":
        errors.append("sport must be MLB")
    if candidate.get("market_type") != "moneyline":
        errors.append("market_type must be moneyline")
    if candidate.get("execution_mode") != "standing_authorized":
        errors.append("execution_mode must be standing_authorized")
    if candidate.get("execution_status") != "pending":
        errors.append("execution_status must be pending")
    if candidate.get("manual_bet_status") == "awaiting_jerry":
        errors.append("manual_bet_status must not be awaiting_jerry")
    if candidate.get("executed") is not False:
        errors.append("executed must be false")
    max_price = candidate.get("max_polymarket_price")
    if (
        not isinstance(max_price, (int, float))
        or isinstance(max_price, bool)
        or not 0 < max_price < 1
    ):
        errors.append("max_polymarket_price must be between 0 and 1")
    forbidden = sorted(
        field
        for field in ("execution_cron_id", "execution_cron_fire_utc", "approval_token")
        if field in candidate
    )
    if forbidden:
        errors.append(
            f"forbidden execution fields present: {', '.join(forbidden)}; "
            "use the recurring MLB execution poller"
        )
    return errors


def validate_review_transition(
    before: dict[str, Any],
    after: dict[str, Any],
    candidate_ids: list[str],
    watchlist_ids: list[str],
    sport: str = "MLB",
    mlb_standing_authorized: bool = False,
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

    before_by_id = {candidate_identity(item): item for item in before_candidates}
    after_by_id = {candidate_identity(item): item for item in after_candidates}
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
            errors.extend(
                f"candidate {identity}: {message}"
                for message in approved_candidate_errors(
                    candidate, sport, mlb_standing_authorized
                )
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

    for entry_id, entry in before_watch.items():
        if entry_id not in targeted_watch and after_watch.get(entry_id) != entry:
            errors.append(f"untargeted watchlist {entry_id} changed")
    return errors


def review_work(
    schedule: dict[str, Any], sport: str, now: datetime | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = pending_candidates(parse_candidates(schedule))
    watchlist = due_entries(schedule, now) if sport == "MLB" else []
    return candidates, watchlist


def build_regular_review_prompt(
    sport: str,
    day: str,
    schedule_path: Path,
    candidates: list[dict[str, Any]],
    mlb_standing_authorized: bool = False,
) -> str:
    sides = ", ".join(str(candidate.get("side", "<unknown>")) for candidate in candidates)
    routing = (
        """For an MLB approval under Jerry's standing authorization, set
execution_mode=standing_authorized, execution_status=pending, and executed=false.
Set sport=MLB, market_type=moneyline, and an explicit max_polymarket_price
between 0 and 1 from the approved price
discipline rail. Remove any legacy manual reminder status. Do not create a one-shot cron,
approval token, or trading command here. The recurring MLB execution poller will
refresh every gate and handle capped execution with canonical receipts.
"""
        if sport.upper() == "MLB" and mlb_standing_authorized
        else """Every approval is manual-only: set execution_mode=manual,
manual_bet_status=awaiting_jerry, and executed=false. Include no execution cron,
approval token, or trading command. An approved candidate is only a reminder for
Jerry and must never place or schedule a bet.
"""
    )
    return f"""You are Vig performing the independent {sport} card review for {day}.
Read {schedule_path}. Review only pending candidates: {sides}. Refresh decisive
inputs and current supported-market prices, then apply every original hard gate.
Update each reviewed candidate with boolean vig_approved and concise vig_notes.

{routing}

Return a concise card review with approved/rejected count, decisive reason per
candidate, and total proposed exposure.
"""


def _schedule_path(sport: str, day: str) -> Path:
    if sport == "MLB":
        return ROOT / ".picks" / "execute" / f"{day}-schedule.json"
    return ROOT / ".picks" / "execute" / "intl-soccer" / f"{day}-schedule.json"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def write_latest_action(
    sport: str,
    day: str,
    schedule: dict[str, Any],
    mlb_standing_authorized: bool = False,
) -> Path:
    candidates = parse_candidates(schedule)
    approved = sum(candidate.get("vig_approved") is True for candidate in candidates)
    rejected = sum(candidate.get("vig_approved") is False for candidate in candidates)
    pending_watch = sum(
        isinstance(entry, dict) and entry.get("status") == "pending_lineup_recheck"
        for entry in schedule.get("lineup_watchlist", [])
    )
    label = sport.upper()
    if label == "MLB" and mlb_standing_authorized:
        text = (
            f"{day}: MLB review complete. {approved} approved standing-authorized "
            f"{_plural(approved, 'candidate')} routed to execution poller; "
            f"{rejected} rejected. "
        )
    else:
        text = (
            f"{day}: {label} review complete. {approved} approved manual-only "
            f"{_plural(approved, 'candidate')} awaiting Jerry; {rejected} rejected. "
        )
    if label == "MLB":
        text += (
            f"{pending_watch} lineup watchlist {_plural(pending_watch, 'recheck')} pending. "
        )
        exposure = sum(
            float(candidate.get("unit_size", 0))
            for candidate in candidates
            if candidate.get("vig_approved") is True
            and isinstance(candidate.get("unit_size"), (int, float))
            and not isinstance(candidate.get("unit_size"), bool)
        )
        cap = schedule.get("daily_cap")
        if isinstance(cap, (int, float)) and not isinstance(cap, bool):
            text += f"Approved exposure ${exposure:g} / ${cap:g}. "
    text += "Review gate placed no bet.\n"

    path = ROOT / ".picks" / "latest-action.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return path


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
    mlb_standing_authorized = sport.upper() == "MLB" and standing_authorization_enabled()

    prompts: list[str] = []
    if candidates:
        prompts.append(
            build_regular_review_prompt(
                sport, day, schedule_path, candidates, mlb_standing_authorized
            )
        )
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
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=1800)
    if proc.returncode:
        print(f"{sport} review failed (exit {proc.returncode}):\n{(proc.stderr or proc.stdout).strip()[:3000]}")
        return proc.returncode

    try:
        updated = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{sport} review gate ERROR: could not validate reviewed state: {exc}")
        return 1
    if not isinstance(updated, dict):
        print(f"{sport} review gate ERROR: reviewed schedule must remain an object")
        return 1
    normalization_errors = normalize_review_routing(
        schedule, updated, sport, mlb_standing_authorized
    )
    if normalization_errors:
        print(
            f"{sport} review gate ERROR: routing normalization failed closed: "
            f"{'; '.join(normalization_errors)}"
        )
        return 1
    transition_errors = validate_review_transition(
        schedule,
        updated,
        candidate_ids,
        watchlist_ids,
        sport,
        mlb_standing_authorized,
    )
    if transition_errors:
        print(f"{sport} review gate ERROR: invalid review transition: {'; '.join(transition_errors)}")
        return 1
    try:
        write_latest_action(sport, day, updated, mlb_standing_authorized)
        temporary = schedule_path.with_suffix(f"{schedule_path.suffix}.tmp")
        temporary.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
        temporary.replace(schedule_path)
    except (OSError, ScheduleFormatError) as exc:
        print(f"{sport} review gate ERROR: could not persist reviewed state: {exc}")
        return 1

    out = proc.stdout.strip()
    lines = out.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith(("Vig review", "## Vig", "Card review", "Approved:", "MLB lineup"))
    ]
    if starts:
        out = "\n".join(lines[starts[-1] :]).strip()
    if out and out != "[SILENT]":
        print(out)
    return 0
