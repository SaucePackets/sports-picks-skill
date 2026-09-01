#!/usr/bin/env python3
"""Measure what the card did with every scheduled game, from recorded state only.

``mlb_model_eval_dataset`` builds the rows the deployment gate consumes. It is
an evaluator's input, so it SKIPS a read it cannot score, and it is right to:
an evaluation set with an imputed probability in it is worse than a smaller
one. But a skip list is not a measurement. The question Rebecca's lane asks —
*why are we not getting picks, and are we right when we do* — is answered by
the games that produced no row at all just as much as by the ones that did.

So this module emits **one row per scheduled game, always**, and a game whose
read carries no usable number still gets a row that says which field is
missing and what reason the run gave for it. The denominator is the point.
Nothing here is imputed, defaulted, or completed from a neighbouring game.

**Recorder rows only.** The 2026-08-11..08-31 replay (PR #75) reconstructed its
inputs from slate prose, and its entire graded set is ``faithful_inputs:
false``. Folding a prose-reconstructed row and a recorded row into one table is
the fidelity blend that was blocker 1 on that very PR wearing a different hat.
The replay is history for this lane, not population. Ruled by Rebecca
2026-09-01 (D2).

**Ask-based, and it says so.** Every price in the deployed pipeline is an ask
or an executed fill. There is no bid, no midpoint and no last-trade field
anywhere, so BBO, mid, and traded status are reported ``unavailable (never
captured)`` on every row rather than approximated from the ask. Capturing them
is an order-book fetch inside the slate run — a runtime behaviour change, and
explicitly out of this read-only lane (D1). It wants its own slice.

**No blended headline.** Every metric names its bucket and its n. There is no
combined Brier, log-loss, record, or calibration key anywhere in the output,
because the replay's blocker 1 was exactly that: half the outcome record was
drawn from a different fidelity of input and the headline did not say so. The
suite pins that on the JSON report *and* on the rendered Markdown, not on
``aggregate`` alone — asserting it one level below where a headline would be
written left the mutation that writes one green.

**A date that produced nothing says which nothing it is.** ``report`` carries a
schedule-level audit beside the per-read counters: dates used, dates whose
schedule was read but carried no usable ``slate_denominator`` (named, with the
reason), reads dropped for naming a game outside the denominator, and rows.
Without it a whole date drops out as ``rows: 0`` and reads as an empty slate
rather than an unopened one.

**Read-only in the strongest available sense.** No network at all — finals come
from a cache directory or the row says it has no final. Nothing is written
except the report the caller asks for.

Usage:
  python3 scripts/mlb_measurement_lane.py \\
      --schedules .picks/execute --finals .picks/audit-results \\
      --start 2026-09-01 --until 2026-09-21 \\
      --out-json measurement.json --out-markdown measurement.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Consulted, never restated. Four review rounds in this repo have been spent on
# copies of one rule drifting apart; a reporting layer that decided for itself
# what a usable probability is, which side gets scored, or what a transposed
# read looks like would be the fifth.
from mlb_game_reads import (  # noqa: E402
    ALLOWED_BLOCKERS,
    MODEL_TRAIL_FIELDS,
    REQUIRED_ORIGINAL_GATES,
    _is_probability,
    denominator_games,
    validate_read,
)
from mlb_model_eval_dataset import (  # noqa: E402
    EVALUATED_SIDE,
    _final_by_game_pk,
    _transposition,
    dates_in_range,
)
from numeric_util import is_finite_number  # noqa: E402

# The market-only fallback's model identity. Defined here rather than imported
# for the same reason mlb_game_reads defines its own coherence tolerance: a
# read-only reporting module must not drag the execution-path model module into
# its import closure. A test pins this string equal to
# mlb_probability_model.MARKET_MODEL_VERSION, because two copies of one name
# agree only until one of them changes.
MARKET_ONLY_MODEL_VERSION = "vig-mlb-market-v1"

# What kind of record the row rests on. Closed and zero-filled: a bucket that
# never occurred prints 0 rather than vanishing, because a reader cannot
# otherwise tell a constant axis from an impossible one.
FIDELITY_BUCKETS = (
    "recorded_handicap",
    "no_handicap",
    "unusable_read",
    "no_read",
)

# An axis independent of fidelity. A full handicap recorded under the
# market-only fallback is a recorded handicap whose raw probability is DK's own
# fair — measuring it against DK would be measuring DK against itself, so the
# split has to survive into every aggregate.
SOURCE_QUALITIES = (
    "market_only_fallback",
    "non_market_model",
    "not_applicable",
)

# Rails that mean a required input never arrived. A gate cannot be said to have
# refused a game it was never able to look at, so these take precedence over
# every handicapping rail when a read names both — stated here rather than left
# to the order the run happened to write them in.
PROCESS_RAILS = frozenset(
    {
        "no_dk_price",
        "no_polymarket_market",
        "incomplete_input_data",
        "game_already_started",
    }
)

# Rails that are a decision about the game rather than a missing input.
HANDICAPPING_RAILS = (
    frozenset(REQUIRED_ORIGINAL_GATES) | frozenset(ALLOWED_BLOCKERS) | {"park_environment_cap"}
)

VOLUME_RAILS = frozenset({"daily_volume_cap"})

# Why we did not bet this game. Closed, zero-filled, and mutually exclusive by
# the precedence in ``refusal_attribution``.
REFUSAL_ATTRIBUTIONS = (
    "not_refused",
    "process_missing_input",
    "gate_handicapping_rail",
    "gate_volume_cap",
    "gate_candidate_from_inferred_input",
    "unclassified_rail",
)

# What the result says about the read. ``unattributed_no_game_script`` is the
# honest answer for every row that has both a probability and a final: Rebecca's
# classes 1 and 2 — the read was wrong on the merits, versus the case held and
# the game did not — are not separable from a scoreline. Separating them needs
# game script and decisive scoring events, which nothing in this pipeline
# records. A row is labelled undecided rather than sorted into whichever class
# looks plausible.
OUTCOME_ATTRIBUTIONS = (
    "unattributed_no_game_script",
    "pending_no_final",
    "no_probability_recorded",
    "refused_transposed_read",
)

# Price facts the pipeline has never captured. Named individually so the report
# distinguishes "we looked and there is none" from "nobody thought about it".
UNCAPTURED_PRICE_FIELDS = ("polymarket_bbo", "polymarket_mid", "polymarket_traded")
UNCAPTURED_PRICE_REASON = (
    "never captured; every price in the deployed pipeline is an ask or an executed fill, "
    "and order-book capture is a runtime change held out of this read-only lane (D1)"
)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _side_value(entry: dict[str, Any], field: str) -> Any:
    value = entry.get(field)
    return value.get(EVALUATED_SIDE) if isinstance(value, dict) else None


def _unavailable_reason(entry: dict[str, Any], field: str) -> str | None:
    unavailable = entry.get("unavailable")
    if not isinstance(unavailable, dict):
        return None
    reason = unavailable.get(field)
    return reason if isinstance(reason, str) and reason.strip() else None


def _availability(entry: dict[str, Any], field: str, usable: bool) -> dict[str, Any]:
    """One field's value and where it came from, or why there is not one.

    The three states are deliberately distinct. A recorded value is
    ``recorded``. An absent value the run explained is ``unavailable`` and
    carries the run's own words. An absent value nobody explained is
    ``unexplained_absence`` — which the recorder's validator already refuses at
    slate time, so seeing one here means a read reached this report without
    passing that gate, and the report should say so rather than fold it in with
    the honest absences.
    """
    if usable:
        return {"value": _side_value(entry, field), "provenance": "recorded"}
    reason = _unavailable_reason(entry, field)
    if reason is not None:
        return {"value": None, "provenance": "unavailable", "reason": reason}
    return {
        "value": None,
        "provenance": "unexplained_absence",
        "reason": "absent, and the read gives no reason; the recorder's validator refuses this",
    }


def _scalar_availability(entry: dict[str, Any], field: str, usable: bool) -> dict[str, Any]:
    if usable:
        return {"value": entry.get(field), "provenance": "recorded"}
    reason = _unavailable_reason(entry, field)
    if reason is not None:
        return {"value": None, "provenance": "unavailable", "reason": reason}
    return {
        "value": None,
        "provenance": "unexplained_absence",
        "reason": "absent, and the read gives no reason; the recorder's validator refuses this",
    }


def _has_full_trail(entry: dict[str, Any]) -> bool:
    """Whether the away side carries a complete, usable model trail.

    The recorder already enforces all-or-nothing on the four trail fields and
    the per-side coherence between them, so this only has to ask whether the
    fields are there and usable on the side being scored.
    """
    for field in ("raw_probability", "conservative_probability"):
        if not _is_probability(_side_value(entry, field)):
            return False
    haircut = entry.get("uncertainty_haircut")
    # is_finite_number, not a local math.isfinite: that call is NOT total —
    # it raises OverflowError on an int too large to convert to a float, and
    # json.loads parses an arbitrarily long integer literal straight off a
    # card. A predicate that can itself crash is not a guard.
    if not is_finite_number(haircut) or haircut < 0:
        return False
    version = entry.get("model_version")
    return isinstance(version, str) and bool(version.strip())


def _fidelity(entry: dict[str, Any] | None, read_errors: list[str]) -> str:
    if entry is None:
        return "no_read"
    if read_errors:
        return "unusable_read"
    if _has_full_trail(entry):
        return "recorded_handicap"
    return "no_handicap"


def _source_quality(entry: dict[str, Any] | None, fidelity: str) -> str:
    if fidelity != "recorded_handicap" or entry is None:
        return "not_applicable"
    version = str(entry.get("model_version", "")).strip()
    return "market_only_fallback" if version == MARKET_ONLY_MODEL_VERSION else "non_market_model"


def refusal_attribution(entry: dict[str, Any] | None, fidelity: str) -> tuple[str, list[str]]:
    """Why we did not bet this game, from recorded state and nothing else.

    Precedence is stated rather than incidental: a missing input outranks a
    handicapping rail, because a gate cannot be said to have refused a game it
    was never able to price. A read naming both ``no_dk_price`` and
    ``price_discipline`` is a process failure that happens to have also written
    down a gate.

    ``gate_candidate_from_inferred_input`` — Rebecca's fourth class, an apparent
    candidate that existed only because an input was inferred — is structurally
    unreachable in this population: recorder rows have no inferred inputs, only
    recorded ones and explained absences. It stays in the closed set and prints
    zero, because a category that is impossible here and a category that merely
    did not occur are different facts and the reader is entitled to both.
    """
    if entry is None:
        return "process_missing_input", ["no game_reads entry for a scheduled game"]
    if fidelity == "unusable_read":
        return "process_missing_input", ["the read does not pass the recorder's own validator"]
    disposition = entry.get("disposition")
    if disposition in ("candidate", "lineup_watchlist"):
        return "not_refused", []

    rails = entry.get("refusing_rails")
    rails = [r for r in rails if isinstance(r, str)] if isinstance(rails, list) else []
    unavailable = entry.get("unavailable")
    missing_fields = sorted(unavailable) if isinstance(unavailable, dict) else []

    process_named = sorted(set(rails) & PROCESS_RAILS)
    if process_named or missing_fields:
        evidence = [f"rail {rail}" for rail in process_named]
        evidence += [
            f"{field}: {_unavailable_reason(entry, field)}" for field in missing_fields
        ]
        return "process_missing_input", evidence
    volume_named = sorted(set(rails) & VOLUME_RAILS)
    if volume_named:
        return "gate_volume_cap", [f"rail {rail}" for rail in volume_named]
    gate_named = sorted(set(rails) & HANDICAPPING_RAILS)
    if gate_named:
        return "gate_handicapping_rail", [f"rail {rail}" for rail in gate_named]
    return "unclassified_rail", [f"rail {rail}" for rail in sorted(rails)] or [
        "the read names no rail this module recognises"
    ]


def _outcome(
    entry: dict[str, Any] | None, final: dict[str, Any] | None, fidelity: str
) -> dict[str, Any]:
    """The away side's result, or the reason there is not one.

    The side join is made explicitly and not assumed. Probabilities descend
    from the ESPN scoreboard and the outcome from StatsAPI; a transposed read
    otherwise produces a perfectly clean row scoring one club's handicap
    against the other club's result. That detection is the dataset builder's,
    consulted here rather than re-derived.
    """
    if final is None:
        return {"outcome": None, "provenance": "unavailable", "reason": "no Final result cached"}
    winner = final.get("winner")
    away_name, home_name = final.get("away"), final.get("home")
    if not isinstance(winner, str) or winner not in (away_name, home_name):
        return {
            "outcome": None,
            "provenance": "unavailable",
            "reason": "the cached final carries no winner on either side",
        }
    if entry is not None and fidelity != "no_read":
        swap = _transposition(entry, away_name, home_name)
        if swap is not None:
            return {"outcome": None, "provenance": "refused", "reason": swap}
    return {
        "outcome": 1 if winner == away_name else 0,
        "provenance": "recorded",
        "winner": winner,
        "away": away_name,
        "home": home_name,
    }


def _input_availability(entry: dict[str, Any] | None, rails: list[str], blocker: str) -> str:
    """Starter and lineup availability, said no wider than the evidence.

    Nothing in a read states that a starter was announced or a lineup posted.
    What exists is the rail the run named when one was NOT. So the answer is
    ``pending`` when the rail is present and ``not_stated`` when it is absent —
    never ``confirmed``. The absence of a complaint is not a confirmation, and
    reporting one as the other would manufacture exactly the kind of input
    provenance this lane exists to stop inventing.
    """
    if entry is None:
        return "no_read"
    return "pending" if blocker in rails else "not_stated"


def build_row(
    date: str,
    game: dict[str, Any],
    entry: dict[str, Any] | None,
    entry_index: int | None,
    finals: dict[int, dict[str, Any]],
    captured_at_utc: dict[str, Any],
) -> dict[str, Any]:
    """One row for one scheduled game. Always a row, never a skip."""
    game_pk = game.get("game_pk")
    read_errors = validate_read(entry, entry_index or 0) if entry is not None else []
    fidelity = _fidelity(entry, read_errors)
    source_quality = _source_quality(entry, fidelity)
    final = finals.get(game_pk) if isinstance(game_pk, int) else None

    rails: list[str] = []
    if entry is not None and isinstance(entry.get("refusing_rails"), list):
        rails = [r for r in entry["refusing_rails"] if isinstance(r, str)]

    row: dict[str, Any] = {
        "date": date,
        "game_pk": game_pk,
        "event_id": game.get("event_id"),
        "away": game.get("away"),
        "home": game.get("home"),
        # Fixed, mechanical, and independent of what the model thought. Both
        # sides would double n with perfectly anti-correlated rows and break
        # the independence every metric here assumes; "the side the model
        # favoured" would re-introduce the selection bias this lane exists to
        # remove. Inherited from the dataset builder rather than re-decided.
        "side": EVALUATED_SIDE,
        "fidelity": fidelity,
        "source_quality": source_quality,
        "disposition": entry.get("disposition") if entry is not None else None,
        "refusing_rails": rails,
        "captured_at_utc": captured_at_utc,
        "starter_availability": _input_availability(entry, rails, "starter_unannounced"),
        "lineup_availability": _input_availability(entry, rails, "lineups_unconfirmed"),
    }
    if read_errors:
        row["read_errors"] = read_errors

    if entry is None:
        empty = {
            "value": None,
            "provenance": "unavailable",
            "reason": "the slate recorded no read for this scheduled game",
        }
        for field in ("dk_fair_prob", "polymarket_ask", "net_edge", *MODEL_TRAIL_FIELDS):
            row[field] = dict(empty)
    else:
        for field in ("dk_fair_prob", "polymarket_ask", "raw_probability",
                      "conservative_probability"):
            row[field] = _availability(entry, field, _is_probability(_side_value(entry, field)))
        net_edge = _side_value(entry, "net_edge")
        row["net_edge"] = _availability(entry, "net_edge", is_finite_number(net_edge))
        haircut = entry.get("uncertainty_haircut")
        row["uncertainty_haircut"] = _scalar_availability(
            entry, "uncertainty_haircut", is_finite_number(haircut) and haircut >= 0
        )
        version = entry.get("model_version")
        row["model_version"] = _scalar_availability(
            entry, "model_version", isinstance(version, str) and bool(version.strip())
        )

    for field in UNCAPTURED_PRICE_FIELDS:
        row[field] = {
            "value": None,
            "provenance": "never_captured",
            "reason": UNCAPTURED_PRICE_REASON,
        }

    row["result"] = _outcome(entry, final, fidelity)
    label, evidence = refusal_attribution(entry, fidelity)
    row["refusal_attribution"] = {"label": label, "evidence": evidence}
    row["outcome_attribution"] = _outcome_attribution(row)
    return row


def _outcome_attribution(row: dict[str, Any]) -> dict[str, Any]:
    result = row["result"]
    if result["provenance"] == "refused":
        return {"label": "refused_transposed_read", "reason": result["reason"]}
    if result["outcome"] is None:
        return {"label": "pending_no_final", "reason": result.get("reason", "")}
    if row["fidelity"] != "recorded_handicap":
        return {
            "label": "no_probability_recorded",
            "reason": f"fidelity is {row['fidelity']}; there is no handicap to judge",
        }
    conservative = row["conservative_probability"]["value"]
    raw = row["raw_probability"]["value"]
    won = result["outcome"] == 1
    return {
        "label": "unattributed_no_game_script",
        "reason": (
            "a scoreline cannot separate a wrong read from an adverse result; "
            "this lane records both and refuses to sort them"
        ),
        # Evidence beside the label, deliberately NOT a classification. It says
        # which way the read leaned and what happened, and stops there.
        "read_favoured_away": raw > 0.5 if raw is not None else None,
        "away_won": won,
        "conservative_probability": conservative,
    }


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Brier, log loss and record over rows that carry both a probability and a
    result. ``None`` when no row in the bucket does.

    No clamping. The recorder refuses a probability that is not strictly inside
    (0, 1), so log loss cannot reach an infinity here — and clamping would
    quietly turn an impossible value into a scoreable one, which is the
    imputation this lane refuses everywhere else.
    """
    scored = [
        (r["conservative_probability"]["value"], r["result"]["outcome"])
        for r in rows
        if r["fidelity"] == "recorded_handicap"
        and r["result"]["outcome"] is not None
        and _is_probability(r["conservative_probability"]["value"])
    ]
    if not scored:
        return None
    brier = statistics.fmean((p - o) ** 2 for p, o in scored)
    log_loss = statistics.fmean(
        -(o * math.log(p) + (1 - o) * math.log(1 - p)) for p, o in scored
    )
    wins = sum(o for _, o in scored)
    return {
        "n": len(scored),
        "away_wins": wins,
        "away_losses": len(scored) - wins,
        "brier": round(brier, 6),
        "log_loss": round(log_loss, 6),
        "mean_conservative_probability": round(statistics.fmean(p for p, _ in scored), 6),
    }


