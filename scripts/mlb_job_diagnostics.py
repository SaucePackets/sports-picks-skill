#!/usr/bin/env python3
"""Read-only snapshot diagnostics; never run a job, render a task, or authorize work.

An execution row proves a scheduler run, not an order or even agent dispatch.
The supplied script cwd is an observation/hypothesis, not inferred from jobs.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from mlb_execution_gate import resolve_root
from mlb_runtime_policy import MARKET_MODEL_VERSION


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include an offset")
    return parsed.astimezone(timezone.utc)


def execution_window(path: Path, job_id: str, since: str, until: str) -> dict:
    """Query the named store only, converting offsets before filtering.

    Missing stores are errors, never created as empty SQLite databases. A
    completed run does not establish which schedule or script bytes it read.
    """
    start, end = instant(since), instant(until)
    if start >= end:
        raise ValueError("since must precede until")
    rows = []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute(
            "SELECT id, job_id, status, claimed_at, started_at, finished_at "
            "FROM executions WHERE job_id = ?", (job_id,),
        ):
            item = dict(row)
            # Keep the measurement basis explicit: a claim is not a start.
            stamp = item["started_at"] or item["claimed_at"]
            if start <= instant(stamp) < end:
                item["window_basis"] = "started_at" if item["started_at"] else "claimed_at"
                rows.append(item)
    rows.sort(key=lambda r: (instant(r[r["window_basis"]]), r["id"]))
    return {"store": str(path.resolve()), "job_id": job_id, "since": since,
            "until_exclusive": until, "runs": rows,
            "agent_dispatch": "unknown", "order_outcome": "unknown"}


def market_identity(candidate: dict) -> dict:
    """Diagnose the documented market-only identity, without changing a gate."""
    if candidate.get("model_version") != MARKET_MODEL_VERSION:
        return {"status": "not_applicable", "errors": []}
    errors = []
    fields = ("dk_fair_prob", "raw_probability", "conservative_probability",
              "uncertainty_haircut")
    valid = {}
    for field in fields:
        value = candidate.get(field)
        valid[field] = (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and 0 <= value <= 1 and math.isfinite(value))
        if not valid[field]:
            errors.append(f"{field}: missing or invalid probability")
    for field in ("raw_probability", "conservative_probability"):
        if valid[field] and valid["dk_fair_prob"]:
            if abs(candidate[field] - candidate["dk_fair_prob"]) > 1e-9:
                errors.append(f"{field}: differs from dk_fair_prob")
    if valid["uncertainty_haircut"] and candidate["uncertainty_haircut"] != 0:
        errors.append("uncertainty_haircut: must be zero")
    components = candidate.get("probability_components")
    for field in ("adjustments", "haircuts"):
        if not isinstance(components, dict) or components.get(field) != []:
            errors.append(f"probability_components.{field}: must be an empty list")
    return {"status": "contradicted" if errors else "consistent", "errors": errors}


def root_snapshot(runtime: Path, script_cwd: Path, home: Path, day: str) -> dict:
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("day must be canonical YYYY-MM-DD")
    resolved = resolve_root(cwd=script_cwd, home=home)
    result = {
        "evidence_kind": "current_snapshot_not_historical_execution",
        "runtime_root": str(runtime.resolve()), "supplied_script_cwd": str(script_cwd.resolve()),
        "root_override": os.environ.get("SPORTS_PICKS_ROOT"),
        "resolved_gate_root": str(resolved), "root_matches_runtime": resolved == runtime.resolve(),
        "schedules": [],
    }
    for label, root in (("runtime", runtime), ("resolved_gate", resolved)):
        path = root / ".picks" / "execute" / f"{day}-schedule.json"
        item = {"role": label, "path": str(path.resolve()), "exists": path.exists()}
        if item["exists"]:
            schedule = json.loads(path.read_text())
            if not isinstance(schedule, dict) or not isinstance(schedule.get("candidates"), list):
                raise ValueError(f"invalid schedule structure: {path}")
            candidates = schedule["candidates"]
            if any(not isinstance(c, dict) for c in candidates):
                raise ValueError(f"invalid candidate structure: {path}")
            item["candidate_count"] = len(candidates)
            item["approved_count"] = sum(c.get("vig_approved") is True for c in candidates)
            item["market_identity"] = [
                {"candidate_index": index, **market_identity(c)}
                for index, c in enumerate(candidates)
            ]
        result["schedules"].append(item)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--script-cwd", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--day", required=True)
    parser.add_argument("--execution-db", required=True, type=Path, action="append")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args(argv)
    try:
        report = root_snapshot(args.runtime_root, args.script_cwd, args.home, args.day)
        report["execution_windows"] = [
            execution_window(path, args.job_id, args.since, args.until)
            for path in args.execution_db
        ]
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "evidence_complete": False}))
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
