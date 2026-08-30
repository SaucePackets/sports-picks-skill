#!/usr/bin/env python3
"""Durable, dated record of every scheduled Vig gate run.

The review gate previously left a trace on exactly one path: a completed
review overwrote ``.picks/latest-action.md``. Every other outcome — no
schedule file for the day, a schedule with no reviewable work, an invalid
review transition, a child reviewer that timed out — printed to stdout and
returned, so once cron's delivery scrolled away there was nothing on disk
saying the job had run at all. That is why 2026-08-12/13/14/19/20 are
indistinguishable from days the cron never fired: a PASS and a silent
failure look identical afterwards.

This module is the append-only side of that. One JSONL file per Chicago
schedule day under ``.picks/journal/``, one object per gate invocation,
carrying the outcome, the stage it stopped at, the counts, the notices the
gate printed, and every input the gate deferred or skipped with the source
that reported it and the instant it was observed.

Two properties are deliberate:

* **It never raises and never changes a verdict.** ``record_run`` returns an
  error string instead of propagating. Observability that can fail a review
  is a new outage mode in a lane whose whole problem is losing work, so the
  gate's own decision stays authoritative and a journal failure is reported
  loudly rather than escalated.
* **It is append-only.** A run never rewrites a previous run's record, so a
  later cycle of the same day cannot erase the earlier one's evidence, which
  is precisely how ``latest-action.md`` lost it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

JOURNAL_SCHEMA = "vig-run-journal-v1"

# Outcomes. Every gate exit path maps onto exactly one of these.
OUTCOME_NO_SCHEDULE = "no_schedule"   # nothing was collected for the day
OUTCOME_NO_WORK = "no_work"           # a schedule exists, nothing to review
OUTCOME_REVIEWED = "reviewed"         # a review completed and was persisted
OUTCOME_ERROR = "error"               # the gate refused or failed closed
OUTCOMES = (OUTCOME_NO_SCHEDULE, OUTCOME_NO_WORK, OUTCOME_REVIEWED, OUTCOME_ERROR)

# A run that produced no card is a real, reportable outcome — not an absence.
# Both of these are PASS days as far as reporting is concerned.
PASS_OUTCOMES = (OUTCOME_NO_SCHEDULE, OUTCOME_NO_WORK)

# Sources that can report an input as deferred or skipped. The source travels
# with the deferral because "id X was not reviewed" is unactionable without
# knowing which feed went quiet.
SOURCE_LINEUP_FEED = "lineup_feed"
SOURCE_PRICE_FEED = "price_feed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def journal_path(root: Path, day: str) -> Path:
    """One file per schedule day, shared by every sport's gate.

    Keyed on the day rather than the sport so "did anything run on 08-19?"
    is a single stat() instead of a search across per-sport names.
    """
    return Path(root) / ".picks" / "journal" / f"{day}-runs.jsonl"


def deferral(entry_id: Any, source: str, reason: str, observed_at: str | None = None) -> dict[str, Any]:
    """One deferred/skipped input, with the source and instant that saw it."""
    return {
        "id": str(entry_id),
        "source": source,
        "reason": reason,
        "observed_at": observed_at or utc_now_iso(),
    }


def build_record(
    *,
    sport: str,
    day: str,
    outcome: str,
    stage: str,
    detail: str = "",
    schedule_path: Any = None,
    counts: dict[str, int] | None = None,
    notices: Iterable[str] = (),
    deferrals: Iterable[dict[str, Any]] = (),
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build one journal record. Pure: no clock unless ``recorded_at`` is None.

    ``stage`` names where the run stopped (``schedule_missing``,
    ``review_transition``, ``persist``, ``complete``, ...). Outcome says what
    happened; stage says where, and the pair is what makes a failure
    diagnosable from the artifact alone.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    return {
        "schema": JOURNAL_SCHEMA,
        "recorded_at": recorded_at or utc_now_iso(),
        "sport": sport,
        "day": day,
        "outcome": outcome,
        "stage": stage,
        "detail": detail,
        "schedule_path": str(schedule_path) if schedule_path is not None else "",
        "counts": dict(counts or {}),
        "notices": list(notices),
        "deferrals": list(deferrals),
    }


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Append one record under an exclusive lock, durably.

    Raises OSError on any failure; ``record_run`` is the non-raising wrapper
    callers in the gate use.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_run(root: Path, record: dict[str, Any]) -> str | None:
    """Persist one record. Returns an error description, or None on success.

    Never raises: a journal that can abort a review would convert a logging
    problem into a missed card, which is the failure this module exists to
    make visible.
    """
    path = journal_path(root, str(record.get("day", "")))
    try:
        append_record(path, record)
    except (OSError, TypeError, ValueError) as exc:
        return f"could not write run journal {path}: {exc}"
    return None


def read_records(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read a day's journal. Returns ``(records, problems)``.

    A corrupt line is reported as a problem and skipped rather than raising,
    so one bad append can never hide the rest of the day's evidence.
    """
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"unreadable journal {path}: {exc}"]
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}:{number}: corrupt record: {exc}")
            continue
        if not isinstance(parsed, dict):
            problems.append(f"{path.name}:{number}: record must be an object")
            continue
        records.append(parsed)
    return records, problems


