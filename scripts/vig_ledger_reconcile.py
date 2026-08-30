#!/usr/bin/env python3
"""Detect conflicts between the canonical picks ledger and its derived views.

``picks.json`` is the canonical ledger. ``record.json`` is a denormalized
view of it, recomputed by the settlement agent — which means it is prose-
maintained state that has gone stale before and silently disabled settlement
(see the comment in ``vig_postgame_gate.py``). Nothing checked it, so the two
could disagree indefinitely and every report quoted whichever one it happened
to read.

There are two independent conflict classes and this module covers both:

* **Derived-view conflict** — ``record.json``'s stored counters disagree with
  the same counters recomputed from ``picks.json``, or ``record.json``
  disagrees with itself (wins + losses + voids must equal settled).
* **Split-brain conflict** — more than one file claims to be the ledger and
  they are not the same file. The runtime's ``.picks/picks.json`` is today a
  symlink to ``~/notes/Sports/picks/picks.json``, which is the fix for the
  2026-08-23 split brain; this makes a regression detectable rather than
  something to rediscover from mismatched totals months later.

Read-only. Exits 1 when any conflict exists, 0 when clean.

Complementary to ``receipts_ledger_reconcile.py``, which answers a different
question: whether every filled Polymarket receipt reached the ledger at all.
This one answers whether everything reading the ledger sees the same numbers.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Iterable

CANONICAL_ENV = "VIG_PICKS_FILE"
DEFAULT_PICKS = Path(os.environ.get(CANONICAL_ENV) or Path.home() / "notes/Sports/picks/picks.json")

SETTLED_STATUS = "settled"
OPEN_STATUSES = ("active", "pending")
DECIDED_RESULTS = ("win", "loss")
VOID_RESULTS = ("void", "push", "cancelled", "canceled")

# Money comparisons tolerate half a cent; the ledger stores rounded dollars
# and an exact float equality would report a conflict on every rounding.
MONEY_TOLERANCE = 0.005
MONEY_KEYS = ("total_staked", "total_commission_paid", "total_pnl")


def _number(value: Any) -> float:
    try:
        if isinstance(value, bool) or value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def recompute_counters(picks: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Recompute every ``record.json`` counter that is derivable from picks.

    Only derivable counters appear here. ``current_streak`` and the free-text
    notes are excluded deliberately: a checker that guesses at a field it
    cannot derive reports conflicts that are not conflicts, and one false
    alarm is enough to make the whole check ignorable.
    """
    rows = [pick for pick in picks if isinstance(pick, dict)]
    settled = [row for row in rows if row.get("status") == SETTLED_STATUS]
    wins = sum(1 for row in settled if row.get("result") == "win")
    losses = sum(1 for row in settled if row.get("result") == "loss")
    voids = sum(1 for row in settled if row.get("result") in VOID_RESULTS)
    decided = wins + losses
    return {
        "total": len(rows),
        "total_picks": len(rows),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "decision_count": decided,
        "pending": sum(1 for row in rows if row.get("status") == "pending"),
        "active": sum(1 for row in rows if row.get("status") == "active"),
        "win_rate": round(wins / decided, 6) if decided else 0.0,
        "total_staked": round(
            sum(_number(row.get("entry_notional") or row.get("unit_size")) for row in settled), 2
        ),
        "total_commission_paid": round(sum(_number(row.get("commission")) for row in settled), 2),
        "total_pnl": round(sum(_number(row.get("pnl")) for row in settled), 2),
    }


def _conflict(kind: str, field: str, stored: Any, expected: Any, detail: str = "") -> dict[str, Any]:
    return {"kind": kind, "field": field, "stored": stored, "expected": expected, "detail": detail}


