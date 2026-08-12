#!/usr/bin/env python3
"""Versioned MLB probability contract, dataset builder, and deployment gate.

Phase 3 of the 2026-08-11 MLB pick-process hardening plan. Three jobs:

1. **Probability component contract.** De-vigged DraftKings fair probability
   (``dk_fair_prob``) is the market prior. Every adjustment away from it must
   be an explicit named component with evidence — never "full handicap
   assigns 64%" — and the components must sum to
   ``raw_probability - dk_fair_prob``. Uncertainty haircuts (small samples,
   opener/bulk uncertainty, contact/HR risk, unavailable leverage relievers,
   lineup doubt, conflicting signals) are their own named components summing
   to ``uncertainty_haircut``, and
   ``conservative_probability = raw_probability - uncertainty_haircut`` is the
   only probability used for edge and execution. The market-only fallback is
   the empty contract: no adjustments, no haircut, raw == DK fair.

2. **Historical dataset + walk-forward evaluation.** ``build_dataset`` turns
   settled ledger rows into a leakage-free evaluation dataset (pre-pitch
   fields only). Evaluation is time-ordered walk-forward — never a random
   split — reporting Brier score, log loss, calibration slope/intercept, and
   reliability buckets, always against the DK-fair market baseline.

3. **Versioned deployment gate.** A model version may deploy only when its
   out-of-sample calibration is no worse than the market baseline AND at
   least one predictive score improves by the predeclared margin from the
   machine-readable deployment policy in ``risk_limits.json``. The gate fails
   closed (not deployable, market-only fallback) on a missing/invalid policy
   or a too-small evaluation window.

Usage:
  python scripts/mlb_probability_model.py dataset --picks picks.json --out dataset.jsonl
  python scripts/mlb_probability_model.py evaluate --dataset dataset.jsonl [--window 20]
  python scripts/mlb_probability_model.py gate --dataset dataset.jsonl --model-version <version>

``gate`` exits non-zero when the version is not deployable.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_runtime_policy import resolve_state_dir  # noqa: E402

MARKET_MODEL_VERSION = "vig-mlb-market-v1"
DEPLOYMENT_POLICY_SCHEMA = "vig-mlb-model-deployment-policy-v1"

# The versioned feature contract: every raw-probability adjustment off the
# market prior must be one of these named, pre-pitch components.
ADJUSTMENT_COMPONENTS = (
    "starter_run_prevention",
    "starter_expected_innings",
    "k_bb_contact_profile",
    "opponent_starter_bulk_path",
    "lineup_offense_quality",
    "bullpen_quality_availability",
    "park_home_context",
    "injury_lineup_deltas",
    "recent_form",
)

# Documented uncertainty haircuts, per the plan's immediate probability
# contract. Amounts are positive; their sum is uncertainty_haircut.
HAIRCUT_COMPONENTS = (
    "small_sample",
    "opener_bulk_uncertainty",
    "contact_hr_risk",
    "leverage_relievers_unavailable",
    "lineup_unconfirmed_or_weakened",
    "conflicting_signals",
)

COMPONENT_SUM_TOLERANCE = 1e-3
MAX_SINGLE_ADJUSTMENT = 0.15
# Recent form is a low-weight supporting input, never the anchor.
MAX_RECENT_FORM_DELTA = 0.02

# Field names that would leak postgame information into a "pre-pitch" feature
# set. Their presence inside probability_components is a hard failure, and the
# dataset builder never copies them into feature columns.
POSTGAME_LEAKAGE_KEYS = frozenset(
    {
        "result",
        "final_score",
        "away_score",
        "home_score",
        "winner",
        "pnl",
        "process_grade",
        "postgame_evidence",
        "scoring_plays",
    }
)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_probability(value: Any) -> bool:
    return _is_number(value) and 0 < value < 1


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ---------------------------------------------------------------------------
# Probability component contract
# ---------------------------------------------------------------------------


def _component_entries(
    entries: Any,
    *,
    kind: str,
    allowed: tuple[str, ...],
    value_key: str,
    errors: list[str],
) -> dict[str, float]:
    """Validate one component list; return {component: value} for valid entries."""
    values: dict[str, float] = {}
    if not isinstance(entries, list):
        errors.append(f"probability_components.{kind} must be a list")
        return values
    for index, entry in enumerate(entries):
        label = f"probability_components.{kind}[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object")
            continue
        leaked = POSTGAME_LEAKAGE_KEYS.intersection(entry)
        if leaked:
            errors.append(
                f"{label} carries postgame fields ({', '.join(sorted(leaked))}); "
                "components must be pre-pitch only"
            )
        component = entry.get("component")
        if component not in allowed:
            errors.append(
                f"{label}.component must be one of: {', '.join(allowed)}"
            )
            continue
        if component in values:
            errors.append(f"{label}: duplicate component {component!r}")
            continue
        value = entry.get(value_key)
        if not _is_number(value):
            errors.append(f"{label}.{value_key} must be a finite number")
            continue
        if not _is_non_empty_string(entry.get("evidence")):
            errors.append(f"{label} requires non-empty written evidence")
            continue
        values[component] = float(value)
    return values


def validate_probability_components(components: Any, candidate: dict[str, Any]) -> list[str]:
    """Hard-validate the structured probability contract on a candidate.

    The market-only fallback is expressed naturally: empty adjustment and
    haircut lists validate exactly when ``raw_probability == dk_fair_prob``
    and ``uncertainty_haircut == 0``.
    """
    if not isinstance(components, dict):
        return ["probability_components must be an object"]

    errors: list[str] = []
    adjustments = _component_entries(
        components.get("adjustments"),
        kind="adjustments",
        allowed=ADJUSTMENT_COMPONENTS,
        value_key="delta",
        errors=errors,
    )
    haircuts = _component_entries(
        components.get("haircuts"),
        kind="haircuts",
        allowed=HAIRCUT_COMPONENTS,
        value_key="amount",
        errors=errors,
    )
    if errors:
        return errors

    for component, delta in adjustments.items():
        if abs(delta) > MAX_SINGLE_ADJUSTMENT:
            errors.append(
                f"adjustment {component} delta {delta:+.3f} exceeds the "
                f"±{MAX_SINGLE_ADJUSTMENT} single-component bound"
            )
    recent_form = adjustments.get("recent_form")
    if recent_form is not None and recent_form != 0.0:
        if abs(recent_form) > MAX_RECENT_FORM_DELTA:
            errors.append(
                f"recent_form delta {recent_form:+.3f} exceeds the "
                f"±{MAX_RECENT_FORM_DELTA} low-weight bound"
            )
        others = [abs(d) for c, d in adjustments.items() if c != "recent_form" and d != 0.0]
        if not others or abs(recent_form) > max(others):
            errors.append(
                "recent_form cannot be the largest adjustment — it is a "
                "low-weight supporting input, never the anchor"
            )

    for component, amount in haircuts.items():
        if amount <= 0:
            errors.append(f"haircut {component} amount must be positive")

    dk_fair = candidate.get("dk_fair_prob")
    raw = candidate.get("raw_probability")
    haircut = candidate.get("uncertainty_haircut")
    conservative = candidate.get("conservative_probability")
    if not (_is_probability(dk_fair) and _is_probability(raw)):
        errors.append(
            "dk_fair_prob and raw_probability must be numbers between 0 and 1 "
            "before components can be validated"
        )
        return errors
    if not (_is_number(haircut) and haircut >= 0):
        errors.append("uncertainty_haircut must be a non-negative number")
        return errors

    adjustment_sum = sum(adjustments.values())
    stated_delta = float(raw) - float(dk_fair)
    if abs(adjustment_sum - stated_delta) > COMPONENT_SUM_TOLERANCE:
        errors.append(
            f"adjustments sum to {adjustment_sum:+.4f} but raw_probability - "
            f"dk_fair_prob is {stated_delta:+.4f}; every point of disagreement "
            "with the market prior must be an explicit component"
        )
    haircut_sum = sum(haircuts.values())
    if abs(haircut_sum - float(haircut)) > COMPONENT_SUM_TOLERANCE:
        errors.append(
            f"haircut components sum to {haircut_sum:.4f} but "
            f"uncertainty_haircut is {float(haircut):.4f}"
        )
    if _is_number(conservative) and abs(
        (float(raw) - float(haircut)) - float(conservative)
    ) > COMPONENT_SUM_TOLERANCE:
        errors.append(
            "conservative_probability must equal raw_probability - "
            f"uncertainty_haircut (stated {float(conservative):.4f}, "
            f"computed {float(raw) - float(haircut):.4f})"
        )
    return errors


def probability_component_errors(candidate: dict[str, Any]) -> list[str]:
    """Convenience wrapper for a candidate dict."""
    return validate_probability_components(
        candidate.get("probability_components"), candidate
    )


def valid_probability_components(**overrides: Any) -> dict[str, Any]:
    """Return a valid probability_components fixture for tests.

    Matches the repo-wide canonical candidate trail: dk_fair_prob=0.55,
    raw_probability=0.57, uncertainty_haircut=0.03,
    conservative_probability=0.54.
    """
    components: dict[str, Any] = {
        "adjustments": [
            {
                "component": "starter_run_prevention",
                "delta": 0.015,
                "evidence": "Starter FIP 3.1 vs opponent 4.5 over the season sample",
            },
            {
                "component": "lineup_offense_quality",
                "delta": 0.005,
                "evidence": "Top-5 wOBA lineup confirmed vs contact-prone starter",
            },
        ],
        "haircuts": [
            {
                "component": "small_sample",
                "amount": 0.03,
                "evidence": "Starter has 9 starts; sample-size buffer per contract",
            }
        ],
    }
    components.update(overrides)
    return components


# ---------------------------------------------------------------------------
# Historical dataset builder (pre-pitch features + official outcome only)
# ---------------------------------------------------------------------------

DATASET_PROBABILITY_FIELDS = (
    "dk_fair_prob",
    "raw_probability",
    "conservative_probability",
)
DATASET_OPTIONAL_FIELDS = (
    "entry_price",
    "slate_ask",
    "ask_at_recheck",
    "net_edge",
)


def build_dataset(picks: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Build evaluation rows from settled ledger picks.

    A row carries pre-pitch probabilities, the model version, and the official
    binary outcome — nothing else from the postgame record, so downstream
    evaluation cannot leak. Returns ``(rows, skipped_reasons)``; unusable picks
    are skipped loudly, never silently repaired.
    """
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    if not isinstance(picks, list):
        return rows, ["picks must be a list"]
    for index, pick in enumerate(picks):
        pick_id = (pick.get("pick_id") if isinstance(pick, dict) else None) or f"index-{index}"
        if not isinstance(pick, dict):
            skipped.append(f"{pick_id}: not an object")
            continue
        if pick.get("status") != "settled" or pick.get("result") not in ("win", "loss"):
            skipped.append(f"{pick_id}: not a settled win/loss")
            continue
        date = pick.get("game_date") or pick.get("date")
        if not _is_non_empty_string(date):
            skipped.append(f"{pick_id}: missing game date")
            continue
        missing = [
            field
            for field in DATASET_PROBABILITY_FIELDS
            if not _is_probability(pick.get(field))
        ]
        if missing:
            skipped.append(f"{pick_id}: missing probability fields ({', '.join(missing)})")
            continue
        haircut = pick.get("uncertainty_haircut")
        row: dict[str, Any] = {
            "date": date,
            "pick_id": str(pick_id),
            "side": pick.get("side") or pick.get("team"),
            "model_version": pick.get("model_version")
            if _is_non_empty_string(pick.get("model_version"))
            else None,
            "uncertainty_haircut": float(haircut) if _is_number(haircut) else None,
            "outcome": 1 if pick["result"] == "win" else 0,
        }
        for field in DATASET_PROBABILITY_FIELDS:
            row[field] = float(pick[field])
        for field in DATASET_OPTIONAL_FIELDS:
            value = pick.get(field)
            if _is_number(value):
                row[field] = float(value)
        rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["pick_id"]))
    return rows, skipped


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

