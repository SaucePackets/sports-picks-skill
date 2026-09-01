#!/usr/bin/env python3
"""Make the substitution of DK's number for ours explicit, per read.

The drought's mechanism is one substitution. The slate handicaps a game, the
gate discards that handicap under the market-only fallback and sets our
probability equal to DraftKings' de-vigged fair price, and the edge that
remains is ``dk_fair - polymarket_ask`` — a book-to-book residual measured at
a median of about +0.001 against a 0.05 floor. Nothing in the artifacts SAYS
this happened. It is inferred, every time, by noticing that two numbers are
equal.

So this report states it. For every recorded read it walks the chain

    dk_fair -> raw_probability -> uncertainty_haircut -> conservative_probability
            -> polymarket_ask -> edge

and classifies the read by what the NUMBERS do, not by what the label claims:

- ``market_substitution``  raw equals dk_fair on both sides and the haircut is
  zero. Our probability IS the book's. This is the market-only fallback's own
  arithmetic, and it is reported whether or not the read is labelled with the
  market-only ``model_version``.
- ``independent_handicap`` raw departs from dk_fair on at least one side. The
  read is making a claim of its own.
- ``unhandicapped``        no model trail at all (a ``not_priced`` game).
- ``indeterminate``        a trail exists but a field needed to decide is
  missing or unusable. Never folded into either of the first two.

Keying on the label instead would answer a different and weaker question. A
read tagged ``vig-mlb-market-v1`` whose raw probability differs from dk_fair is
not a market-only read, and a read tagged with some other version whose numbers
are exactly dk_fair is not an independent one. The label is reported alongside
the classification precisely so the two can be seen to disagree; a mismatch is
counted and named rather than resolved in favour of either.

The report also names, per read, whether the claimed ``model_version`` would be
accepted at the execution boundary — the same
``mlb_runtime_policy.model_deployment_errors`` the money gates call, not a
second opinion about it.

Read-only. Reads schedules and prints a report. No network, no writes, no gate
input, and nothing here is consulted by any executing code path.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_runtime_policy import (  # noqa: E402
    MARKET_MODEL_VERSION,
    model_deployment_errors,
)

REPORT_SCHEMA = "vig-mlb-probability-chain-v1"

CLASS_SUBSTITUTION = "market_substitution"
CLASS_INDEPENDENT = "independent_handicap"
CLASS_UNHANDICAPPED = "unhandicapped"
CLASS_INDETERMINATE = "indeterminate"
# Closed and zero-filled: a class that did not occur prints 0 rather than
# vanishing, so a reader can tell a constant axis from an impossible one.
CLASSES = (
    CLASS_SUBSTITUTION,
    CLASS_INDEPENDENT,
    CLASS_UNHANDICAPPED,
    CLASS_INDETERMINATE,
)

SIDES = ("away", "home")

# Equality between two independently rounded probabilities is a floating-point
# question, not an exact one. The slate writes three decimals, so anything
# inside a thousandth is the same number as far as this report is concerned.
# Deliberately NOT reused from mlb_game_reads' coherence tolerance: that one
# bounds an arithmetic identity the validator enforces, this one bounds a
# judgement about whether two separately sourced numbers are the same, and
# tying them together would let a change to either silently move the other.
SUBSTITUTION_TOLERANCE = 1e-3


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _side_number(entry: Any, field: str, side: str) -> float | None:
    block = entry.get(field) if isinstance(entry, dict) else None
    if not isinstance(block, dict):
        return None
    return _number(block.get(side))


def classify_read(entry: dict[str, Any]) -> dict[str, Any]:
    """Classify one game_reads entry by what its numbers do."""
    version = entry.get("model_version")
    version = version.strip() if isinstance(version, str) else None
    haircut = _number(entry.get("uncertainty_haircut"))
    raw = {side: _side_number(entry, "raw_probability", side) for side in SIDES}
    fair = {side: _side_number(entry, "dk_fair_prob", side) for side in SIDES}
    cons = {side: _side_number(entry, "conservative_probability", side) for side in SIDES}
    ask = {side: _side_number(entry, "polymarket_ask", side) for side in SIDES}

    trail_present = any(v is not None for v in raw.values()) or version is not None

    result: dict[str, Any] = {
        "game_pk": entry.get("game_pk"),
        "event_id": entry.get("event_id"),
        "away": entry.get("away"),
        "home": entry.get("home"),
        "disposition": entry.get("disposition"),
        "model_version": version,
        "uncertainty_haircut": haircut,
        "chain": {
            side: {
                "dk_fair_prob": fair[side],
                "raw_probability": raw[side],
                "conservative_probability": cons[side],
                "polymarket_ask": ask[side],
                "edge": (
                    None
                    if cons[side] is None or ask[side] is None
                    else round(cons[side] - ask[side], 6)
                ),
            }
            for side in SIDES
        },
    }

    if not trail_present:
        result["classification"] = CLASS_UNHANDICAPPED
        result["basis"] = "no model trail recorded; the game was never handicapped"
    elif haircut is None or any(
        raw[side] is None or fair[side] is None for side in SIDES
    ):
        result["classification"] = CLASS_INDETERMINATE
        missing = [
            name
            for name, value in (
                ("uncertainty_haircut", haircut),
                ("raw_probability.away", raw["away"]),
                ("raw_probability.home", raw["home"]),
                ("dk_fair_prob.away", fair["away"]),
                ("dk_fair_prob.home", fair["home"]),
            )
            if value is None
        ]
        result["basis"] = (
            "cannot decide whether our number is the book's: missing "
            + ", ".join(missing)
        )
    else:
        deltas = {side: raw[side] - fair[side] for side in SIDES}
        substituted = (
            abs(haircut) <= SUBSTITUTION_TOLERANCE
            and all(abs(deltas[side]) <= SUBSTITUTION_TOLERANCE for side in SIDES)
        )
        result["raw_minus_dk_fair"] = {
            side: round(deltas[side], 6) for side in SIDES
        }
        result["classification"] = (
            CLASS_SUBSTITUTION if substituted else CLASS_INDEPENDENT
        )
        result["basis"] = (
            "raw_probability equals dk_fair_prob on both sides at a zero "
            "haircut: our probability is DraftKings'"
            if substituted
            else "raw_probability departs from dk_fair_prob: the read makes its own claim"
        )

    if version is None:
        result["label_agrees_with_numbers"] = None
        result["execution_eligible_version"] = None
    else:
        labelled_market_only = version == MARKET_MODEL_VERSION
        if result["classification"] in (CLASS_SUBSTITUTION, CLASS_INDEPENDENT):
            result["label_agrees_with_numbers"] = labelled_market_only == (
                result["classification"] == CLASS_SUBSTITUTION
            )
        else:
            result["label_agrees_with_numbers"] = None
        result["execution_eligible_version"] = not model_deployment_errors(
            {"model_version": version}
        )
    return result


def build_report(schedules: list[tuple[str, Any]]) -> dict[str, Any]:
    """Report over one or more (label, schedule) pairs."""
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for label, schedule in schedules:
        reads = schedule.get("game_reads") if isinstance(schedule, dict) else None
        if not isinstance(reads, list):
            # Named, never silently dropped: a day with no recorded reads is
            # the finding, and a report that omits it counts the wrong
            # denominator.
            skipped.append(f"{label}: no game_reads list recorded")
            continue
        for entry in reads:
            if not isinstance(entry, dict):
                skipped.append(f"{label}: a game_reads entry is not an object")
                continue
            row = classify_read(entry)
            row["source"] = label
            rows.append(row)

    counts = {name: 0 for name in CLASSES}
    for row in rows:
        counts[row["classification"]] += 1
    mismatches = [row for row in rows if row.get("label_agrees_with_numbers") is False]
    ineligible = [row for row in rows if row.get("execution_eligible_version") is False]
    return {
        "schema": REPORT_SCHEMA,
        "reads": len(rows),
        "counts": counts,
        "label_number_mismatches": len(mismatches),
        "versions_not_execution_eligible": len(ineligible),
        "skipped": skipped,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("schedules", type=Path, nargs="+")
    parser.add_argument(
        "--summary-only", action="store_true", help="omit per-read rows from the output"
    )
    args = parser.parse_args(argv)

    loaded: list[tuple[str, Any]] = []
    for path in args.schedules:
        try:
            loaded.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"{path}: {exc}")
    report = build_report(loaded)
    if args.summary_only:
        report = {key: value for key, value in report.items() if key != "rows"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
