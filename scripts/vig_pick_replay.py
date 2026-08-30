"""Read-only historical replay and attribution over the audited MLB pick corpus.

This module answers two questions the audit deliberately did not: why did
executed picks miss, and why did we pass on winners. It is built ON the audit,
not beside it:

1. Reconciliation, official-result provenance, normalization, and every
   per-candidate classification come from `vig_historical_audit.build_report`
   — the same code path the merged audit runs, not a copy of it. This module
   never re-derives an outcome, re-reads a schedule shape, or re-fetches a
   score.
2. It is read-only end to end. It writes nothing; the one opt-in side effect
   (`--fetch`) delegates to the audit's `fetch_missing_results`, which writes
   only the explicit results cache.
3. "Passed opportunities" means candidates that were PROPOSED and not
   executed. A no-pick control day carries no candidate, so it contributes
   nothing to the passed cohort and is never graded as a bet — and games the
   slate never proposed at all are OUT OF SCOPE of this report entirely. The
   report says so rather than letting the passed-winner count read as "all the
   winners we could have had".

Economics here are SYNTHETIC: a flat one-unit stake at the record's effective
price (paid price when recorded, quoted ask otherwise), gross of fees. Only 8
cards in the corpus carry a real P&L, so a synthetic replay is the only way to
grade cohorts — and every number derived from it is labelled synthetic and
travels with that caveat.

Rule-change candidates are BOUNDED and graded honestly: the rule set is a
fixed, enumerated dictionary (no fitted thresholds), and evaluation is
leave-one-period-out by calendar month — the rule applied to a held-out month
is chosen only on the other months, so nothing is ever tuned and graded on the
same slice. Each cohort's rule set contains its own no-change rule
(`keep_all` / `add_none`), so when no filter beats doing nothing on the
selection months, the honest winner is the status quo.

Usage:
  python scripts/vig_pick_replay.py --picks-dir ~/projects/sports-picks-runtime/.picks
  python scripts/vig_pick_replay.py --results-dir /tmp/mlb-results --fetch
  python scripts/vig_pick_replay.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vig_calibration_report import wilson_ci  # noqa: E402
from vig_historical_audit import (  # noqa: E402
    DEFAULT_MIN_CONSERVATIVE_EDGE,
    MARKET_SUFFIX_RE,
    build_report,
    effective_price,
    fetch_missing_results,
    schedule_paths,
    team_token_matches,
)

DEFAULT_MIN_SAMPLE = 20
# A rule may only be SELECTED for a held-out month when the selection months
# hold at least this many eligible records; below it the fold reports
# insufficient_selection instead of pretending a choice was informed.
DEFAULT_MIN_SELECTION = 15

SYNTHETIC_CAVEATS = (
    "P&L here is SYNTHETIC: flat one-unit stakes at the recorded effective "
    "price (paid price when the card records one, quoted ask otherwise), "
    "gross of fees. Quoted asks are not fills and move against the taker.",
    "The passed cohort covers only candidates the slate PROPOSED and did not "
    "execute. Control days carry no candidates, and games never proposed are "
    "out of scope — this report cannot see the slate's blind spots, only its "
    "declined proposals.",
    "No number below is a claim about model edge at these sample sizes; the "
    "insufficient-sample flags are load-bearing, not decoration.",
)


# ---------------------------------------------------------------------------
# Synthetic economics
# ---------------------------------------------------------------------------


def synthetic_units(record: dict[str, Any]) -> float | None:
    """Flat one-unit outcome at the record's effective price, or None.

    A win at price p returns (1-p)/p units of profit (one unit buys 1/p
    contracts paying 1 each); a loss forfeits the unit; a push returns it.
    Undecided or priceless records have no synthetic result at all — None,
    never zero, because "no evidence" and "broke even" are different facts.
    """
    price, _ = effective_price(record)
    if price is None or price <= 0.0:
        return None
    outcome = record["side_outcome"]
    if outcome == "win":
        return round((1.0 - price) / price, 6)
    if outcome == "loss":
        return -1.0
    if outcome == "push":
        return 0.0
    return None


def eligible_for_replay(record: dict[str, Any]) -> bool:
    """Resolved against an official Final, with a usable price.

    PUSH POLICY, applied consistently everywhere: a priced push IS replayable
    — the stake came back, which is economic evidence worth zero units, not
    absent evidence — so pushes enter the economic sample and the LOPO
    grader. The WIN RATE alone stays strictly wins/(wins+losses); a push says
    nothing about side-picking skill and never enters that denominator.
    `synthetic_units` is the single arbiter: it is None exactly when there is
    nothing to replay.
    """
    return synthetic_units(record) is not None


def cohort_summary(records: list[dict[str, Any]], min_sample: int) -> dict[str, Any]:
    """One cohort's numbers under the push policy on `eligible_for_replay`.

    Pushes are counted (`pushes`, `resolved`) and priced pushes sit inside
    `replayable_with_price`/`synthetic_units` at zero units. The WIN RATE, its
    Wilson interval, and the sufficiency gate stay strictly wins/(wins+losses):
    the claim `min_sample` protects is the rate, and a push carries no
    information about it — counting pushes toward sufficiency would let
    push-heavy cohorts make rate claims on fewer decided records.
    """
    decided = [r for r in records if r["side_outcome"] in ("win", "loss")]
    pushes = sum(1 for r in records if r["side_outcome"] == "push")
    wins = sum(1 for r in decided if r["side_outcome"] == "win")
    replayable = [r for r in records if eligible_for_replay(r)]
    units = round(sum(synthetic_units(r) for r in replayable), 6)
    lo, hi = wilson_ci(wins, len(decided)) if decided else (0.0, 1.0)
    return {
        "candidates": len(records),
        "resolved": len(decided) + pushes,
        "decided": len(decided),
        "wins": wins,
        "losses": len(decided) - wins,
        "pushes": pushes,
        "win_rate": round(wins / len(decided), 6) if decided else None,
        "wilson_95": [round(lo, 6), round(hi, 6)],
        "replayable_with_price": len(replayable),
        "synthetic_units": units,
        "sufficient_for_a_claim": len(decided) >= min_sample,
    }


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


PRICE_BANDS = (
    ("under_0.40", lambda p: p < 0.40),
    ("0.40_to_0.55", lambda p: 0.40 <= p < 0.55),
    ("0.55_and_up", lambda p: p >= 0.55),
)


def price_band(record: dict[str, Any]) -> str | None:
    price, _ = effective_price(record)
    if price is None:
        return None
    for name, member in PRICE_BANDS:
        if member(price):
            return name
    return None


def attribution_matrix(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """disposition x side_outcome, every candidate counted exactly once."""
    matrix: dict[str, dict[str, int]] = {}
    for record in records:
        row = matrix.setdefault(record["disposition"], {})
        row[record["side_outcome"]] = row.get(record["side_outcome"], 0) + 1
    return matrix


def passed_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if r["disposition"] != "executed"]


def _entry_line(record: dict[str, Any]) -> dict[str, Any]:
    price, basis = effective_price(record)
    return {
        "date": record["date"],
        "game": record["game"],
        "side": record.get("resolved_side") or record["side_raw"],
        "disposition": record["disposition"],
        "skip_reason": record["skip_reason"],
        "price": price,
        "price_basis": basis,
        "price_band": price_band(record),
        "stated_probability": record["stated_probability"],
        "confidence": record["confidence"],
        "synthetic_units": synthetic_units(record),
    }


def missed_winners(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Passed candidates whose side won — proposals declined that were right."""
    return [_entry_line(r) for r in passed_records(records) if r["side_outcome"] == "win"]


