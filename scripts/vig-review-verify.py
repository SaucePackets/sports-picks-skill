#!/usr/bin/env python3
"""Read-only verification of an MLB Vig review handoff.

Usage:
    python scripts/vig-review-verify.py YYYY-MM-DD [options]

The verifier reads a dated schedule, Hermes cron storage, the canonical pick
ledger, and ``.picks/latest-action.md``. It never writes any of them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FINAL_RESULTS = {"win", "loss", "lost", "won", "void", "push", "cancelled", "canceled"}
INACTIVE_STATUSES = {"settled", "closed", "void", "cancelled", "canceled"}
DEFAULT_DELIVER = "telegram:-1003740149270:4"
DEFAULT_SKILLS = ["sports-betting-markets", "sports-data-apis"]
DEFAULT_PROVIDER = "deepseek-api"
DEFAULT_MODEL = "deepseek-v4-flash"


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


def same_instant(left: Any, right: Any) -> bool:
    left_dt = parse_instant(left)
    right_dt = parse_instant(right)
    return left_dt is not None and right_dt is not None and left_dt == right_dt


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


def load_jobs(path: Path, report: VerificationReport) -> dict[str, dict[str, Any]]:
    try:
        data = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        report.fail(f"Could not read Hermes cron jobs {path}: {exc}")
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        report.fail(f"Cron jobs file must contain a jobs list: {path}")
        return {}
    indexed = {str(job.get("id")): job for job in jobs if isinstance(job, dict) and job.get("id")}
    report.ok(f"Loaded {len(indexed)} Hermes cron jobs from {path}")
    return indexed


def expected(candidate: dict[str, Any], schedule: dict[str, Any], key: str, fallback: Any) -> Any:
    return candidate.get(key, schedule.get(key, fallback))


def verify_execution_jobs(
    approved: list[dict[str, Any]],
    schedule: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
    root: Path,
    report: VerificationReport,
) -> None:
    for index, candidate in enumerate(approved):
        label = describe_candidate(candidate, index)
        job_id = candidate.get("execution_cron_id")
        fire = candidate.get("execution_cron_fire_utc")
        if not isinstance(job_id, str) or not job_id.strip():
            report.fail(f"{label} is approved but has no execution_cron_id")
            continue
        if parse_instant(fire) is None:
            report.fail(f"{label} has invalid execution_cron_fire_utc={fire!r}")
            continue
        job = jobs.get(job_id)
        if job is None:
            report.fail(f"{label} references missing cron job {job_id}")
            continue

        failures: list[str] = []
        if job.get("enabled") is not True or job.get("state") != "scheduled":
            failures.append(f"not active (enabled={job.get('enabled')!r}, state={job.get('state')!r})")
        raw_schedule = job.get("schedule")
        job_schedule: dict[str, Any] = raw_schedule if isinstance(raw_schedule, dict) else {}
        if job_schedule.get("kind") != "once":
            failures.append(f"schedule kind is {job_schedule.get('kind')!r}, not 'once'")
        if not same_instant(job_schedule.get("run_at"), fire):
            failures.append(f"run_at {job_schedule.get('run_at')!r} != {fire!r}")
        if job.get("next_run_at") is not None and not same_instant(job.get("next_run_at"), fire):
            failures.append(f"next_run_at {job.get('next_run_at')!r} != {fire!r}")
        raw_repeat = job.get("repeat")
        repeat: dict[str, Any] = raw_repeat if isinstance(raw_repeat, dict) else {}
        if repeat.get("times") != 1 or repeat.get("completed") != 0:
            failures.append(f"Repeat is {repeat.get('completed')!r}/{repeat.get('times')!r}, expected 0/1")

        checks = {
            "deliver": expected(candidate, schedule, "execution_deliver", DEFAULT_DELIVER),
            "skills": expected(candidate, schedule, "execution_skills", DEFAULT_SKILLS),
            "workdir": str(expected(candidate, schedule, "execution_workdir", root)),
            "provider": expected(candidate, schedule, "execution_provider", DEFAULT_PROVIDER),
            "model": expected(candidate, schedule, "execution_model", DEFAULT_MODEL),
        }
        for field, wanted in checks.items():
            actual = job.get(field)
            if field == "skills":
                actual = actual if isinstance(actual, list) else []
            if actual != wanted:
                failures.append(f"{field}={actual!r}, expected {wanted!r}")

        if failures:
            for failure in failures:
                report.fail(f"Cron {job_id} for {label}: {failure}")
        else:
            report.ok(f"Cron {job_id} is an active matching one-shot with Repeat 0/1 and pinned runtime fields")


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
    for candidate in approved:
        job_id = candidate.get("execution_cron_id")
        if isinstance(job_id, str) and job_id not in text:
            failures.append(f"one-shot id {job_id}")
    if failures:
        report.fail(f"latest-action.md does not match review state ({', '.join(failures)}): {path}")
    else:
        report.ok(f"latest-action.md matches approval counts, one-shots, and exposure: {path}")


def default_cron_jobs_file(profile: str) -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    profile_path = hermes_home / "profiles" / profile / "cron" / "jobs.json"
    direct_path = hermes_home / "cron" / "jobs.json"
    return profile_path if profile_path.is_file() else direct_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a read-only MLB Vig review handoff.")
    parser.add_argument("date", help="MLB schedule date (YYYY-MM-DD)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="sports-picks runtime root (default: cwd)")
    parser.add_argument("--profile", default="vig", help="Hermes profile owning execution jobs (default: vig)")
    parser.add_argument("--cron-jobs-file", type=Path, help="Hermes jobs.json override")
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

    cron_path = (args.cron_jobs_file or default_cron_jobs_file(args.profile)).expanduser().resolve()
    jobs = load_jobs(cron_path, report)
    verify_execution_jobs(approved, schedule, jobs, root, report)

    picks_path = resolve_picks_file(root, args.picks_file).expanduser().resolve()
    verify_picks(picks_path, report)

    latest_path = (args.latest_action_file or root / ".picks" / "latest-action.md").expanduser().resolve()
    verify_latest_action(latest_path, args.date, len(approved), flagged, approved, exposure, cap, report)

    report.print()
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
