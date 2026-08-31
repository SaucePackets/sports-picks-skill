#!/usr/bin/env python3
"""Build the model evaluation dataset from per-game reads instead of picks.

``mlb_probability_model.py dataset`` builds its evaluation set from
``picks.json`` — settled, EXECUTED picks. That is a closed loop: a model
version cannot deploy without out-of-sample evidence, evidence rows only come
from bets we placed, and we cannot place bets while no model is deployed. It
grows at roughly half a row a day when the card is active and exactly zero a
day during a drought; on 2026-08-31 the whole ledger yielded eight usable rows
across four months.

The way out is that a handicap on a game we PASSED is still a testable
pre-pitch prediction. The slate handicaps every scheduled game and keeps the
one or two it bets; ``game_reads`` now records the rest. That is roughly
fourteen rows a night with nothing at risk.

It is also the better sample, and that is the part worth stating plainly:
``picks.json`` is selection-biased by construction, because a pick exists only
where the model liked itself enough to clear a five-point edge floor.
Calibration measured there is calibration measured where the model was most
confident. Every scheduled game, whatever we thought of it, is the population
the deployment gate should actually be judging.

**One row per game, always the away side.** An evaluation row needs one
probability against one binary outcome, and a read carries two sides. Emitting
both would double the count with perfectly anti-correlated rows and quietly
break the independence every metric here assumes. Choosing "the side the model
favoured" would re-introduce exactly the selection bias this module exists to
remove. So the rule is fixed, mechanical, and independent of what the model
thought: the away side, every time.

**Nothing is imputed.** A read missing any of the three probabilities, the
model identity, or a final is SKIPPED with a stated reason, never defaulted.
The skip list is part of the output, because a dataset that silently drops the
games it could not parse is a dataset whose denominator nobody can check.

The rows this emits are exactly what ``mlb_probability_model``'s existing
evaluator and deployment gate already consume. Neither is changed here: they
were never broken, only starved.

Usage:
  python3 scripts/mlb_model_eval_dataset.py \
      --schedules .picks/execute --start 2026-09-01 --until 2026-09-14 \
      --finals .picks/audit-results --out dataset.jsonl
  python3 scripts/mlb_probability_model.py evaluate --dataset dataset.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_final_scores import final_scores  # noqa: E402

# Consulted, not restated. This repo has spent four review rounds on copies of
# one numeric rule drifting apart; a dataset builder that decided for itself
# what counts as a usable probability would be the fifth. A test pins that this
# name is the recorder's own rule and not a look-alike.
from mlb_game_reads import _is_probability  # noqa: E402
from numeric_util import is_finite_number  # noqa: E402

# The side every row is scored on. Fixed on purpose — see the module docstring.
EVALUATED_SIDE = "away"

# The fields mlb_probability_model's evaluator and deployment gate read. Named
# here so a change on either side of the seam is a visible edit rather than a
# row that silently evaluates to nothing.
REQUIRED_PROBABILITY_FIELDS = (
    "dk_fair_prob",
    "raw_probability",
    "conservative_probability",
)


def _final_by_game_pk(payload: Any) -> dict[int, dict[str, Any]]:
    """Index a StatsAPI schedule payload by gamePk, Final games only.

    Parsed with ``mlb_final_scores.final_scores`` rather than re-walking the
    payload here: settlement and evaluation disagreeing about what a final is
    would be a defect nobody would find until it mattered.
    """
    if not isinstance(payload, dict):
        return {}
    indexed: dict[int, dict[str, Any]] = {}
    for row in final_scores(payload):
        game_pk = row.get("gamePk")
        if isinstance(game_pk, int) and not isinstance(game_pk, bool):
            indexed[game_pk] = row
    return indexed


def row_for_read(
    date: str, entry: Any, finals: dict[int, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """One evaluation row, or ``None`` and the reason there is not one."""
    if not isinstance(entry, dict):
        return None, f"{date}: game_reads entry is not an object"
    game_pk = entry.get("game_pk")
    if not isinstance(game_pk, int) or isinstance(game_pk, bool):
        return None, f"{date}: game_reads entry has no usable game_pk"
    label = f"{date} game {game_pk}"

    missing = []
    for field in REQUIRED_PROBABILITY_FIELDS:
        value = entry.get(field)
        side_value = value.get(EVALUATED_SIDE) if isinstance(value, dict) else None
        if not _is_probability(side_value):
            missing.append(field)
    if missing:
        return None, f"{label}: no usable {EVALUATED_SIDE} value for {', '.join(missing)}"

    model_version = entry.get("model_version")
    if not isinstance(model_version, str) or not model_version.strip():
        return None, f"{label}: no model_version, so the row cannot be attributed to a model"

    final = finals.get(game_pk)
    if final is None:
        return None, f"{label}: no Final result available"
    winner = final.get("winner")
    if not isinstance(winner, str) or not winner.strip():
        return None, f"{label}: the final carries no winner"
    away_name = final.get("away")
    home_name = final.get("home")
    if winner not in (away_name, home_name):
        return None, f"{label}: winner {winner!r} is neither team on the final"

    row: dict[str, Any] = {
        "date": date,
        # The evaluator sorts on (date, pick_id). These are reads, not picks;
        # the key keeps that contract without changing the evaluator, and the
        # value says what it really is.
        "pick_id": f"read-{date}-{game_pk}-{EVALUATED_SIDE}",
        "game_pk": game_pk,
        "side": away_name,
        "source": "game_reads",
        "model_version": model_version.strip(),
        "outcome": 1 if winner == away_name else 0,
    }
    for field in REQUIRED_PROBABILITY_FIELDS:
        row[field] = float(entry[field][EVALUATED_SIDE])
    haircut = entry.get("uncertainty_haircut")
    row["uncertainty_haircut"] = (
        float(haircut) if is_finite_number(haircut) and haircut >= 0 else None
    )
    for field, key in (("polymarket_ask", "slate_ask"), ("net_edge", "net_edge")):
        value = entry.get(field)
        if isinstance(value, dict) and is_finite_number(value.get(EVALUATED_SIDE)):
            row[key] = float(value[EVALUATED_SIDE])
    return row, None


def build_rows(
    schedules: list[tuple[str, Any]], finals_by_date: dict[str, dict[int, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows and skip reasons, in date order. Nothing is imputed or defaulted."""
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for date, schedule in schedules:
        if not isinstance(schedule, dict):
            skipped.append(f"{date}: schedule is not a JSON object")
            continue
        reads = schedule.get("game_reads")
        if not isinstance(reads, list):
            skipped.append(f"{date}: schedule carries no game_reads array")
            continue
        finals = finals_by_date.get(date) or {}
        if not finals:
            skipped.append(f"{date}: no finals available for this date")
        for entry in reads:
            row, reason = row_for_read(date, entry, finals)
            if row is None:
                skipped.append(reason or f"{date}: unusable read")
            else:
                rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["pick_id"]))
    return rows, skipped