def _market_comparison(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The model against DK, over rows where both are recorded, or ``None``.

    Its n is reported separately from the metric block's n on purpose: the two
    populations differ whenever a read carries a handicap and no DK line, and a
    comparison quietly computed over a smaller set than the record beside it is
    the shape that made the replay's headline wrong.
    """
    paired = [
        (
            r["conservative_probability"]["value"],
            r["dk_fair_prob"]["value"],
            r["result"]["outcome"],
        )
        for r in rows
        if r["fidelity"] == "recorded_handicap"
        and r["result"]["outcome"] is not None
        and _is_probability(r["conservative_probability"]["value"])
        and _is_probability(r["dk_fair_prob"]["value"])
    ]
    if not paired:
        return None
    deltas = [model - dk for model, dk, _ in paired]
    return {
        "n": len(paired),
        "model_brier": round(statistics.fmean((p - o) ** 2 for p, _, o in paired), 6),
        "dk_brier": round(statistics.fmean((d - o) ** 2 for _, d, o in paired), 6),
        "median_model_minus_dk": round(statistics.median(deltas), 6),
        "model_below_dk": sum(1 for delta in deltas if delta < 0),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and metrics, per bucket, with no combined key anywhere.

    There is deliberately no top-level Brier, record, or calibration number in
    this output. On PR #75 a headline drawn from one fidelity of input without
    saying so was the blocking finding; the fix there was to make the combined
    key impossible to write rather than to remember not to write it, and the
    same applies here.
    """
    coverage = {bucket: 0 for bucket in FIDELITY_BUCKETS}
    quality = {q: 0 for q in SOURCE_QUALITIES}
    refusals = {label: 0 for label in REFUSAL_ATTRIBUTIONS}
    outcomes = {label: 0 for label in OUTCOME_ATTRIBUTIONS}
    for row in rows:
        coverage[row["fidelity"]] += 1
        quality[row["source_quality"]] += 1
        refusals[row["refusal_attribution"]["label"]] += 1
        outcomes[row["outcome_attribution"]["label"]] += 1

    buckets = []
    keys = sorted(
        {
            (
                row["fidelity"],
                row["source_quality"],
                str(row.get("model_version", {}).get("value") or ""),
            )
            for row in rows
        }
    )
    for fidelity, source_quality, model_version in keys:
        member = [
            row
            for row in rows
            if row["fidelity"] == fidelity
            and row["source_quality"] == source_quality
            and str(row.get("model_version", {}).get("value") or "") == model_version
        ]
        buckets.append(
            {
                "fidelity": fidelity,
                "source_quality": source_quality,
                "model_version": model_version or None,
                "rows": len(member),
                "metrics": _metrics(member),
                "market_comparison": _market_comparison(member),
            }
        )
    return {
        "coverage_by_fidelity": coverage,
        "rows_by_source_quality": quality,
        "refusal_attribution": refusals,
        "outcome_attribution": outcomes,
        "buckets": buckets,
    }


def ranked_process_fixes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Process failures ranked by how many scheduled games they cost.

    Ranked by count, and each one names the specific rail or field rather than
    a category, because "fix the process" is not a fix and "the DK line was
    missing on eleven games" is.
    """
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for row in rows:
        attribution = row["refusal_attribution"]
        if attribution["label"] != "process_missing_input":
            continue
        for item in attribution["evidence"]:
            key = item.split(":")[0].strip()
            counts[key] = counts.get(key, 0) + 1
            examples.setdefault(key, f"{row['date']} {row['away']} at {row['home']}: {item}")
    return [
        {"cause": cause, "games": count, "example": examples[cause]}
        for cause, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def load_schedules(
    roots: list[Path], dates: list[str]
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Schedules for the requested dates, deduped by content digest.

    **One dedup policy, stated in the output.** Open finding #4 on the replay
    was two dedup policies in one report — a cross-document metric that deduped
    and population denominators that did not — so the policy here is single and
    printed: byte-identical copies of a date's schedule across roots collapse
    to one, and a date whose roots hold DIFFERENT schedules is EXCLUDED and
    named. Not repaired, excluded: choosing between two disagreeing captures of
    the same slate is exactly the decision the replay could not make on
    2026-08-22, and inventing an answer here would be worse than reporting the
    gap.
    """
    found: dict[str, dict[str, list[str]]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for root in roots:
        for date in dates:
            path = root / f"{date}-schedule.json"
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                found.setdefault(date, {}).setdefault("unreadable", []).append(str(path))
                continue
            if not isinstance(payload, dict):
                found.setdefault(date, {}).setdefault("unreadable", []).append(str(path))
                continue
            digest = _digest(raw)
            found.setdefault(date, {}).setdefault(digest, []).append(str(path))
            payloads[f"{date}:{digest}"] = payload

    schedules: list[tuple[str, dict[str, Any]]] = []
    excluded: list[dict[str, Any]] = []
    missing: list[str] = []
    duplicates_collapsed = 0
    for date in dates:
        seen = found.get(date)
        if not seen:
            missing.append(date)
            continue
        unreadable = seen.pop("unreadable", None)
        digests = sorted(seen)
        if len(digests) > 1:
            excluded.append(
                {
                    "date": date,
                    "reason": "two roots hold different schedules for this date; "
                    "choosing between disagreeing captures is not this lane's call",
                    "digests": {d: seen[d] for d in digests},
                }
            )
            continue
        if not digests:
            excluded.append(
                {"date": date, "reason": "every schedule file for this date was unreadable",
                 "paths": unreadable or []}
            )
            continue
        duplicates_collapsed += len(seen[digests[0]]) - 1
        schedules.append((date, payloads[f"{date}:{digests[0]}"]))
        if unreadable:
            excluded.append(
                {
                    "date": date,
                    "reason": "used the readable copy; these copies could not be read",
                    "paths": unreadable,
                }
            )
    return schedules, {
        "policy": "byte-identical copies of a date's schedule collapse to one; a date whose "
        "roots disagree is excluded and named, never merged or preferred",
        "dates_requested": len(dates),
        "dates_used": len(schedules),
        "dates_with_no_schedule": missing,
        "duplicate_copies_collapsed": duplicates_collapsed,
        "excluded": excluded,
    }


def load_finals(directory: Path | None, dates: list[str]) -> dict[str, dict[int, dict[str, Any]]]:
    """Finals from a cache directory only. This lane never touches the network."""
    finals: dict[str, dict[int, dict[str, Any]]] = {}
    if directory is None:
        return finals
    for date in dates:
        path = directory / f"{date}.json"
        if not path.is_file():
            continue
        try:
            finals[date] = _final_by_game_pk(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return finals


def _no_denominator_reason(schedule: dict[str, Any]) -> str:
    """Why this schedule produced no roster. Classification only.

    Called only when :func:`denominator_games` has already returned nothing, so
    it never decides WHICH games count — that stays the recorder's function.
    It only says which of the three ways of having none this is, because
    "malformed" and "the slate genuinely had no games" are different findings.
    """
    denominator = schedule.get("slate_denominator")
    if not isinstance(denominator, dict):
        return "the schedule carries no slate_denominator object"
    if not isinstance(denominator.get("games"), list):
        return "the slate_denominator carries no games list"
    return "the slate_denominator lists zero games"


def build_rows(
    schedules: list[tuple[str, dict[str, Any]]],
    finals_by_date: dict[str, dict[int, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One row per scheduled game, over every date that survived dedup.

    Returns the rows **and a schedule-level audit**, because the per-read
    counters cannot see the two ways a whole date can vanish here:

    * a date whose file was read but whose ``slate_denominator`` is missing or
      malformed contributes no rows. ``denominator_games`` returns ``[]`` for
      that case with no raise and no log, and on the corpus that exists today
      that is *every* historical date. Reported as ``rows: 0`` with the date
      still counted as used, the page reads "we measured these slates and found
      nothing in them" when the truth is "we never opened them".
    * a ``game_reads`` entry whose ``game_pk`` is not in the denominator is
      dropped, correctly — the denominator is the roster the recorder
      cross-checks against a fresh scan, and a run must not be able to add rows
      to its own population. But the recorder's own validator calls exactly
      that an error, so an orphaned read reaching here is evidence the slate
      was written unvalidated. Dropping it is right; dropping it silently is
      the same defect one level down.

    Both are named, not merely counted, for the same reason ``unusable_read``
    is its own fidelity bucket: a record that reached this report without
    passing the recorder's gate is itself the finding.
    """
    rows: list[dict[str, Any]] = []
    dates_used: list[str] = []
    no_denominator: list[dict[str, Any]] = []
    orphaned: list[dict[str, Any]] = []
    for date, schedule in schedules:
        dates_used.append(date)
        denominator = schedule.get("slate_denominator")
        captured = {
            "value": denominator.get("fetched_at_utc") if isinstance(denominator, dict) else None,
            # Schedule-level, and said so. No read carries its own capture time,
            # so this is the roster's fetch time and not a per-game observation.
            "provenance": "slate_denominator.fetched_at_utc (schedule-level, not per-game)",
        }
        if captured["value"] is None:
            captured = {
                "value": None,
                "provenance": "unavailable",
                "reason": "the schedule records no denominator fetch time",
            }
        reads = schedule.get("game_reads")
        by_pk: dict[int, tuple[int, dict[str, Any]]] = {}
        if isinstance(reads, list):
            for index, entry in enumerate(reads):
                if isinstance(entry, dict) and isinstance(entry.get("game_pk"), int):
                    by_pk.setdefault(entry["game_pk"], (index, entry))
        finals = finals_by_date.get(date) or {}
        games = denominator_games(schedule)
        if not games:
            no_denominator.append({"date": date, "reason": _no_denominator_reason(schedule)})
        rostered = set()
        for game in games:
            if not isinstance(game, dict):
                continue
            rostered.add(game.get("game_pk"))
            index_entry = by_pk.get(game.get("game_pk"))
            index, entry = index_entry if index_entry is not None else (None, None)
            rows.append(build_row(date, game, entry, index, finals, dict(captured)))
        orphaned += [
            {"date": date, "game_pk": game_pk}
            for game_pk in sorted(by_pk)
            if game_pk not in rostered
        ]
    rows.sort(key=lambda r: (r["date"], r["game_pk"] if r["game_pk"] is not None else 0))
    audit = {
        "policy": "a date read but never opened is named, not counted as zero games; a read "
        "outside the denominator is dropped and named, never allowed to add to its own "
        "population",
        "dates_used": len(dates_used),
        "dates_with_no_usable_denominator": no_denominator,
        "orphaned_reads": orphaned,
        "rows": len(rows),
    }
    return rows, audit


def report(
    rows: list[dict[str, Any]], dedup: dict[str, Any], schedule_audit: dict[str, Any]
) -> dict[str, Any]:
    return {
        "population": "game_reads recorder rows only; the 2026-08 prose replay is history for "
        "this lane, not population (D2)",
        "side": EVALUATED_SIDE,
        "network": "none; finals are read from cache or the row says it has no final",
        "uncaptured_price_fields": {
            field: UNCAPTURED_PRICE_REASON for field in UNCAPTURED_PRICE_FIELDS
        },
        "dedup": dedup,
        "schedule_audit": schedule_audit,
        "rows": len(rows),
        "aggregates": aggregate(rows),
        "ranked_process_fixes": ranked_process_fixes(rows),
    }


def _count_table(title: str, counts: dict[str, int]) -> list[str]:
    lines = [f"### {title}", "", "| bucket | games |", "| --- | --- |"]
    lines += [f"| `{key}` | {value} |" for key, value in counts.items()]
    lines.append("")
    return lines


def markdown(payload: dict[str, Any]) -> str:
    """The report as prose. Every number carries its bucket and its n."""
    aggregates = payload["aggregates"]
    lines = [
        "# MLB measurement lane",
        "",
        f"Population: {payload['population']}",
        "",
        f"Rows: **{payload['rows']}** — one per scheduled game, always the "
        f"`{payload['side']}` side.",
        "",
        f"Network: {payload['network']}.",
        "",
        "## Dedup policy",
        "",
        payload["dedup"]["policy"] + ".",
        "",
        f"Dates requested {payload['dedup']['dates_requested']}, used "
        f"{payload['dedup']['dates_used']}, duplicate copies collapsed "
        f"{payload['dedup']['duplicate_copies_collapsed']}.",
        "",
    ]
    for item in payload["dedup"]["excluded"]:
        lines.append(f"- Excluded `{item['date']}`: {item['reason']}")
    if payload["dedup"]["excluded"]:
        lines.append("")
    if payload["dedup"]["dates_with_no_schedule"]:
        lines.append(
            "Dates with no schedule file: "
            + ", ".join(f"`{d}`" for d in payload["dedup"]["dates_with_no_schedule"])
        )
        lines.append("")

    audit = payload["schedule_audit"]
    lines += [
        "## Schedules opened",
        "",
        audit["policy"] + ".",
        "",
        f"Dates used {audit['dates_used']}, of which "
        f"{len(audit['dates_with_no_usable_denominator'])} produced no roster; rows "
        f"{audit['rows']}.",
        "",
    ]
    for item in audit["dates_with_no_usable_denominator"]:
        lines.append(f"- No roster `{item['date']}`: {item['reason']}")
    if audit["dates_with_no_usable_denominator"]:
        lines.append("")
    if audit["orphaned_reads"]:
        lines.append(
            f"Reads dropped for naming a game the denominator does not list: "
            f"{len(audit['orphaned_reads'])} — "
            + ", ".join(
                f"`{item['date']}` game_pk {item['game_pk']}" for item in audit["orphaned_reads"]
            )
        )
        lines.append("")

    lines += _count_table("Coverage by fidelity", aggregates["coverage_by_fidelity"])
    lines += _count_table("Rows by source quality", aggregates["rows_by_source_quality"])
    lines += _count_table("Why we did not bet", aggregates["refusal_attribution"])
    lines += _count_table("What the result says about the read", aggregates["outcome_attribution"])

    lines += ["### Metrics, per bucket", ""]
    if not aggregates["buckets"]:
        lines += ["No rows, so no bucket has a metric.", ""]
    for bucket in aggregates["buckets"]:
        header = (
            f"**{bucket['fidelity']} / {bucket['source_quality']}"
            f" / {bucket['model_version'] or 'no model_version'}** — {bucket['rows']} rows"
        )
        lines.append(header)
        metrics = bucket["metrics"]
        if metrics is None:
            lines += ["", "No row in this bucket carries both a probability and a final.", ""]
        else:
            lines += [
                "",
                f"- n {metrics['n']}, away {metrics['away_wins']}-{metrics['away_losses']}",
                f"- Brier {metrics['brier']}, log loss {metrics['log_loss']}",
                f"- mean conservative probability {metrics['mean_conservative_probability']}",
            ]
            comparison = bucket["market_comparison"]
            if comparison is None:
                lines.append("- no row in this bucket pairs a handicap with a DK fair price")
            else:
                lines += [
                    f"- against DK on the {comparison['n']} rows carrying both: model Brier "
                    f"{comparison['model_brier']}, DK Brier {comparison['dk_brier']}",
                    f"- median model minus DK {comparison['median_model_minus_dk']}, model below "
                    f"DK on {comparison['model_below_dk']} of {comparison['n']}",
                ]
            lines.append("")

    lines += ["### Ranked process fixes", ""]
    fixes = payload["ranked_process_fixes"]
    if not fixes:
        lines += ["No row was refused for a missing input.", ""]
    else:
        lines += ["| games | cause | example |", "| --- | --- | --- |"]
        lines += [
            f"| {fix['games']} | `{fix['cause']}` | {fix['example']} |" for fix in fixes
        ]
        lines.append("")

    lines += ["### Price fields this pipeline has never captured", ""]
    for field, reason in payload["uncaptured_price_fields"].items():
        lines.append(f"- `{field}`: {reason}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure what the card did with every scheduled game, from recorded state."
    )
    parser.add_argument(
        "--schedules",
        type=Path,
        action="append",
        required=True,
        help="directory of <date>-schedule.json; repeatable for multiple roots",
    )
    parser.add_argument("--start", required=True, help="first date, YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="last date, YYYY-MM-DD")
    parser.add_argument(
        "--finals", type=Path, help="directory of cached StatsAPI schedule payloads, <date>.json"
    )
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-markdown", type=Path)
    parser.add_argument("--out-rows", type=Path, help="write the per-game rows as JSONL")
    args = parser.parse_args(argv)

    try:
        dates = dates_in_range(
            dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.until)
        )
    except ValueError as exc:
        parser.error(str(exc))

    schedules, dedup = load_schedules(args.schedules, dates)
    finals = load_finals(args.finals, dates)
    rows, schedule_audit = build_rows(schedules, finals)
    payload = report(rows, dedup, schedule_audit)

    if args.out_rows:
        args.out_rows.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    if args.out_markdown:
        args.out_markdown.write_text(markdown(payload), encoding="utf-8")
    if args.out_json:
        args.out_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
