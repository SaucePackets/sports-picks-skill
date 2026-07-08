#!/usr/bin/env python3
"""Verify Vig second-review/runtime handoff state without mutating betting files.

Usage:
    python scripts/vig-review-verify.py YYYY-MM-DD

Checks performed:
- schedule JSON files for the date are valid and reviewer-gated candidates have
  explicit boolean vig_approved values
- one-shot/cron ids recorded in schedule JSON still appear in `hermes cron list --all`
- latest-action.md exists (when present in the ledger) and is readable/non-empty
- picks.json is valid JSON and has no duplicate active entries by market_slug

The verifier is intentionally read-only: it never writes schedule, picks, or cron
state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CRON_ID_KEY_RE = re.compile(r"(?:^|_)(?:cron|job)(?:_job)?_?id$|cron.*id$", re.IGNORECASE)
ACTIVE_STATUSES = {"open", "active", "pending", "watch", "watching", "executed", "live"}
SETTLED_STATUSES = {"settled", "closed", "void", "cancelled", "canceled", "lost", "won"}


@dataclass
class Check:
    level: str
    message: str


class VerificationReport:
    def __init__(self) -> None:
        self.checks: list[Check] = []

    def ok(self, message: str) -> None:
        self.checks.append(Check("OK", message))

    def warn(self, message: str) -> None:
        self.checks.append(Check("WARN", message))

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


def find_schedule_paths(root: Path, date: str) -> list[Path]:
    execute_root = root / ".picks" / "execute"
    if not execute_root.exists():
        return []
    return sorted(execute_root.glob(f"**/{date}-schedule.json"))


def candidates_from_schedule(schedule: Any) -> list[dict[str, Any]]:
    if isinstance(schedule, dict) and isinstance(schedule.get("candidates"), list):
        return [item for item in schedule["candidates"] if isinstance(item, dict)]
    if isinstance(schedule, list):
        return [item for item in schedule if isinstance(item, dict)]
    return []


def is_review_gated(candidate: dict[str, Any]) -> bool:
    if "vig_review_needed" in candidate:
        return bool(candidate.get("vig_review_needed"))
    return "vig_approved" in candidate


def describe_candidate(candidate: dict[str, Any], index: int) -> str:
    for key in ("polymarket_slug", "market_slug", "slug", "game", "matchup", "pick"):
        value = candidate.get(key)
        if value:
            return f"candidate[{index}] {key}={value}"
    return f"candidate[{index}]"


def collect_cron_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if CRON_ID_KEY_RE.search(str(key)):
                if isinstance(child, str) and child.strip():
                    ids.add(child.strip())
                elif isinstance(child, (int, float)):
                    ids.add(str(child))
            ids.update(collect_cron_ids(child))
    elif isinstance(value, list):
        for child in value:
            ids.update(collect_cron_ids(child))
    return ids


def get_cron_listing(cron_list_file: Path | None) -> str:
    if cron_list_file is not None:
        return cron_list_file.read_text(encoding="utf-8")

    env = os.environ.copy()
    env.setdefault("HERMES_ACCEPT_HOOKS", "1")
    proc = subprocess.run(
        ["hermes", "cron", "list", "--all"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "hermes cron list failed").strip())
    return proc.stdout


def verify_schedules(root: Path, date: str, report: VerificationReport) -> set[str]:
    paths = find_schedule_paths(root, date)
    if not paths:
        report.fail(f"No schedule JSON found under {root / '.picks' / 'execute'} for {date}")
        return set()

    cron_ids: set[str] = set()
    for path in paths:
        try:
            schedule = load_json(path)
        except json.JSONDecodeError as exc:
            report.fail(f"Invalid schedule JSON: {path} ({exc})")
            continue

        candidates = candidates_from_schedule(schedule)
        report.ok(f"Valid schedule JSON: {path} ({len(candidates)} candidates)")
        cron_ids.update(collect_cron_ids(schedule))

        for index, candidate in enumerate(candidates):
            label = describe_candidate(candidate, index)
            if is_review_gated(candidate):
                approved = candidate.get("vig_approved")
                if isinstance(approved, bool):
                    report.ok(f"{label} has vig_approved={approved}")
                else:
                    report.fail(f"{label} needs boolean vig_approved before execution; found {approved!r}")
            else:
                report.ok(f"{label} is not Vig-review gated")
    return cron_ids


def verify_cron_ids(cron_ids: Iterable[str], cron_list_file: Path | None, report: VerificationReport) -> None:
    ids = sorted(set(cron_ids))
    if not ids:
        report.warn("No one-shot cron ids found in schedule JSON")
        return

    try:
        listing = get_cron_listing(cron_list_file)
    except Exception as exc:  # noqa: BLE001 - CLI failure should become verifier failure
        report.fail(f"Could not read Hermes cron list: {exc}")
        return

    for cron_id in ids:
        if cron_id in listing:
            report.ok(f"One-shot cron id present in Hermes cron list: {cron_id}")
        else:
            report.fail(f"One-shot cron id missing from Hermes cron list: {cron_id}")


def latest_action_candidates(root: Path) -> list[Path]:
    return [root / ".picks" / "latest-action.md", root / "latest-action.md"]


def verify_latest_action(root: Path, date: str, report: VerificationReport) -> None:
    existing = [path for path in latest_action_candidates(root) if path.exists()]
    if not existing:
        report.warn("latest-action.md not found at .picks/latest-action.md or repo root")
        return

    path = existing[0]
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        report.fail(f"latest-action.md is empty: {path}")
        return
    if date in text:
        report.ok(f"latest-action.md references {date}: {path}")
    else:
        report.warn(f"latest-action.md is readable but does not mention {date}: {path}")


def pick_is_active(pick: dict[str, Any]) -> bool:
    status = str(pick.get("status", "open")).strip().lower()
    if status in SETTLED_STATUSES:
        return False
    if status in ACTIVE_STATUSES:
        return True
    return True


def verify_picks(root: Path, report: VerificationReport) -> None:
    candidates = [root / ".picks" / "picks.json", root / "picks.json"]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        report.warn("picks.json not found at .picks/picks.json or repo root")
        return

    path = existing[0]
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        report.fail(f"Invalid picks JSON: {path} ({exc})")
        return

    if isinstance(data, dict):
        picks = data.get("picks", [])
    else:
        picks = data
    if not isinstance(picks, list):
        report.fail(f"picks.json must contain a list or an object with a picks list: {path}")
        return

    active_slugs = [
        str(pick.get("market_slug")).strip()
        for pick in picks
        if isinstance(pick, dict) and pick.get("market_slug") and pick_is_active(pick)
    ]
    duplicates = sorted(slug for slug, count in Counter(active_slugs).items() if count > 1)
    if duplicates:
        report.fail(f"Duplicate active picks by market_slug in {path}: {', '.join(duplicates)}")
    else:
        report.ok(f"picks.json valid with no duplicate active market_slug entries: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify Vig review handoff state without modifying betting files.")
    parser.add_argument("date", help="Slate date to verify, format YYYY-MM-DD")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="sports-picks runtime root (default: cwd)")
    parser.add_argument("--cron-list-file", type=Path, help="Use saved hermes cron list output instead of running hermes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not DATE_RE.match(args.date):
        print("date must use YYYY-MM-DD", file=sys.stderr)
        return 2

    root = args.root.resolve()
    report = VerificationReport()
    cron_ids = verify_schedules(root, args.date, report)
    verify_cron_ids(cron_ids, args.cron_list_file, report)
    verify_latest_action(root, args.date, report)
    verify_picks(root, report)
    report.print()
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