def dates_in_range(start: dt.date, until: dt.date) -> list[str]:
    if until < start:
        raise ValueError("--until is before --start")
    span = (until - start).days
    return [(start + dt.timedelta(days=offset)).isoformat() for offset in range(span + 1)]


def load_schedules(directory: Path, dates: list[str]) -> tuple[list[tuple[str, Any]], list[str]]:
    schedules: list[tuple[str, Any]] = []
    missing: list[str] = []
    for date in dates:
        path = directory / f"{date}-schedule.json"
        if not path.is_file():
            missing.append(f"{date}: no schedule file at {path.name}")
            continue
        try:
            schedules.append((date, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError) as exc:
            missing.append(f"{date}: schedule unreadable ({exc})")
    return schedules, missing


def load_finals(
    directory: Path | None, dates: list[str], fetch: bool
) -> tuple[dict[str, dict[int, dict[str, Any]]], list[str]]:
    """Finals from a cache directory, or fetched. Never both silently."""
    finals: dict[str, dict[int, dict[str, Any]]] = {}
    notes: list[str] = []
    for date in dates:
        payload: Any = None
        if directory is not None:
            path = directory / f"{date}.json"
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    notes.append(f"{date}: cached finals unreadable ({exc})")
        if payload is None and fetch:
            # Imported here so the default, offline path never needs the network
            # module at all.
            from http_util import fetch_json  # noqa: PLC0415

            try:
                payload = fetch_json(
                    "https://statsapi.mlb.com/api/v1/schedule"
                    f"?sportId=1&date={date}&hydrate=linescore"
                )
            except Exception as exc:  # noqa: BLE001 - reported, never silent
                notes.append(f"{date}: finals fetch failed ({exc})")
                payload = None
        if payload is not None:
            finals[date] = _final_by_game_pk(payload)
    return finals, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the model evaluation dataset from game_reads plus finals."
    )
    parser.add_argument("--schedules", type=Path, required=True)
    parser.add_argument("--start", required=True, help="first date, YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="last date, YYYY-MM-DD")
    parser.add_argument(
        "--finals", type=Path, help="directory of cached StatsAPI schedule payloads, <date>.json"
    )
    parser.add_argument(
        "--fetch-finals",
        action="store_true",
        help="fetch finals for any date the cache does not cover (network)",
    )
    parser.add_argument("--out", type=Path, help="write JSONL here instead of stdout")
    args = parser.parse_args(argv)

    try:
        dates = dates_in_range(
            dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.until)
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.finals is None and not args.fetch_finals:
        parser.error("pass --finals, --fetch-finals, or both; a dataset with no outcomes is empty")

    schedules, missing = load_schedules(args.schedules, dates)
    finals, notes = load_finals(args.finals, dates, args.fetch_finals)
    rows, skipped = build_rows(schedules, finals)

    out = "\n".join(json.dumps(row) for row in rows)
    if args.out:
        args.out.write_text(out + ("\n" if out else ""), encoding="utf-8")
    else:
        print(out)
    print(
        json.dumps(
            {
                "rows": len(rows),
                "dates_requested": len(dates),
                "schedules_read": len(schedules),
                "skipped": len(skipped) + len(missing) + len(notes),
                "skip_reasons": missing + notes + skipped,
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