def executed_losses(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_entry_line(r) for r in records
            if r["disposition"] == "executed" and r["side_outcome"] == "loss"]


def profile(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Where a cohort's records sit: price bands, field presence, reasons.

    Field presence is reported against the cohort size, because on this corpus
    absence is the norm — a profile that only described the present fields
    would read as coverage the data does not have.
    """
    total = len(records)
    bands = Counter(b for r in records if (b := price_band(r)) is not None)
    return {
        "candidates": total,
        "price_band": dict(bands),
        "no_price": total - sum(bands.values()),
        "field_presence": {
            "stated_probability": sum(1 for r in records if r["stated_probability"] is not None),
            "confidence": sum(1 for r in records if r["confidence"] is not None),
            "entry_price": sum(1 for r in records if r["entry_price"] is not None),
            "slate_price": sum(1 for r in records if r["slate_price"] is not None),
            "conservative_probability": sum(
                1 for r in records if r["conservative_probability"] is not None
            ),
        },
        "confidence_values": dict(Counter(
            str(r["confidence"]) for r in records if r["confidence"] is not None
        )),
        "skip_reasons": dict(Counter(
            str(r["skip_reason"]) for r in records if r["skip_reason"] is not None
        )),
        "by_month": dict(Counter(r["date"][:7] for r in records if r["date"])),
    }


# ---------------------------------------------------------------------------
# Side-selection attribution
# ---------------------------------------------------------------------------
#
# Everything in this section reads RECORDED pregame fields — the card's thesis,
# review notes, model numbers, gate reasons, and (on Phase 2 cards) the
# structured opponent case — plus the official row's team NAMES to identify the
# opponent. It never reads the official outcome to construct a rationale:
# outcome fields appear only as labels (`side_outcome`) and to select which
# games belong in the opposing-winners enumeration. Legacy or malformed records
# missing `recorded_rationale` (or any field) degrade to explicit
# `not_recorded` labels, never to a crash or an invented reason.

# Kinds of recorded evidence a card can carry FOR its selected side.
SELECTION_EVIDENCE_KINDS = (
    "recorded_thesis", "recorded_model_probability", "review_notes",
)

# Why the opponent was not selected — a closed set, so a typo becomes a test
# failure rather than a silent new bucket.
OPPONENT_CATEGORIES = (
    "opponent_case_recorded",
    "recorded_case_backed_selected_side",
    "not_recorded",
)

# Classification of a miss where the OPPOSING side won. `risk_gate_declined`
# means a recorded gate declined the (losing) selected side — the gate was not
# the miss, and the winning opponent was never itself proposed.
MISS_CLASSIFICATIONS = (
    "evidence_process_miss",
    "executed_without_recorded_evidence",
    "risk_gate_declined",
    "no_recorded_reason",
)


def _rationale(record: dict[str, Any]) -> dict[str, Any]:
    rationale = record.get("recorded_rationale")
    return rationale if isinstance(rationale, dict) else {}


def selection_evidence_kinds(record: dict[str, Any]) -> list[str]:
    """Which recorded pregame evidence kinds back the selected side."""
    rationale = _rationale(record)
    kinds = []
    if rationale.get("thesis"):
        kinds.append("recorded_thesis")
    if (record.get("stated_probability") is not None
            or record.get("conservative_probability") is not None):
        kinds.append("recorded_model_probability")
    if rationale.get("vig_notes"):
        kinds.append("review_notes")
    return kinds


def opponent_side_of(record: dict[str, Any]) -> tuple[str | None, str | None]:
    """The team the card did NOT pick, and where that name came from.

    The official row is preferred because its names are canonical, and using it
    is identity only — WHICH team is the opponent, never how the game ended.
    Unreconciled records fall back to the card's own matchup when the picked
    side matches exactly one of its teams; anything else is None, labelled.
    """
    resolved = record.get("resolved_side")
    official = record.get("official")
    if isinstance(resolved, str) and isinstance(official, dict):
        away, home = official.get("away"), official.get("home")
        if isinstance(away, str) and away.casefold() == resolved.casefold():
            return home, "official_row"
        if isinstance(home, str) and home.casefold() == resolved.casefold():
            return away, "official_row"
    side = resolved or record.get("side_raw")
    away, home = record.get("away_team"), record.get("home_team")
    if isinstance(side, str) and isinstance(away, str) and isinstance(home, str):
        token = MARKET_SUFFIX_RE.sub("", side).strip()
        away_match = team_token_matches(token, away)
        home_match = team_token_matches(token, home)
        if away_match != home_match:
            return (home if away_match else away), "card_matchup"
    return None, None


def why_opponent_not_selected(record: dict[str, Any]) -> dict[str, str]:
    """Built ONLY from recorded pregame fields; unknown is said out loud."""
    rationale = _rationale(record)
    opponent_case = rationale.get("opponent_shutdown_path")
    kinds = selection_evidence_kinds(record)
    if opponent_case:
        return {
            "category": "opponent_case_recorded",
            "explanation": (
                "the card records an explicit opponent case: " + opponent_case
            ),
        }
    if kinds:
        return {
            "category": "recorded_case_backed_selected_side",
            "explanation": (
                "the recorded pregame case (" + ", ".join(kinds) + ") backs the "
                "selected side; no separate opponent-side case was recorded"
            ),
        }
    return {
        "category": "not_recorded",
        "explanation": (
            "no pregame rationale was recorded on the card; why the opponent "
            "was not selected is unknown"
        ),
    }


def attribution_record(record: dict[str, Any]) -> dict[str, Any]:
    """One candidate's structured, auditable side-selection attribution.

    `side_outcome` and `synthetic_units` are outcome LABELS placed alongside
    the rationale; every field under `selected_evidence` and
    `opponent_evidence` is recorded pregame data, so the rationale half of
    this record is invariant to how the game ended.
    """
    rationale = _rationale(record)
    kinds = selection_evidence_kinds(record)
    opponent, opponent_basis = opponent_side_of(record)
    price, price_basis = effective_price(record)
    opponent_case = rationale.get("opponent_shutdown_path")
    return {
        "date": record.get("date"),
        "game": record.get("game"),
        "selected_side": record.get("resolved_side") or record.get("side_raw"),
        "selected_side_resolution": (
            "official" if record.get("resolved_side") else "card_only"
        ),
        "opponent_side": opponent,
        "opponent_side_basis": opponent_basis,
        "disposition": record.get("disposition"),
        "side_outcome": record.get("side_outcome"),
        "reconciled": record.get("side_outcome") in ("win", "loss", "push"),
        "selected_evidence": {
            "status": "recorded" if kinds else "not_recorded",
            "kinds": kinds,
            "thesis": rationale.get("thesis"),
            "vig_notes": rationale.get("vig_notes"),
            "skip_reason": record.get("skip_reason"),
            "candidate_failure_path": rationale.get("candidate_failure_path"),
            "named_risks": rationale.get("named_risks"),
            "confidence": record.get("confidence"),
            "stated_probability": record.get("stated_probability"),
            "dk_fair_prob": record.get("dk_fair_prob"),
            "conservative_probability": record.get("conservative_probability"),
            "current_ask": record.get("current_ask"),
            "stored_net_edge": record.get("stored_net_edge"),
            "stored_projected_edge": record.get("stored_projected_edge"),
            "price": price,
            "price_basis": price_basis,
        },
        "opponent_evidence": {
            "recorded": bool(opponent_case),
            "opponent_shutdown_path": opponent_case,
            "opponent_field": record.get("opponent_raw"),
            "note": None if opponent_case else (
                "no opposing-side evidence recorded on the card"
            ),
        },
        "why_opponent_not_selected": why_opponent_not_selected(record),
    }


def classify_opposing_winner_miss(record: dict[str, Any]) -> str:
    """Why the winning opponent was missed — from recorded dispositions only."""
    kinds = selection_evidence_kinds(record)
    if record.get("disposition") == "executed":
        return "evidence_process_miss" if kinds else "executed_without_recorded_evidence"
    if (record.get("skip_reason")
            or record.get("disposition") == "review_rejected"
            or _rationale(record).get("vig_notes")):
        return "risk_gate_declined"
    return "no_recorded_reason"


def opposing_winner_cases(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every reconciled candidate whose selected side lost — the opponent won.

    Enumerated separately from the per-candidate comparison records: this list
    answers "which opposing winners did we miss and why", not "how does every
    opponent compare".
    """
    cases = []
    for record in records:
        if record.get("side_outcome") != "loss":
            continue
        opponent, opponent_basis = opponent_side_of(record)
        line = _entry_line(record)
        line["opposing_winner"] = opponent
        line["opposing_winner_basis"] = opponent_basis
        line["miss_classification"] = classify_opposing_winner_miss(record)
        line["recorded_reason"] = (
            record.get("skip_reason") or _rationale(record).get("vig_notes")
        )
        cases.append(line)
    return cases


def side_selection_attribution(records: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [attribution_record(r) for r in records]
    cases = opposing_winner_cases(records)
    return {
        "note": (
            "The slate proposes ONE side per game, so the opponent's "
            "non-selection is structural; these records state what the card "
            "RECORDED about that choice. Rationale fields are pregame data "
            "verbatim — the official outcome is never used to construct a "
            "reason, only to label results and select the opposing-winner "
            "cases. Games the slate never proposed are out of scope entirely."
        ),
        "records": rows,
        "evidence_status_counts": dict(Counter(
            row["selected_evidence"]["status"] for row in rows
        )),
        "why_opponent_counts": dict(Counter(
            row["why_opponent_not_selected"]["category"] for row in rows
        )),
        "opposing_winners": {
            "note": (
                "reconciled candidates whose selected side lost — the opposing "
                "side won these games; enumerated separately from the "
                "per-candidate records above"
            ),
            "cases": cases,
            "classification_counts": dict(Counter(
                case["miss_classification"] for case in cases
            )),
        },
    }


# ---------------------------------------------------------------------------
# Bounded rule candidates, graded leave-one-period-out
# ---------------------------------------------------------------------------


Rule = Callable[[dict[str, Any]], bool]


def _price_under(limit: float) -> Rule:
    def rule(record: dict[str, Any]) -> bool:
        price, _ = effective_price(record)
        return price is not None and price < limit
    return rule


def _price_between(low: float, high: float) -> Rule:
    def rule(record: dict[str, Any]) -> bool:
        price, _ = effective_price(record)
        return price is not None and low <= price < high
    return rule


# Exclusion rules for the EXECUTED cohort: which executed picks to keep.
# `keep_all` is the no-change policy and must stay in the set — when nothing
# beats it on the selection months, the honest recommendation is no change.
EXECUTED_RULES: dict[str, Rule] = {
    "keep_all": lambda record: True,
    "keep_under_0.55": _price_under(0.55),
    "keep_under_0.50": _price_under(0.50),
    "keep_0.40_to_0.55": _price_between(0.40, 0.55),
}

# Inclusion rules for the PASSED cohort: which declined proposals to take.
# `add_none` is that cohort's no-change policy.
PASSED_RULES: dict[str, Rule] = {
    "add_none": lambda record: False,
    "add_under_0.55": _price_under(0.55),
    "add_under_0.50": _price_under(0.50),
    "add_0.40_to_0.55": _price_between(0.40, 0.55),
}


def period_of(record: dict[str, Any]) -> str:
    return (record["date"] or "unknown")[:7]


def rule_units(records: list[dict[str, Any]], rule: Rule) -> tuple[int, float]:
    kept = [r for r in records if rule(r)]
    return len(kept), round(sum(synthetic_units(r) for r in kept), 6)


def in_sample_table(records: list[dict[str, Any]], rules: dict[str, Rule]) -> dict[str, Any]:
    """Every rule scored on the WHOLE cohort — reference only, never a verdict.

    This table is what over-fitting looks like when it wins, which is why it
    is printed next to the held-out result instead of instead of it.
    """
    return {
        name: {"n": n, "synthetic_units": units}
        for name, (n, units) in ((name, rule_units(records, rule)) for name, rule in rules.items())
    }


def leave_one_period_out(
    records: list[dict[str, Any]], rules: dict[str, Rule], min_selection: int,
) -> dict[str, Any]:
    """Grade a rule-selection POLICY, not a rule.

    For each calendar-month period, the rule applied to that month is the one
    with the best synthetic units on all OTHER months (deterministic tiebreak:
    rule name). The held-out months' results are the only ones aggregated. A
    fold whose selection months hold fewer than `min_selection` eligible
    records selects nothing and is reported as insufficient rather than
    letting a coin-flip choice into the aggregate.
    """
    eligible = [r for r in records if eligible_for_replay(r)]
    periods = sorted({period_of(r) for r in eligible})
    folds = []
    held_out_n = 0
    held_out_units = 0.0
    for held in periods:
        selection = [r for r in eligible if period_of(r) != held]
        test = [r for r in eligible if period_of(r) == held]
        fold: dict[str, Any] = {
            "period": held, "n_selection": len(selection), "n_held_out": len(test),
        }
        if len(selection) < min_selection:
            fold["status"] = "insufficient_selection"
            folds.append(fold)
            continue
        scored = {name: rule_units(selection, rule) for name, rule in rules.items()}
        chosen = max(sorted(scored), key=lambda name: scored[name][1])
        n_kept, units = rule_units(test, rules[chosen])
        fold.update({
            "status": "graded",
            "chosen_rule": chosen,
            "chosen_on_selection_units": scored[chosen][1],
            # A "chosen" rule that merely tied and won on name order is a
            # different fact from one that won on the economics — disclose it.
            "selection_ties": sorted(
                name for name in scored
                if name != chosen and scored[name][1] == scored[chosen][1]
            ),
            "held_out_kept": n_kept,
            "held_out_units": units,
        })
        held_out_n += n_kept
        held_out_units += units
        folds.append(fold)
    graded = [f for f in folds if f["status"] == "graded"]
    return {
        "method": (
            "leave-one-calendar-month-out; the rule for each month is chosen "
            "only on the other months, so no record is ever tuned on and "
            "graded on the same slice"
        ),
        "eligible_records": len(eligible),
        "periods": periods,
        "min_selection": min_selection,
        "folds": folds,
        "held_out": {
            "graded_folds": len(graded),
            "insufficient_folds": len(folds) - len(graded),
            "kept": held_out_n,
            "synthetic_units": round(held_out_units, 6),
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def replay_report(
    audit_report: dict[str, Any], min_sample: int, min_selection: int,
) -> dict[str, Any]:
    days = audit_report["days"]
    records = [record for day in days for record in day["candidates"]]
    executed = [r for r in records if r["disposition"] == "executed"]
    passed = passed_records(records)
    control_dates = audit_report["aggregate"]["days"]["control_dates"]

    executed_lopo = leave_one_period_out(executed, EXECUTED_RULES, min_selection)
    passed_lopo = leave_one_period_out(passed, PASSED_RULES, min_selection)

    return {
        "foundation": {
            "source": "vig_historical_audit.build_report — same reconciliation and provenance",
            "execute_dir": audit_report["execute_dir"],
            "results_dir": audit_report["results_dir"],
            "candidates": len(records),
            "reconciled": audit_report["aggregate"]["candidates"]["reconciled_to_official"],
        },
        "controls": {
            "no_pick_control_days": len(control_dates),
            "control_dates": control_dates,
            "note": (
                "A control day proposed no candidate. It contributes nothing "
                "to the passed cohort and is never graded as a bet; the "
                "passed-opportunity numbers below cover declined PROPOSALS only."
            ),
        },
        "attribution_matrix": attribution_matrix(records),
        "side_selection_attribution": side_selection_attribution(records),
        "cohorts": {
            "executed": cohort_summary(executed, min_sample),
            "passed": cohort_summary(passed, min_sample),
        },
        "missed_winners": missed_winners(records),
        "executed_losses": executed_losses(records),
        "profiles": {
            "executed": profile(executed),
            "executed_wins": profile([
                r for r in executed if r["side_outcome"] == "win"
            ]),
            "executed_losses": profile([
                r for r in executed if r["side_outcome"] == "loss"
            ]),
            "passed": profile(passed),
            "missed_winners": profile([
                r for r in passed if r["side_outcome"] == "win"
            ]),
        },
        "rule_candidates": {
            "executed_exclusions": {
                "in_sample_reference_only": in_sample_table(
                    [r for r in executed if eligible_for_replay(r)], EXECUTED_RULES
                ),
                "leave_one_period_out": executed_lopo,
            },
            "passed_inclusions": {
                "in_sample_reference_only": in_sample_table(
                    [r for r in passed if eligible_for_replay(r)], PASSED_RULES
                ),
                "leave_one_period_out": passed_lopo,
            },
        },
        "min_sample_for_a_claim": min_sample,
        "caveats": list(SYNTHETIC_CAVEATS),
    }


def _fmt_units(value: float) -> str:
    return f"{value:+.2f}u"


def render(report: dict[str, Any]) -> str:
    out: list[str] = []
    f = report["foundation"]
    out.append("# MLB historical pick replay & attribution")
    out.append(f"Foundation: {f['source']}")
    out.append(f"Schedules: {f['execute_dir']}; official results: {f['results_dir']}")
    out.append(f"{f['reconciled']}/{f['candidates']} candidates reconciled to an official Final")
    out.append("")

    c = report["controls"]
    out.append("## Controls")
    out.append(f"- {c['no_pick_control_days']} no-pick control days")
    out.append(f"- {c['note']}")
    out.append("")

    out.append("## Attribution: disposition x official outcome")
    for disposition, row in sorted(report["attribution_matrix"].items()):
        cells = ", ".join(f"{k}={v}" for k, v in sorted(row.items()))
        out.append(f"- {disposition}: {cells}")
    out.append("")

    out.append("## Cohorts")
    for name in ("executed", "passed"):
        s = report["cohorts"][name]
        flag = "" if s["sufficient_for_a_claim"] else (
            f"  [INSUFFICIENT SAMPLE — n<{report['min_sample_for_a_claim']}, no claim]"
        )
        pushes = f", {s['pushes']} pushes" if s["pushes"] else ""
        if s["decided"]:
            out.append(
                f"- {name}: {s['wins']}-{s['losses']} = {s['win_rate'] * 100:.1f}% W/L "
                f"(95% CI {s['wilson_95'][0] * 100:.1f}–{s['wilson_95'][1] * 100:.1f}%{pushes}), "
                f"synthetic {_fmt_units(s['synthetic_units'])} over "
                f"{s['replayable_with_price']} priced{flag}"
            )
        else:
            out.append(f"- {name}: no decided candidates{pushes}")
    out.append("")

    ssa = report["side_selection_attribution"]
    out.append("## Side-selection attribution")
    out.append(f"- {ssa['note']}")
    out.append("- selected-side evidence: " + ", ".join(
        f"{k}={v}" for k, v in sorted(ssa["evidence_status_counts"].items())
    ))
    out.append("- why the opponent was not selected: " + ", ".join(
        f"{k}={v}" for k, v in sorted(ssa["why_opponent_counts"].items())
    ))
    ow = ssa["opposing_winners"]
    out.append(f"### Opposing winners we missed ({len(ow['cases'])})")
    out.append(f"- {ow['note']}")
    if ow["classification_counts"]:
        out.append("- classifications: " + ", ".join(
            f"{k}={v}" for k, v in sorted(ow["classification_counts"].items())
        ))
    for case in ow["cases"]:
        winner = case["opposing_winner"] or "(opponent unresolvable)"
        reason = f" — recorded reason: {case['recorded_reason']}" if case["recorded_reason"] else ""
        out.append(
            f"- {case['date']} {case['game']}: {winner} won; picked side "
            f"{case['side']} ({case['disposition']}) — "
            f"{case['miss_classification']}{reason}"
        )
    out.append("")

    out.append(f"## Winners we passed on ({len(report['missed_winners'])})")
    for line in report["missed_winners"]:
        price = f" at {line['price']:.2f}" if line["price"] is not None else ""
        reason = f" — {line['skip_reason']}" if line["skip_reason"] else ""
        out.append(f"- {line['date']} {line['game']} ({line['disposition']}{price}){reason}")
    mw = report["profiles"]["missed_winners"]
    if mw["candidates"]:
        out.append(f"- price bands: {mw['price_band'] or 'none priced'}; no price: {mw['no_price']}")
    out.append("")

    out.append(f"## Executed picks that lost ({len(report['executed_losses'])})")
    el = report["profiles"]["executed_losses"]
    out.append(f"- price bands: {el['price_band'] or 'none priced'}; no price: {el['no_price']}")
    ew = report["profiles"]["executed_wins"]
    out.append(
        f"- executed WINS in the same bands (the denominator the loss bands "
        f"must be read against): {ew['price_band'] or 'none priced'}; "
        f"no price: {ew['no_price']}"
    )
    presence = report["profiles"]["executed"]["field_presence"]
    total = report["profiles"]["executed"]["candidates"]
    out.append(
        "- field presence across ALL executed: "
        + ", ".join(f"{k}={v}/{total}" for k, v in sorted(presence.items()))
    )
    out.append("")

    out.append("## Rule-change candidates (bounded set, graded leave-one-month-out)")
    for label, key in (("executed exclusions", "executed_exclusions"),
                       ("passed inclusions", "passed_inclusions")):
        block = report["rule_candidates"][key]
        lopo = block["leave_one_period_out"]
        out.append(f"### {label}")
        out.append("- in-sample (reference only, NEVER a verdict): " + "; ".join(
            f"{name} n={cell['n']} {_fmt_units(cell['synthetic_units'])}"
            for name, cell in sorted(block["in_sample_reference_only"].items())
        ))
        held = lopo["held_out"]
        out.append(
            f"- held-out: {held['graded_folds']} folds graded, "
            f"{held['insufficient_folds']} insufficient; kept {held['kept']}, "
            f"synthetic {_fmt_units(held['synthetic_units'])}"
        )
        for fold in lopo["folds"]:
            if fold["status"] == "graded":
                tie = (
                    f" [tied with {', '.join(fold['selection_ties'])}; won on name order]"
                    if fold["selection_ties"] else ""
                )
                out.append(
                    f"  - {fold['period']}: chose {fold['chosen_rule']}{tie} on the other "
                    f"{fold['n_selection']} records; held-out kept "
                    f"{fold['held_out_kept']}/{fold['n_held_out']} for "
                    f"{_fmt_units(fold['held_out_units'])}"
                )
            else:
                out.append(
                    f"  - {fold['period']}: INSUFFICIENT SELECTION "
                    f"({fold['n_selection']} < {lopo['min_selection']}) — not graded"
                )
        out.append("")

    out.append("## Caveats (these travel with every number above)")
    for caveat in report["caveats"]:
        out.append(f"- {caveat}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only historical MLB pick replay/attribution report"
    )
    parser.add_argument("--picks-dir", help="the .picks directory (default: $SPORTS_PICKS_ROOT/.picks)")
    parser.add_argument("--results-dir", help="cache of MLB Stats API schedule payloads (default: <picks-dir>/audit-results)")
    parser.add_argument("--since", help="earliest schedule date YYYY-MM-DD")
    parser.add_argument("--until", help="latest schedule date YYYY-MM-DD")
    parser.add_argument("--edge-floor", type=float, default=DEFAULT_MIN_CONSERVATIVE_EDGE,
                        help="conservative edge floor passed through to the audit")
    parser.add_argument("--min-sample", type=int, default=DEFAULT_MIN_SAMPLE,
                        help=(f"minimum decided records before a rate is a claim; also passed "
                              f"through as the audit's calibration bucket threshold "
                              f"(default {DEFAULT_MIN_SAMPLE})"))
    parser.add_argument("--min-selection", type=int, default=DEFAULT_MIN_SELECTION,
                        help=f"minimum selection records before a fold is graded (default {DEFAULT_MIN_SELECTION})")
    parser.add_argument("--fetch", action="store_true",
                        help="opt-in: populate missing results-cache dates via the audit's fetch helper")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = parser.parse_args(argv)

    # Fail closed on nonsense thresholds, mirroring vig_historical_audit's CLI:
    # a negative --min-sample marks every cohort sufficient, --min-selection 0
    # grades folds on zero selection records, and an out-of-range edge floor
    # silently changes which candidates the audit even considers.
    for flag, value in (("--since", args.since), ("--until", args.until)):
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parser.error(f"{flag} must be YYYY-MM-DD")
    if not 0 < args.edge_floor < 1:
        parser.error("--edge-floor must be between 0 and 1")
    if args.min_sample < 1:
        parser.error("--min-sample must be at least 1")
    if args.min_selection < 1:
        parser.error("--min-selection must be at least 1")

    picks_dir = args.picks_dir or (
        os.environ.get("SPORTS_PICKS_ROOT") and
        str(Path(os.environ["SPORTS_PICKS_ROOT"]) / ".picks")
    )
    if not picks_dir:
        print("no --picks-dir and no SPORTS_PICKS_ROOT", file=sys.stderr)
        return 2
    execute_dir = Path(picks_dir).expanduser() / "execute"
    results_dir = (
        Path(args.results_dir).expanduser() if args.results_dir
        else Path(picks_dir).expanduser() / "audit-results"
    )

    if args.fetch:
        dates = []
        for path in schedule_paths(execute_dir, args.since, args.until):
            dates.append(path.name[:10])
        written = fetch_missing_results(dates, results_dir)
        print(f"fetched {len(written)} result payloads", file=sys.stderr)

    audit_report = build_report(
        execute_dir, results_dir, args.edge_floor, args.since, args.until,
        0.05, args.min_sample,
    )
    report = replay_report(audit_report, args.min_sample, args.min_selection)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