def counter_conflicts(
    picks: Iterable[dict[str, Any]], record: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every ``record.json`` counter that disagrees with ``picks.json``.

    Fields absent from ``record.json`` are skipped rather than reported: the
    view is allowed to carry fewer counters than the ledger can derive, and
    demanding presence would turn a schema addition into a false conflict.
    """
    expected = recompute_counters(picks)
    conflicts: list[dict[str, Any]] = []
    for field, want in expected.items():
        if field not in record:
            continue
        got = record[field]
        if field in MONEY_KEYS or field == "win_rate":
            tolerance = MONEY_TOLERANCE if field in MONEY_KEYS else 1e-6
            if abs(_number(got) - float(want)) > tolerance:
                conflicts.append(_conflict("derived", field, got, want))
        elif int(_number(got)) != int(want):
            conflicts.append(_conflict("derived", field, got, want))
    return conflicts


def internal_conflicts(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Conflicts visible inside ``record.json`` without reading the ledger.

    Worth checking separately: these bite even when the ledger is missing or
    unreadable, and they catch a hand-edited view that no recomputation ran
    over.
    """
    conflicts: list[dict[str, Any]] = []
    parts = [key for key in ("wins", "losses", "voids") if key in record]
    if "settled" in record and parts:
        total = sum(int(_number(record[key])) for key in parts)
        if total != int(_number(record["settled"])):
            conflicts.append(
                _conflict(
                    "internal", "settled", record["settled"], total,
                    detail="wins + losses + voids must equal settled",
                )
            )
    if {"decision_count", "wins", "losses"} <= set(record):
        decided = int(_number(record["wins"])) + int(_number(record["losses"]))
        if decided != int(_number(record["decision_count"])):
            conflicts.append(
                _conflict(
                    "internal", "decision_count", record["decision_count"], decided,
                    detail="decision_count must equal wins + losses",
                )
            )
    return conflicts


def source_conflicts(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Detect a split brain: two ledger paths that are different files.

    Resolution is by ``Path.resolve()``, so a symlink or a hardlink-free copy
    is the distinction that matters. Two distinct real paths are reported
    only when their contents actually differ — a byte-identical copy is a
    latent hazard, not a live conflict, and is reported as such.
    """
    seen: dict[Path, tuple[Path, bytes | None]] = {}
    conflicts: list[dict[str, Any]] = []
    for candidate in paths:
        path = Path(candidate)
        if not path.exists():
            continue
        real = path.resolve()
        try:
            payload: bytes | None = real.read_bytes()
        except OSError as exc:
            conflicts.append(
                _conflict("source", str(path), str(real), "readable", detail=f"unreadable: {exc}")
            )
            payload = None
        if real in seen:
            # Same file reached by a second name — a symlink to the canonical
            # ledger, which is the fix, not the fault.
            continue
        for other_real, (other_path, other_payload) in seen.items():
            differ = payload is None or other_payload is None or payload != other_payload
            conflicts.append(
                _conflict(
                    "source", str(path), str(real), str(other_real),
                    detail=(
                        f"a second ledger file distinct from {other_path}; contents "
                        + ("DIFFER — reports disagree depending on which is read"
                           if differ else
                           "are identical today, but nothing keeps them so (latent split brain)")
                    ),
                )
            )
        seen[real] = (path, payload)
    return conflicts


def load_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"missing: {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable {path}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect conflicts between picks.json and its derived record.json view."
    )
    parser.add_argument("--picks-file", type=Path, default=DEFAULT_PICKS,
                        help=f"canonical ledger (default: ${CANONICAL_ENV} or ~/notes/Sports/picks/picks.json)")
    parser.add_argument("--record-file", type=Path, default=None,
                        help="derived view (default: record.json beside the ledger)")
    parser.add_argument("--also-ledger", type=Path, action="append", default=[],
                        help="another path claiming to be the ledger; repeatable")
    args = parser.parse_args(argv)

    record_path = args.record_file or args.picks_file.with_name("record.json")

    conflicts: list[dict[str, Any]] = []
    problems: list[str] = []

    data, error = load_json(args.picks_file)
    if error:
        problems.append(f"canonical ledger {error}")
        picks: list[dict[str, Any]] = []
    else:
        picks = data.get("picks", []) if isinstance(data, dict) else []
        if not isinstance(picks, list):
            problems.append(f"canonical ledger picks must be a list: {args.picks_file}")
            picks = []

    record, record_error = load_json(record_path)
    if record_error:
        problems.append(f"derived record {record_error}")
        record = None
    elif not isinstance(record, dict):
        problems.append(f"derived record must be an object: {record_path}")
        record = None

    if record is not None:
        conflicts.extend(internal_conflicts(record))
        if not error:
            conflicts.extend(counter_conflicts(picks, record))

    conflicts.extend(source_conflicts([args.picks_file, *args.also_ledger]))

    print(f"canonical ledger: {args.picks_file}")
    print(f"derived record:   {record_path}")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    for conflict in conflicts:
        detail = f" ({conflict['detail']})" if conflict["detail"] else ""
        print(
            f"LEDGER CONFLICT [{conflict['kind']}] {conflict['field']}: "
            f"stored {conflict['stored']!r} != expected {conflict['expected']!r}{detail}"
        )
    if conflicts or problems:
        print(
            f"RECONCILE FAILED: {len(conflicts)} conflict(s), {len(problems)} problem(s). "
            "picks.json is canonical — recompute record.json from it, never the reverse."
        )
        return 1
    print("RECONCILE OK: the derived record agrees with the canonical ledger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