_LOG_LOSS_CLIP = 1e-6


def brier_score(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def log_loss(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for p, y in pairs:
        p = min(max(p, _LOG_LOSS_CLIP), 1 - _LOG_LOSS_CLIP)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(pairs)


def calibration_line(pairs: list[tuple[float, int]]) -> tuple[float, float] | None:
    """Least-squares slope/intercept of outcome on predicted probability.

    Perfect calibration is slope 1, intercept 0. Returns None when the
    predictions are degenerate (fewer than 2 points or zero variance).
    """
    if len(pairs) < 2:
        return None
    n = len(pairs)
    mean_p = sum(p for p, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    var_p = sum((p - mean_p) ** 2 for p, _ in pairs)
    if var_p <= 0:
        return None
    cov = sum((p - mean_p) * (y - mean_y) for p, y in pairs)
    slope = cov / var_p
    return slope, mean_y - slope * mean_p


def calibration_error(line: tuple[float, float] | None) -> float | None:
    """Distance from perfect calibration: |slope - 1| + |intercept|."""
    if line is None:
        return None
    slope, intercept = line
    return abs(slope - 1.0) + abs(intercept)


def reliability_buckets(
    pairs: list[tuple[float, int]], width: float = 0.05
) -> list[dict[str, Any]]:
    buckets: dict[float, list[int]] = {}
    for p, y in pairs:
        key = math.floor(p / width) * width
        buckets.setdefault(round(key, 4), []).append((p, y))
    report = []
    for key in sorted(buckets):
        rows = buckets[key]
        report.append(
            {
                "bucket": f"{key:.2f}-{key + width:.2f}",
                "n": len(rows),
                "mean_predicted": round(sum(p for p, _ in rows) / len(rows), 4),
                "observed_rate": round(sum(y for _, y in rows) / len(rows), 4),
            }
        )
    return report


def evaluate_predictions(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    pairs = [
        (float(row[field]), int(row["outcome"]))
        for row in rows
        if _is_probability(row.get(field)) and row.get("outcome") in (0, 1)
    ]
    line = calibration_line(pairs)
    return {
        "field": field,
        "n": len(pairs),
        "brier": brier_score(pairs),
        "log_loss": log_loss(pairs),
        "calibration_slope": None if line is None else line[0],
        "calibration_intercept": None if line is None else line[1],
        "calibration_error": calibration_error(line),
        "reliability_buckets": reliability_buckets(pairs),
    }


def walk_forward_report(
    rows: list[dict[str, Any]], field: str, window: int = 20
) -> dict[str, Any]:
    """Time-ordered walk-forward evaluation — never a random split.

    Rows are sorted chronologically and evaluated in consecutive windows, so
    every window is strictly out-of-sample relative to the picks that came
    before it and no postgame information can travel backwards in time.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    ordered = sorted(rows, key=lambda r: (str(r.get("date")), str(r.get("pick_id"))))
    windows = []
    for start in range(0, len(ordered), window):
        chunk = ordered[start : start + window]
        result = evaluate_predictions(chunk, field)
        result["start_date"] = chunk[0].get("date")
        result["end_date"] = chunk[-1].get("date")
        del result["reliability_buckets"]
        windows.append(result)
    return {
        "field": field,
        "window_size": window,
        "split": "time-ordered walk-forward (no random split)",
        "windows": windows,
        "cumulative": evaluate_predictions(ordered, field),
    }


def compare_to_market(
    rows: list[dict[str, Any]], model_field: str = "conservative_probability"
) -> dict[str, Any]:
    """Evaluate the model field against the DK-fair market baseline on the
    exact same rows, so every metric delta is apples-to-apples."""
    usable = [
        row
        for row in rows
        if _is_probability(row.get(model_field)) and _is_probability(row.get("dk_fair_prob"))
    ]
    model = evaluate_predictions(usable, model_field)
    market = evaluate_predictions(usable, "dk_fair_prob")
    deltas: dict[str, Any] = {}
    for metric in ("brier", "log_loss", "calibration_error"):
        a, b = model.get(metric), market.get(metric)
        deltas[metric] = None if a is None or b is None else a - b
    return {"n": model["n"], "model": model, "market": market, "deltas": deltas}


# ---------------------------------------------------------------------------
# Versioned deployment gate (fail-closed)
# ---------------------------------------------------------------------------

DEFAULT_MIN_EVALUATION_PICKS = 40
DEFAULT_MIN_BRIER_IMPROVEMENT = 0.005
DEFAULT_MIN_LOG_LOSS_IMPROVEMENT = 0.01
DEFAULT_MAX_CALIBRATION_REGRESSION = 0.0
DEFAULT_MAX_SCORE_REGRESSION = 0.0


def load_model_deployment_policy(state_dir: Path | None = None) -> dict[str, float] | None:
    """Load the predeclared deployment margins from risk_limits.json.

    Fails closed (None) when the block is missing, malformed, or invalid — a
    model version can never talk its way past margins that were not declared
    before the evaluation ran.
    """
    root = state_dir or resolve_state_dir()
    try:
        data = json.loads((root / "risk_limits.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("mlb_model_deployment_policy")
    if not isinstance(block, dict) or block.get("schema") != DEPLOYMENT_POLICY_SCHEMA:
        return None
    policy = {
        "min_evaluation_picks": block.get(
            "min_evaluation_picks", DEFAULT_MIN_EVALUATION_PICKS
        ),
        "min_brier_improvement": block.get(
            "min_brier_improvement", DEFAULT_MIN_BRIER_IMPROVEMENT
        ),
        "min_log_loss_improvement": block.get(
            "min_log_loss_improvement", DEFAULT_MIN_LOG_LOSS_IMPROVEMENT
        ),
        "max_calibration_regression": block.get(
            "max_calibration_regression", DEFAULT_MAX_CALIBRATION_REGRESSION
        ),
        "max_score_regression": block.get(
            "max_score_regression", DEFAULT_MAX_SCORE_REGRESSION
        ),
    }
    picks = policy["min_evaluation_picks"]
    if not (isinstance(picks, int) and not isinstance(picks, bool) and picks >= 1):
        return None
    for key in (
        "min_brier_improvement",
        "min_log_loss_improvement",
        "max_calibration_regression",
        "max_score_regression",
    ):
        value = policy[key]
        if not _is_number(value) or value < 0:
            return None
        policy[key] = float(value)
    policy["min_evaluation_picks"] = picks
    return policy


def deployment_gate_decision(
    comparison: dict[str, Any], policy: dict[str, float] | None
) -> dict[str, Any]:
    """Decide whether a model version may deploy, per the plan's rule:
    out-of-sample calibration no worse than the market baseline AND at least
    one predictive score improved by the predeclared margin, over at least the
    minimum evaluation window. Anything else falls back to market-only."""
    reasons: list[str] = []
    if policy is None:
        reasons.append(
            "deployment policy missing or invalid in risk_limits.json "
            f"(schema {DEPLOYMENT_POLICY_SCHEMA}); failing closed"
        )
        return {
            "deployable": False,
            "reasons": reasons,
            "fallback_model_version": MARKET_MODEL_VERSION,
        }

    n = comparison.get("n") or 0
    if n < policy["min_evaluation_picks"]:
        reasons.append(
            f"evaluation window has {n} settled picks; "
            f"{policy['min_evaluation_picks']} required"
        )

    deltas = comparison.get("deltas") or {}
    calibration_delta = deltas.get("calibration_error")
    if calibration_delta is None:
        reasons.append("calibration could not be computed for model or market")
    elif calibration_delta > policy["max_calibration_regression"] + 1e-12:
        reasons.append(
            f"out-of-sample calibration is worse than the market baseline "
            f"(calibration_error delta {calibration_delta:+.4f})"
        )

    brier_delta = deltas.get("brier")
    log_loss_delta = deltas.get("log_loss")
    if brier_delta is None or log_loss_delta is None:
        reasons.append("brier/log_loss could not be computed for model or market")
    else:
        for name, delta in (("brier", brier_delta), ("log_loss", log_loss_delta)):
            if delta > policy["max_score_regression"] + 1e-12:
                reasons.append(
                    f"{name} regresses vs the market baseline ({delta:+.4f})"
                )
        improved = (
            brier_delta <= -policy["min_brier_improvement"]
            or log_loss_delta <= -policy["min_log_loss_improvement"]
        )
        if not improved:
            reasons.append(
                "no predictive score improves on the market baseline by its "
                f"predeclared margin (brier {brier_delta:+.4f} vs "
                f"-{policy['min_brier_improvement']}, log_loss "
                f"{log_loss_delta:+.4f} vs -{policy['min_log_loss_improvement']})"
            )

    return {
        "deployable": not reasons,
        "reasons": reasons,
        "fallback_model_version": MARKET_MODEL_VERSION,
    }


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


def probability_contract_prompt_section() -> str:
    """Structured probability contract text for the Vig review prompt."""
    return """\
PROBABILITY COMPONENTS (required object on every approved MLB candidate):
Provide a structured `probability_components` object; the deterministic validator
rejects any approval whose numbers do not reconcile. Prose cannot satisfy this —
the component values must sum to the stated deltas.
- adjustments: list of {component, delta, evidence}. Every point of disagreement
  with the market prior is an explicit component; the deltas must sum to
  raw_probability - dk_fair_prob (tolerance 0.001). Allowed components:
  starter_run_prevention, starter_expected_innings, k_bb_contact_profile,
  opponent_starter_bulk_path, lineup_offense_quality, bullpen_quality_availability,
  park_home_context, injury_lineup_deltas, recent_form. recent_form is a low-weight
  supporting input: |delta| <= 0.02 and never the largest component.
- haircuts: list of {component, amount, evidence}. Positive amounts summing to
  uncertainty_haircut (tolerance 0.001). Allowed components: small_sample,
  opener_bulk_uncertainty, contact_hr_risk, leverage_relievers_unavailable,
  lineup_unconfirmed_or_weakened, conflicting_signals.
- conservative_probability must equal raw_probability - uncertainty_haircut; it is
  the ONLY probability used for edge and execution.
- Every component needs written evidence citing pre-pitch data. Postgame fields
  inside a component are a hard failure (no leakage).
- Market-only fallback (model_version vig-mlb-market-v1): empty adjustments and
  haircuts with raw_probability == dk_fair_prob and uncertainty_haircut == 0.
Model versions are gated: a non-market model_version may only be used after
scripts/mlb_probability_model.py gate reports it deployable out-of-sample against
the DK-fair baseline. When in doubt, use the market-only fallback."""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_picks(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        return data.get("picks", [])
    return data if isinstance(data, list) else []


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _cmd_dataset(args: argparse.Namespace) -> int:
    rows, skipped = build_dataset(_load_picks(Path(args.picks)))
    out = "\n".join(json.dumps(row) for row in rows)
    if args.out:
        Path(args.out).write_text(out + ("\n" if out else ""))
    else:
        print(out)
    print(
        json.dumps({"rows": len(rows), "skipped": len(skipped), "skip_reasons": skipped}),
        file=sys.stderr,
    )
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    rows = _load_dataset(Path(args.dataset))
    report = {
        "walk_forward": walk_forward_report(rows, args.field, args.window),
        "market_comparison": compare_to_market(rows, args.field),
    }
    print(json.dumps(report, indent=2))
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    rows = _load_dataset(Path(args.dataset))
    if args.model_version:
        rows = [row for row in rows if row.get("model_version") == args.model_version]
    comparison = compare_to_market(rows, args.field)
    policy = load_model_deployment_policy(
        Path(args.state_dir) if args.state_dir else None
    )
    decision = deployment_gate_decision(comparison, policy)
    decision["model_version"] = args.model_version
    decision["comparison"] = comparison
    print(json.dumps(decision, indent=2))
    return 0 if decision["deployable"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MLB probability contract, dataset builder, and deployment gate"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dataset = sub.add_parser("dataset", help="build the evaluation dataset from picks.json")
    dataset.add_argument("--picks", required=True, help="path to picks.json")
    dataset.add_argument("--out", help="write JSONL here instead of stdout")
    dataset.set_defaults(func=_cmd_dataset)

    evaluate = sub.add_parser("evaluate", help="walk-forward evaluation vs market baseline")
    evaluate.add_argument("--dataset", required=True, help="path to dataset JSONL")
    evaluate.add_argument("--field", default="conservative_probability")
    evaluate.add_argument("--window", type=int, default=20)
    evaluate.set_defaults(func=_cmd_evaluate)

    gate = sub.add_parser("gate", help="versioned deployment gate (exit 1 = not deployable)")
    gate.add_argument("--dataset", required=True, help="path to dataset JSONL")
    gate.add_argument("--model-version", help="evaluate only rows from this model version")
    gate.add_argument("--field", default="conservative_probability")
    gate.add_argument("--state-dir", help="override the Vig state dir for risk_limits.json")
    gate.set_defaults(func=_cmd_gate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
