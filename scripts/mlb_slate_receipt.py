#!/usr/bin/env python3
"""A deterministic receipt for whether a slate run recorded what it decided.

The 2026-09-01 run is the reason this exists. The daily slate cron reported
``Last run: ok`` and ``Execution: failed`` in the same breath, the slate prose
carried a full read on fifteen games, ``latest-action.md`` said "Slate
complete", and the schedule it wrote had no ``game_reads`` and no
``slate_denominator``. Every signal that could have said "this run did not
record its refusals" either said the opposite or belonged to a system outside
this repository.

Both cron fields are Hermes', not ours: nothing here writes or can write them,
so the discrepancy between them cannot be fixed on this side of the boundary.
What can be fixed is the dependence on them. This module writes ONE artifact,
from repository code, on a closed vocabulary, next to the run journal:

- ``complete``        — every scheduled game has a read, and the record validates.
- ``honest_zero``     — the day genuinely had no games to record.
- ``recorder_failed`` — games were scheduled and the record does not cover them.
- ``no_schedule``     — no schedule file for the day at all.

The distinction ``honest_zero`` / ``recorder_failed`` is the whole point. A
zero row count is only honest if the denominator says zero; a zero row count
against fifteen scheduled games is a failure wearing the same number. That is
why the denominator is read from the scan artifact rather than from the
schedule's own copy of it — a run that trimmed its roster to match a short
read set would otherwise certify itself ``complete``.

Read-only with respect to everything except its own receipt file. No network,
no order behaviour, no gate or policy input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import mlb_runtime_policy  # noqa: E402
from mlb_game_reads import (  # noqa: E402
    check_denominator,
    policy_disposition_errors,
    validate_game_reads,
)

RECEIPT_SCHEMA = "vig-mlb-slate-receipt-v1"

VERDICT_COMPLETE = "complete"
VERDICT_HONEST_ZERO = "honest_zero"
VERDICT_RECORDER_FAILED = "recorder_failed"
VERDICT_NO_SCHEDULE = "no_schedule"
# Closed and enumerated. A run that fits none of these is a bug in this
# module, not a fifth informal state written into `detail`.
VERDICTS = (
    VERDICT_COMPLETE,
    VERDICT_HONEST_ZERO,
    VERDICT_RECORDER_FAILED,
    VERDICT_NO_SCHEDULE,
)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def schedule_path_for(root: Path, day: str) -> Path:
    return root / ".picks" / "execute" / f"{day}-schedule.json"


def receipt_path_for(root: Path, day: str) -> Path:
    return root / ".picks" / "journal" / f"{day}-slate-receipt.json"


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _scan_game_count(scan_rows: Any) -> int | None:
    """How many games the scan enumerated, or None if it cannot be read.

    None is not zero. A scan we could not parse tells us nothing about the
    size of the day, and treating it as an empty slate is precisely how a
    recorder failure would be laundered into an honest zero.
    """
    if not isinstance(scan_rows, list):
        return None
    return len(scan_rows)


def build_receipt(root: Path, day: str) -> dict[str, Any]:
    """Compute the receipt for one day. Pure with respect to the filesystem read."""
    schedule_path = schedule_path_for(root, day)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "sport": "MLB",
        "day": day,
        "recorded_at_utc": utc_now_iso(),
        "schedule_path": str(schedule_path),
        "schedule_sha256": None,
        "scheduled_games": None,
        "reads_recorded": 0,
        "recorder_errors": [],
        "verdict": VERDICT_NO_SCHEDULE,
        "detail": "no schedule file for this day",
    }
    if not schedule_path.exists():
        return receipt
    receipt["schedule_sha256"] = _sha256(schedule_path)
    try:
        schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        receipt["verdict"] = VERDICT_RECORDER_FAILED
        receipt["recorder_errors"] = [f"schedule unreadable: {exc}"]
        receipt["detail"] = "the schedule exists and cannot be parsed"
        return receipt

    reads = schedule.get("game_reads") if isinstance(schedule, dict) else None
    receipt["reads_recorded"] = len(reads) if isinstance(reads, list) else 0

    errors = list(validate_game_reads(schedule))
    # The receipt asks the same question the gate asks, with the same rails: a
    # receipt that called a slate `complete` while the gate reported a defect
    # would be a second opinion about what a valid record is, which is what the
    # shared validator exists to prevent.
    errors.extend(
        policy_disposition_errors(schedule, mlb_runtime_policy.load_mlb_selection_policy())
    )

    # Against the SCAN, not against the schedule's own copy of it.
    # validate_game_reads checks the reads against `slate_denominator`, which
    # the same run wrote: a run that trimmed both together is perfectly
    # self-consistent and would certify itself complete. This is the only
    # check here with an independent source — and it is the SAME function the
    # scheduled gate now calls, so the receipt cannot drift stricter or looser
    # than the rail.
    check = check_denominator(schedule_path, schedule)
    receipt["denominator_path"] = str(check.path) if check.path is not None else None
    errors.extend(check.errors)
    receipt["scheduled_games"] = _scan_game_count(check.rows)
    receipt["recorder_errors"] = errors

    scheduled = receipt["scheduled_games"]
    if scheduled == 0 and not errors:
        receipt["verdict"] = VERDICT_HONEST_ZERO
        receipt["detail"] = "the scan enumerated no games; zero reads is the correct record"
    elif errors:
        receipt["verdict"] = VERDICT_RECORDER_FAILED
        receipt["detail"] = (
            f"{len(errors)} defect(s) in the per-game record"
            + (
                f"; {scheduled} game(s) scheduled against {receipt['reads_recorded']} read(s)"
                if isinstance(scheduled, int)
                else "; the day's size could not be established"
            )
        )
    else:
        receipt["verdict"] = VERDICT_COMPLETE
        receipt["detail"] = (
            f"{receipt['reads_recorded']} read(s) covering {scheduled} scheduled game(s)"
        )
    return receipt


def write_receipt(root: Path, receipt: dict[str, Any]) -> Path:
    path = receipt_path_for(root, receipt["day"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _default_root() -> Path:
    import os

    override = os.environ.get("SPORTS_PICKS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path.cwd().resolve()
    if (current / ".picks").is_dir():
        return current
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", default=None, help="slate date YYYY-MM-DD (default: today)")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--write",
        action="store_true",
        help="persist the receipt under .picks/journal/ (default: report only)",
    )
    args = parser.parse_args(argv)
    root = (args.root or _default_root()).resolve()
    day = args.day or dt.date.today().isoformat()
    receipt = build_receipt(root, day)
    if args.write:
        receipt["receipt_path"] = str(write_receipt(root, receipt))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    # Exit 1 on a recorder failure so a caller that checks status sees it, and
    # 0 for an honest zero: those two must never share an exit code, which is
    # the entire complaint against the run that prompted this module.
    return 1 if receipt["verdict"] == VERDICT_RECORDER_FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
