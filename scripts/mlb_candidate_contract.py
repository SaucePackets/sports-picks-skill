"""Shared candidate admission checks; never author probabilities or place orders."""

from __future__ import annotations
import json
from pathlib import Path
from mlb_runtime_policy import (
    model_deployment_errors,
    resolve_state_dir,
    stale_probability_field_errors,
    live_conservative_edge,
    load_mlb_selection_policy,
)
from mlb_probability_model import probability_component_errors
from numeric_util import is_finite_number


def candidate_errors(
    candidate: dict, *, state_dir: Path | None = None, limits: dict | None = None
) -> list[str]:
    errors = stale_probability_field_errors(candidate)
    errors += model_deployment_errors(candidate, state_dir)
    errors += probability_component_errors(candidate)
    policy = load_mlb_selection_policy(state_dir)
    edge = live_conservative_edge(candidate)
    if policy is None:
        errors.append(
            "shared MLB selection policy missing or invalid in risk_limits.json"
        )
    elif edge is not None and edge + 1e-9 < policy.min_conservative_edge:
        errors.append(
            f"live conservative edge {edge} below policy floor {policy.min_conservative_edge}"
        )
    if limits is None:
        try:
            limits = json.loads(
                ((state_dir or resolve_state_dir()) / "risk_limits.json").read_text()
            )
        except (OSError, ValueError):
            limits = None
    tier = str(candidate.get("confidence") or "").strip().lower()
    caps = limits.get("max_unit_usd") if isinstance(limits, dict) else None
    cap = (
        caps.get(tier)
        if isinstance(caps, dict) and tier in {"small", "medium", "high"}
        else None
    )
    unit = candidate.get("unit_size")
    if not is_finite_number(cap) or cap <= 0:
        errors.append("confidence tier cap missing or invalid in risk_limits.json")
    if not is_finite_number(unit) or unit <= 0:
        errors.append("unit_size must be a positive finite number")
    elif is_finite_number(cap) and cap > 0 and unit > cap:
        errors.append(f"{tier}-tier unit_size {unit} exceeds cap {cap}")
    return list(dict.fromkeys(errors))
