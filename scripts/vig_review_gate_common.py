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

from mlb_lineup_watchlist import build_recheck_prompt, due_entries  # noqa: E402

HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/home/clawdbot/.local/bin/hermes"
ROOT = Path(os.environ.get("SPORTS_PICKS_ROOT", Path.cwd())).expanduser().resolve()


class ScheduleFormatError(ValueError):
    """Raised when a review schedule is valid JSON but has no candidate list."""


def parse_candidates(data: object) -> list[dict[str, Any]]:
    """Accept raw candidate arrays and schedule objects with candidates."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
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


def enforce_manual_state(schedule: dict[str, Any]) -> bool:
    """Apply the non-negotiable manual-only state to every approved candidate."""
    changed = False
    candidates = schedule.get("candidates", [])
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("vig_approved") is not True:
            continue
        required = {
            "execution_mode": "manual",
            "manual_bet_status": "awaiting_jerry",
            "executed": False,
        }
        for key, value in required.items():
            if candidate.get(key) != value:
                candidate[key] = value
                changed = True
        for key in ("execution_cron_id", "execution_cron_fire_utc", "approval_token"):
            if key in candidate:
                candidate.pop(key)
                changed = True
    return changed


def _schedule_path(sport: str, day: str) -> Path:
    if sport == "MLB":
        return ROOT / ".picks" / "execute" / f"{day}-schedule.json"
    return ROOT / ".picks" / "execute" / "intl-soccer" / f"{day}-schedule.json"


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
        schedule: dict[str, Any] = {"candidates": data}
    elif isinstance(data, dict):
        schedule = data
    else:
        print(f"{sport} review gate ERROR: expected object or list, got {type(data).__name__}")
        return 1
    try:
        candidates, watchlist = review_work(schedule, sport)
    except ScheduleFormatError as exc:
        print(f"{sport} review gate ERROR: {exc}")
        return 1
    if not candidates and not watchlist:
        return 0

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
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=1800)
    if proc.returncode:
        print(f"{sport} review failed (exit {proc.returncode}):\n{(proc.stderr or proc.stdout).strip()[:3000]}")
        return proc.returncode

    try:
        updated = json.loads(schedule_path.read_text(encoding="utf-8"))
        if isinstance(updated, dict) and enforce_manual_state(updated):
            schedule_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{sport} review gate ERROR: could not enforce manual-only state: {exc}")
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
