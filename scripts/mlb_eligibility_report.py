#!/usr/bin/env python3
"""A deterministic, read-only eligibility report over an MLB slate's game reads.

The 2026-09-02 slate is why this exists. Stage 2 enumerated fifteen games, four
of them were called out in prose as obvious mismatch spots, and the day produced
an empty candidate list with narrative pass notes. Nothing in the artifacts said
which rail blocked which side of which game, because the decision never existed
in a form anything could read: ``mlb_stage2_scan`` produces a denominator and
context, ``mlb_slate_writer --skeleton`` produces stubs, and the disposition in
between was prompt work.

This module does not close that gap by making the decision. It closes it by
making the decision CHECKABLE: given the numbers the run recorded, it states —
deterministically, from the deployed policy — what the price says about each
side, and whether the disposition the run wrote agrees with the run's own
numbers. Nothing here authors a disposition, promotes a model, moves the edge
floor, or touches an order. PR #79's rule is the reason: the producer may not
rule on its own candidates. The report reports; a human still decides.

**It runs on a DRAFT as well as a landed schedule**, which is the point of the
"before publishing" requirement. Both are documents carrying ``game_reads``, and
the report is a pure function of that array plus the policy — so the rows an
operator sees before landing are, field for field, the rows they would see
after. A preflight view that could differ from the flight would be worse than
none.

**Verdicts are numbers, agreement is bookkeeping.** ``verdict`` says what the
recorded price and handicap imply under the deployed floor. ``agreement`` says
whether the recorded disposition is consistent with that verdict. They are kept
separate on purpose: conflating "this side clears the floor" with "we should
have bet it" is precisely the step this repository will not take mechanically.

No network. No writes except the report file under ``--write``. Read-only with
respect to schedules, drafts, ledgers, and policy.
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

import mlb_game_reads  # noqa: E402
import mlb_runtime_policy  # noqa: E402
from numeric_util import is_finite_number  # noqa: E402

REPORT_SCHEMA = "vig-mlb-eligibility-report-v1"

SIDES = ("away", "home")

# What the recorded numbers say about one side, under the deployed floor.
# Closed and enumerated, same discipline as the receipt's verdicts: a side that
# fits none of these is a bug here, not a fifth informal state in `detail`.
SIDE_ELIGIBLE = "eligible"                # priced, handicapped, edge >= floor
SIDE_BELOW_FLOOR = "below_edge_floor"     # priced, handicapped, edge < floor
SIDE_NOT_PRICED = "not_priced"            # handicapped, no ask to price against
SIDE_UNHANDICAPPED = "unhandicapped"      # priced, no model probability
SIDE_NO_INPUTS = "no_inputs"              # neither, and the read says why
SIDE_VERDICTS = (
    SIDE_ELIGIBLE,
    SIDE_BELOW_FLOOR,
    SIDE_NOT_PRICED,
    SIDE_UNHANDICAPPED,
    SIDE_NO_INPUTS,
)

# Precedence when the two sides disagree about WHY nothing is eligible. Ordered
# most to least informative: a game with one priced side below the floor and one
# side nobody could price is a below-floor game, because that is the fact an
# operator can act on. `eligible` is handled before this list is consulted.
GAME_VERDICT_PRECEDENCE = (
    SIDE_BELOW_FLOOR,
    SIDE_NOT_PRICED,
    SIDE_UNHANDICAPPED,
    SIDE_NO_INPUTS,
)

AGREES = "agrees"
DISAGREES = "disagrees"
UNRECORDED = "unrecorded"

# The report cannot be computed without the floor, and it will not invent one.
STATUS_OK = "ok"
STATUS_POLICY_UNAVAILABLE = "policy_unavailable"
STATUS_NO_READS = "no_reads"


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _side_number(entry: dict[str, Any], field: str, side: str) -> float | None:
    """One usable per-side number, or None.

    None means "not recorded in a usable form" and nothing else. Every caller
    below treats it as an absence to be REPORTED, never as a zero to compute
    with — "no price" and "a price of zero" are different facts and only one of
    them is a finding.
    """
    value = entry.get(field)
    if not isinstance(value, dict):
        return None
    side_value = value.get(side)
    return float(side_value) if is_finite_number(side_value) else None


def side_row(entry: dict[str, Any], side: str, policy: Any) -> dict[str, Any]:
    """The eligibility view of one side of one game.

    ``net_edge`` is reported as the run RECORDED it and again as recomputed from
    the two numbers beside it. They are the same by the time a schedule lands —
    ``mlb_game_reads`` refuses a read where they disagree — but this report also
    runs on a DRAFT, which has been through no validator at all, and a draft
    whose edge does not match its own arithmetic is exactly what an operator
    wants to see before publishing rather than after.

    The verdict is computed from the RECOMPUTED edge. A stored number is a
    claim; the recomputation is the arithmetic, and every rail in this repo
    already prefers the latter (``mlb_runtime_policy.live_conservative_edge``
    makes the same choice at execution time for the same reason).
    """
    dk_fair = _side_number(entry, "dk_fair_prob", side)
    ask = _side_number(entry, "polymarket_ask", side)
    raw = _side_number(entry, "raw_probability", side)
    conservative = _side_number(entry, "conservative_probability", side)
    recorded_edge = _side_number(entry, "net_edge", side)

    computed_edge = None
    if ask is not None and conservative is not None:
        computed_edge = round(conservative - ask, 6)

    floor = getattr(policy, "min_conservative_edge", None)
    if not is_finite_number(floor):
        # Not a verdict of "unknown": there is no such verdict, and inventing
        # one here would put a row in the table that looks computed. A caller
        # reaching this line without a policy has skipped ``build_report``'s
        # fail-closed branch, which is a bug in the caller.
        raise ValueError(
            "side_row requires a loaded MlbSelectionPolicy; without the floor "
            "no side can be called eligible or below it"
        )
    if conservative is None and ask is None:
        verdict = SIDE_NO_INPUTS
    elif conservative is None:
        verdict = SIDE_UNHANDICAPPED
    elif ask is None:
        verdict = SIDE_NOT_PRICED
    elif computed_edge >= float(floor):
        verdict = SIDE_ELIGIBLE
    else:
        verdict = SIDE_BELOW_FLOOR

    # The executable ceiling the reviewer would carry on a candidate for this
    # side, from the policy's own function rather than a second copy of
    # `conservative - floor`.
    ceiling = None
    if conservative is not None and policy is not None:
        ceiling = policy.ceiling_for(conservative)

    return {
        "side": side,
        "team": entry.get(side) if isinstance(entry.get(side), str) else None,
        "dk_fair_prob": dk_fair,
        "polymarket_ask": ask,
        "raw_probability": raw,
        "conservative_probability": conservative,
        "net_edge_recorded": recorded_edge,
        "net_edge_recomputed": computed_edge,
        "max_polymarket_price": ceiling,
        "verdict": verdict,
    }


def _game_verdict(rows: list[dict[str, Any]]) -> str:
    verdicts = [row["verdict"] for row in rows]
    if SIDE_ELIGIBLE in verdicts:
        return SIDE_ELIGIBLE
    for candidate in GAME_VERDICT_PRECEDENCE:
        if candidate in verdicts:
            return candidate
    return SIDE_NO_INPUTS


def _non_price_rails(entry: dict[str, Any]) -> list[str]:
    rails = entry.get("refusing_rails")
    if not isinstance(rails, list):
        return []
    return sorted(
        rail
        for rail in rails
        if isinstance(rail, str) and rail != mlb_game_reads.PRICE_RAIL
    )


def agreement(entry: dict[str, Any], verdict: str) -> tuple[str, str]:
    """Does the recorded disposition agree with the recorded numbers?

    Bookkeeping, not judgement. Each rule below is an identity between a word
    the run wrote and arithmetic the run also wrote; none of them says what the
    run SHOULD have decided.

    - ``candidate``/``lineup_watchlist`` claims a playable side, so the numbers
      must produce one.
    - ``not_priced`` claims there was nothing to price against.
    - ``pass`` is the one with a real degree of freedom: a side clearing the
      price floor may still be refused by a handicapping rail (a starter floor,
      a bullpen path), and refusing it is exactly what those rails are for. So a
      ``pass`` on an eligible side agrees IF some rail other than
      ``price_discipline`` is named, and disagrees only when price is the whole
      stated reason — which ``mlb_game_reads.policy_disposition_errors`` refuses
      outright on a landed schedule, and which this report surfaces on a draft
      before it gets there.
    """
    disposition = entry.get("disposition")
    if disposition not in mlb_game_reads.DISPOSITIONS:
        return UNRECORDED, f"disposition {disposition!r} is not a recorded decision"
    if disposition in mlb_game_reads.ACCEPTING_DISPOSITIONS:
        if verdict == SIDE_ELIGIBLE:
            return AGREES, "a side clears the floor and the game was carded"
        return (
            DISAGREES,
            f"the game was carded as {disposition!r} but no side is eligible ({verdict})",
        )
    if disposition == "not_priced":
        if verdict in (SIDE_NOT_PRICED, SIDE_NO_INPUTS):
            return AGREES, "no usable price was recorded on either side"
        return (
            DISAGREES,
            f"recorded not_priced but the numbers say {verdict}",
        )
    # pass
    if verdict != SIDE_ELIGIBLE:
        return AGREES, f"no side clears the floor ({verdict})"
    others = _non_price_rails(entry)
    if others:
        return AGREES, f"an eligible side was refused by {', '.join(others)}"
    return (
        DISAGREES,
        "a side clears the edge floor and the only rail named is price_discipline",
    )


def game_row(entry: Any, index: int, policy: Any) -> dict[str, Any]:
    """One game's report row, including what is wrong with the read itself.

    ``read_errors`` carries ``validate_read``'s findings verbatim rather than a
    second opinion. Without them a malformed read would report as ``no_inputs``
    and look like a quiet day; the whole failure this lane keeps meeting is a
    defect wearing the appearance of an honest absence.
    """
    if not isinstance(entry, dict):
        return {
            "index": index,
            "game_pk": None,
            "read_errors": [f"game_reads[{index}] must be an object"],
            "sides": [],
            "verdict": SIDE_NO_INPUTS,
            "agreement": UNRECORDED,
            "agreement_detail": "the read is not an object",
            "disposition": None,
            "refusing_rails": [],
        }
    rows = [side_row(entry, side, policy) for side in SIDES]
    verdict = _game_verdict(rows)
    verdict_agreement, detail = agreement(entry, verdict)
    rails = entry.get("refusing_rails")
    return {
        "index": index,
        "game_pk": entry.get("game_pk"),
        "event_id": entry.get("event_id"),
        "away": entry.get("away"),
        "home": entry.get("home"),
        "disposition": entry.get("disposition"),
        "refusing_rails": sorted(rails) if isinstance(rails, list) else [],
        "unavailable": entry.get("unavailable") if isinstance(entry.get("unavailable"), dict) else {},
        "model_version": entry.get("model_version"),
        "uncertainty_haircut": entry.get("uncertainty_haircut"),
        "sides": rows,
        "verdict": verdict,
        "agreement": verdict_agreement,
        "agreement_detail": detail,
        "read_errors": mlb_game_reads.validate_read(entry, index),
    }


def build_report(document: Any, policy: Any, source: str = "") -> dict[str, Any]:
    """The whole report for one draft or schedule document.

    A pure function of ``document['game_reads']`` and ``policy`` — no clock
    beyond the stamp, no filesystem, no network. That purity is what makes the
    draft-versus-landed parity a property rather than a hope: ``land()`` adds a
    denominator and canonicalises ids, and neither is an input here.
    """
    reads = document.get("game_reads") if isinstance(document, dict) else None
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "sport": "MLB",
        "source": source,
        "day": document.get("date") if isinstance(document, dict) else None,
        "generated_at_utc": utc_now_iso(),
        "policy_version": getattr(policy, "policy_version", None),
        "min_conservative_edge": getattr(policy, "min_conservative_edge", None),
        "status": STATUS_OK,
        "games": [],
        "counts": {},
    }
    if policy is None:
        # Fails closed, and says which question it could not answer. Inventing
        # 0.05 here would be the restated-constant defect: the floor is policy,
        # it has moved before, and a report quoting a stale one is worse than a
        # report that refuses.
        report["status"] = STATUS_POLICY_UNAVAILABLE
        report["detail"] = (
            "the MLB selection policy could not be loaded, so no side can be "
            "compared to the edge floor; no verdicts were computed"
        )
        return report
    if not isinstance(reads, list):
        # A MISSING array, not an empty one. `game_reads: []` on a day the scan
        # enumerated no games is an honest zero and reports as an ordinary empty
        # table; a document with no array at all is the 2026-09-02 shape, and
        # this lane exists because those two shared an outcome. They do not
        # share one here either.
        report["status"] = STATUS_NO_READS
        report["detail"] = (
            "the document carries no game_reads array at all; that is a missing "
            "record, not an empty slate — a day with no games records game_reads: []"
        )
        return report

    report["games"] = [game_row(entry, index, policy) for index, entry in enumerate(reads)]
    counts: dict[str, int] = {}
    # Zero-filled over the closed vocabularies. A category printing 0 is how a
    # reader tells a constant axis from an impossible one; without it, a rail
    # that never fires and a rail that cannot fire look identical.
    for verdict in SIDE_VERDICTS:
        counts[f"verdict_{verdict}"] = 0
    for name in (AGREES, DISAGREES, UNRECORDED):
        counts[f"agreement_{name}"] = 0
    for disposition in sorted(mlb_game_reads.DISPOSITIONS):
        counts[f"disposition_{disposition}"] = 0
    counts["games"] = len(report["games"])
    counts["reads_with_errors"] = 0
    for game in report["games"]:
        counts[f"verdict_{game['verdict']}"] += 1
        counts[f"agreement_{game['agreement']}"] += 1
        key = f"disposition_{game['disposition']}"
        if key in counts:
            counts[key] += 1
        if game["read_errors"]:
            counts["reads_with_errors"] += 1
    report["counts"] = counts
    return report


def format_report(report: dict[str, Any]) -> str:
    """One fixed-width table an operator can read in a terminal."""
    lines: list[str] = []
    lines.append(
        f"MLB eligibility report — day {report.get('day')} — status {report.get('status')}"
    )
    if report.get("status") != STATUS_OK:
        lines.append(f"  {report.get('detail', '')}")
        return "\n".join(lines)
    lines.append(
        f"  policy {report.get('policy_version')} floor "
        f"{report.get('min_conservative_edge')}"
    )
    for game in report["games"]:
        lines.append(
            f"  {game.get('away')} at {game.get('home')} "
            f"(game_pk {game.get('game_pk')}) — {game['verdict']}, "
            f"disposition {game.get('disposition')!r} {game['agreement']}"
        )
        for side in game["sides"]:
            lines.append(
                "    {side:<5} dk_fair {dk} ask {ask} raw {raw} cons {cons} "
                "edge {edge} ceiling {ceiling} -> {verdict}".format(
                    side=side["side"],
                    dk=_fmt(side["dk_fair_prob"]),
                    ask=_fmt(side["polymarket_ask"]),
                    raw=_fmt(side["raw_probability"]),
                    cons=_fmt(side["conservative_probability"]),
                    edge=_fmt(side["net_edge_recomputed"]),
                    ceiling=_fmt(side["max_polymarket_price"]),
                    verdict=side["verdict"],
                )
            )
        if game["refusing_rails"]:
            lines.append(f"    rails: {', '.join(game['refusing_rails'])}")
        for error in game["read_errors"]:
            lines.append(f"    READ DEFECT: {error}")
    counts = report.get("counts", {})
    lines.append(
        "  totals: "
        + " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return "  --  " if value is None else f"{float(value):+.4f}"


def load_document(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--schedule", type=Path, help="a landed schedule under .picks/execute/"
    )
    target.add_argument(
        "--draft",
        type=Path,
        help="a slate draft from mlb_slate_writer --skeleton, before landing",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON instead of a table"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="risk_limits.json directory (default: the deployed Vig state dir)",
    )
    args = parser.parse_args(argv)

    path = args.schedule or args.draft
    try:
        document = load_document(path)
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    policy = mlb_runtime_policy.load_mlb_selection_policy(args.state_dir)
    report = build_report(document, policy, source=str(path))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else format_report(report))
    # Exit 1 when the report could not be computed, or when a recorded decision
    # contradicts the numbers recorded beside it. A disagreement is not an
    # opinion about the pick — it is a record that cannot be both true, and it
    # must not share an exit code with a clean slate.
    if report["status"] != STATUS_OK:
        return 1
    counts = report["counts"]
    if counts.get("agreement_disagrees") or counts.get("reads_with_errors"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