def day_range(since: str, until: str) -> list[str]:
    start = date.fromisoformat(since)
    end = date.fromisoformat(until)
    if end < start:
        raise ValueError(f"--until {until} precedes --since {since}")
    return [(start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)]


def unjournalled_days(root: Path, days: Iterable[str], sport: str | None = None) -> list[str]:
    """Days in the range with no gate record at all.

    This is the drift signal the lane never had: a day whose cron did not
    fire, or fired into a crash before the journal, is now a named gap
    instead of an absence indistinguishable from a quiet PASS.
    """
    missing: list[str] = []
    for day in days:
        records, _ = read_records(journal_path(root, day))
        if sport:
            records = [r for r in records if str(r.get("sport", "")).upper() == sport.upper()]
        if not records:
            missing.append(day)
    return missing


def format_record(record: dict[str, Any]) -> str:
    counts = record.get("counts") or {}
    count_text = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    parts = [
        f"[{record.get('recorded_at', '?')}] {record.get('sport', '?')} "
        f"{record.get('outcome', '?')} at {record.get('stage', '?')}"
    ]
    if count_text:
        parts.append(f"  counts: {count_text}")
    if record.get("detail"):
        parts.append(f"  detail: {record['detail']}")
    for notice in record.get("notices") or []:
        parts.append(f"  notice: {notice}")
    for item in record.get("deferrals") or []:
        parts.append(
            f"  deferred: {item.get('id', '?')} via {item.get('source', '?')} "
            f"at {item.get('observed_at', '?')} — {item.get('reason', '')}"
        )
    return "\n".join(parts)


def _default_root() -> Path:
    # Imported lazily: vig_review_gate_common imports this module, so a
    # module-level import in the other direction would be circular.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from vig_review_gate_common import resolve_root  # noqa: E402

    return resolve_root()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report or audit the Vig gate run journal.")
    parser.add_argument("--root", type=Path, default=None,
                        help="sports-picks runtime root (default: resolved like the gate)")
    parser.add_argument("--day", help="report a single schedule day")
    parser.add_argument("--since", help="first day of a coverage audit (YYYY-MM-DD)")
    parser.add_argument("--until", help="last day of a coverage audit (YYYY-MM-DD)")
    parser.add_argument("--sport", help="restrict the coverage audit to one sport")
    args = parser.parse_args(argv)

    root = args.root or _default_root()
    if not args.day and not (args.since and args.until):
        parser.error("pass --day, or both --since and --until")

    status = 0
    if args.day:
        path = journal_path(root, args.day)
        records, problems = read_records(path)
        for problem in problems:
            print(f"JOURNAL PROBLEM: {problem}")
            status = 1
        if not records:
            print(f"NO RUNS JOURNALLED for {args.day} ({path})")
            status = 1
        for record in records:
            print(format_record(record))

    if args.since and args.until:
        try:
            days = day_range(args.since, args.until)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        missing = unjournalled_days(root, days, args.sport)
        label = f" for {args.sport}" if args.sport else ""
        if missing:
            print(f"COVERAGE GAP{label}: {len(missing)} of {len(days)} day(s) have no gate record: "
                  + ", ".join(missing))
            status = 1
        else:
            print(f"COVERAGE OK{label}: all {len(days)} day(s) have at least one gate record")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
