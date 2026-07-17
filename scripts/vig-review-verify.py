#!/usr/bin/env python3
"""Read-only verification of an MLB Vig review handoff.

Usage:
    python scripts/vig-review-verify.py YYYY-MM-DD [options]

The verifier reads a dated schedule, the canonical pick ledger, and
``.picks/latest-action.md``. It never writes any of them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FINAL_RESULTS = {"win", "loss", "lost", "won", "void", "push", "cancelled", "canceled"}
INACTIVE_STATUSES = {"settled", "closed", "void", "cancelled", "canceled"}


@dataclass
class Check:
    level: str
    message: str


class VerificationReport:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def ok(self, message: str) -> None:
        self.checks.append(Check("OK", message))

    def fail(self, message: str) -> None:
        self.checks.append(Check("FAIL", message))

    @property
    def failed(self) -> bool:
        return any(check.level == "FAIL" for check in self.checks)

    def print(self) -> None:
        for check in self.checks:
            print(f"[{check.level}] {check.message}")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def money(value: Decimal) -> str:
    return format(value.normalize(), "f")


def describe_candidate(candidate: dict[str, Any], index: int) -> str:
    slug = candidate.get("polymarket_slug") or candidate.get("market_slug")
    side = candidate.get("side")
    details = " / ".join(str(value) for value in (slug, side) if value)
    return f"candidate[{index}] {details}" if details else f"candidate[{index}]"


def read_schedule(root: Path, date: str, report: VerificationReport) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = root / ".picks" / "execute" / f"{date}-schedule.json"
    if not path.is_file():
        report.fail(f"Schedule not found: {path}")
        return {}, []
    try:
        schedule = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        report.fail(f"Could not read schedule JSON {path}: {exc}")
        return {}, []
    if not isinstance(schedule, dict) or not isinstance(schedule.get("candidates"), list):
        report.fail(f"Schedule must be an object with a candidates list: {path}")
        return {}, []
    if schedule.get("date") != date:
        report.fail(f"Schedule date is {schedule.get('date')!r}, expected {date}")
    candidates = [item for item in schedule["candidates"] if isinstance(item, dict)]
    if len(candidates) != len(schedule["candidates"]):
        report.fail("Every schedule candidate must be a JSON object")
    report.ok(f"Loaded {len(candidates)} MLB candidates from {path}")
    return schedule, candidates


def verify_reviews(candidates: list[dict[str, Any]], report: VerificationReport) -> tuple[list[dict[str, Any]], int]:
    approved: list[dict[str, Any]] = []
    flagged = 0
    for index, candidate in enumerate(candidates):
        label = describe_candidate(candidate, index)
        decision = candidate.get("vig_approved")
        notes = candidate.get("vig_notes")
        if not isinstance(decision, bool):
            report.fail(f"{label} has non-boolean vig_approved={decision!r}")
            continue
        if not isinstance(notes, str) or not notes.strip():
            report.fail(f"{label} has empty vig_notes")
        else:
            report.ok(f"{label} has a decision and non-empty Vig notes")
        if decision:
            approved.append(candidate)
        else:
            flagged += 1
    return approved, flagged


def verify_manual_approvals(approved: list[dict[str, Any]], report: VerificationReport) -> None:
    """Require approved rows to be reminders, never executable instructions."""
    forbidden = ("execution_cron_id", "execution_cron_fire_utc", "approval_token")
    for index, candidate in enumerate(approved):
        label = describe_candidate(candidate, index)
        failures: list[str] = []
        if candidate.get("execution_mode") != "manual":
            failures.append("execution_mode must be 'manual'")
        if candidate.get("manual_bet_status") != "awaiting_jerry":
            failures.append("manual_bet_status must be 'awaiting_jerry'")
        if candidate.get("executed") is not False:
            failures.append("executed must be false")
        present = [field for field in forbidden if field in candidate]
        if present:
            failures.append(f"forbidden execution fields present: {', '.join(present)}")
        if failures:
            for failure in failures:
                report.fail(f"{label}: {failure}")
        else:
            report.ok(f"{label} is a manual-only awaiting_jerry reminder")


def resolve_picks_file(root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env_path = os.environ.get("SPORTS_PICKS_LEDGER")
    candidates = [
        Path(env_path).expanduser() if env_path else None,
        root / ".picks" / "picks.json",
        root / "picks.json",
        Path.home() / "notes" / "Sports" / "picks" / "picks.json",
    ]
    return next((path for path in candidates if path is not None and path.is_file()), root / ".picks" / "picks.json")


def pick_is_active(pick: dict[str, Any]) -> bool:
    result = str(pick.get("result") or "").strip().lower()
    if result in FINAL_RESULTS or pick.get("settled_at"):
        return False
    status = str(pick.get("status") or "active").strip().lower()
    return status not in INACTIVE_STATUSES


def verify_picks(path: Path, report: VerificationReport) -> None:
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        report.fail(f"Could not read picks JSON {path}: {exc}")
        return
    picks = data.get("picks") if isinstance(data, dict) else data
    if not isinstance(picks, list):
        report.fail(f"picks.json must be a list or an object with a picks list: {path}")
        return
    keys: list[tuple[str, str]] = []
    for pick in picks:
        if not isinstance(pick, dict) or not pick_is_active(pick):
            continue
        slug = str(pick.get("market_slug") or pick.get("polymarket_slug") or "").strip()
        side = str(pick.get("side") or "").strip().casefold()
        if slug and side:
            keys.append((slug, side))
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        rendered = ", ".join(f"{slug} + {side}" for slug, side in duplicates)
        report.fail(f"Duplicate active picks by slug+side in {path}: {rendered}")
    else:
        report.ok(f"No duplicate active slug+side pairs in {path}")


def verify_exposure(schedule: dict[str, Any], approved: list[dict[str, Any]], report: VerificationReport) -> tuple[Decimal, Decimal | None]:
    amounts: list[Decimal] = []
    for candidate in approved:
        amount = decimal(candidate.get("unit_size"))
        if amount is None or amount < 0:
            report.fail(f"Approved candidate has invalid unit_size={candidate.get('unit_size')!r}")
        else:
            amounts.append(amount)
    calculated = sum(amounts, Decimal("0"))
    recorded = decimal(schedule.get("approved_exposure"))
    cap = decimal(schedule.get("daily_cap"))
    if recorded != calculated:
        report.fail(f"Schedule approved_exposure={schedule.get('approved_exposure')!r}, calculated {money(calculated)}")
    else:
        report.ok(f"Schedule approved exposure matches approved unit sizes: ${money(calculated)}")
    if cap is None or cap < calculated:
        report.fail(f"Schedule daily_cap={schedule.get('daily_cap')!r} is missing, invalid, or below approved exposure")
    return calculated, cap


def verify_latest_action(
    path: Path,
    date: str,
    approved_count: int,
    flagged_count: int,
    approved: list[dict[str, Any]],
    exposure: Decimal,
    cap: Decimal | None,
    report: VerificationReport,
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.fail(f"Could not read latest-action.md {path}: {exc}")
        return
    failures: list[str] = []
    required_patterns = [
        (rf"\b{re.escape(date)}\b", f"date {date}"),
        (rf"\b{approved_count}\s+approved\b", f"{approved_count} approved"),
        (rf"\b{flagged_count}\s+(?:flagged|rejected)\b", f"{flagged_count} flagged/rejected"),
        (rf"\$?{re.escape(money(exposure))}\s*/\s*\$?{re.escape(money(cap))}\b" if cap is not None else r"(?!)", "approved exposure / daily cap"),
    ]
    for pattern, description in required_patterns:
        if not re.search(pattern, text, re.IGNORECASE):
            failures.append(description)
    if failures:
        report.fail(f"latest-action.md does not match review state ({', '.join(failures)}): {path}")
    else:
        report.ok(f"latest-action.md matches approval counts and exposure: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a read-only MLB Vig review handoff.")
    parser.add_argument("date", help="MLB schedule date (YYYY-MM-DD)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="sports-picks runtime root (default: cwd)")
    parser.add_argument("--picks-file", type=Path, help="canonical picks.json override")
    parser.add_argument("--latest-action-file", type=Path, help="latest-action.md override")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not DATE_RE.fullmatch(args.date):
        print("date must use YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print("date must be a real calendar date", file=sys.stderr)
        return 2

    root = args.root.resolve()
    report = VerificationReport()
    schedule, candidates = read_schedule(root, args.date, report)
    approved, flagged = verify_reviews(candidates, report)
    exposure, cap = verify_exposure(schedule, approved, report)

    verify_manual_approvals(approved, report)

    picks_path = resolve_picks_file(root, args.picks_file).expanduser().resolve()
    verify_picks(picks_path, report)

    latest_path = (args.latest_action_file or root / ".picks" / "latest-action.md").expanduser().resolve()
    verify_latest_action(latest_path, args.date, len(approved), flagged, approved, exposure, cap, report)

    report.print()
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
