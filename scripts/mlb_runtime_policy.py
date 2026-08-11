#!/usr/bin/env python3
"""Deterministic detection of local MLB standing authorization + shared policy.

The MLB selection policy lives in the ``mlb_policy`` section of
``risk_limits.json`` (Vig-owned state). Slate validation, review, watchlist
promotion, and execution all read it through this module so the rails cannot
diverge. Fails closed: an absent/invalid policy section resolves to the most
conservative defaults, never to a looser rail than intended.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

RISK_LIMITS_PATH = Path("/home/clawdbot/.hermes/vig/state/risk_limits.json")

# Policy keys and their conservative rollout defaults. All five keys must be
# present in the policy section; a missing/invalid key fails closed to these.
POLICY_DEFAULTS: dict[str, Any] = {
    "min_conservative_edge": 0.05,
    "max_mlb_official_bets_per_day": 2,
    "starter_pending_promotions_enabled": False,
    "max_small_bets_per_day_during_probation": 1,
}
POLICY_VERSION_DEFAULT = "vig-mlb-policy-v1"

# Numeric probability/edge fields a candidate must carry before it may route to
# standing-authorized execution. The uncertainty haircut converts the raw model
# probability into the conservative probability used for edge math; it is NOT a
# venue fee (Polymarket US charges zero trading fees).
REQUIRED_EXECUTION_PROBABILITY_FIELDS = (
    "dk_fair_prob",
    "raw_probability",
    "uncertainty_haircut",
    "conservative_probability",
    "current_ask",
    "projected_edge_at_current_ask",
    "model_version",
)


def resolve_state_dir(home: Path | None = None) -> Path:
    override = os.environ.get("VIG_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return ((home or Path.home()) / ".hermes" / "vig" / "state").resolve()


def resolve_risk_limits_path() -> Path:
    override = os.environ.get("VIG_RISK_LIMITS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return RISK_LIMITS_PATH


def standing_authorization_enabled(state_dir: Path | None = None) -> bool:
    """Authorization is an explicit flag file, never prose substring matching.

    Prose matching was both over-broad ("standing authorization is suspended"
    still matched) and fragile (innocent rewording silently disabled
    automation). Fails closed on any read/parse problem.
    """
    root = state_dir or resolve_state_dir()
    try:
        flag = json.loads((root / "standing_authorization.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(flag, dict)
        and flag.get("schema") == "vig-standing-authorization-v1"
        and flag.get("enabled") is True
    )


def _clean_probability(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def load_mlb_policy(path: Path | str | None = None) -> dict[str, Any]:
    """Load the shared MLB selection policy from risk_limits.json.

    Returns a dict with the four policy values plus ``policy_version`` and
    ``policy_effective_at``. Every key must be present and well-typed in the
    file's ``mlb_policy`` section; anything missing or malformed fails closed
    to the conservative default so a corrupted state file loosens nothing.
    """
    source = Path(path) if path is not None else resolve_risk_limits_path()
    section: dict[str, Any] = {}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("mlb_policy"), dict):
            section = data["mlb_policy"]
    except (OSError, json.JSONDecodeError):
        section = {}

    edge = _clean_probability(section.get("min_conservative_edge"))
    if edge is None or not 0 < edge < 1:
        edge = float(POLICY_DEFAULTS["min_conservative_edge"])

    max_official = section.get("max_mlb_official_bets_per_day")
    if not isinstance(max_official, int) or isinstance(max_official, bool) or max_official < 1:
        max_official = POLICY_DEFAULTS["max_mlb_official_bets_per_day"]

    starter_enabled = section.get("starter_pending_promotions_enabled")
    if starter_enabled is not True:
        starter_enabled = POLICY_DEFAULTS["starter_pending_promotions_enabled"]

    max_small = section.get("max_small_bets_per_day_during_probation")
    if not isinstance(max_small, int) or isinstance(max_small, bool) or max_small < 1:
        max_small = POLICY_DEFAULTS["max_small_bets_per_day_during_probation"]

    version = section.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        version = POLICY_VERSION_DEFAULT
    effective = section.get("policy_effective_at")
    if not isinstance(effective, str) or not effective.strip():
        effective = ""

    return {
        "min_conservative_edge": edge,
        "max_mlb_official_bets_per_day": max_official,
        "starter_pending_promotions_enabled": bool(starter_enabled),
        "max_small_bets_per_day_during_probation": max_small,
        "policy_version": version.strip(),
        "policy_effective_at": effective.strip(),
    }


def executable_price_ceiling(
    conservative_probability: Any, policy: dict[str, Any] | None = None
) -> float | None:
    """max_polymarket_price = conservative_probability - min_conservative_edge.

    Returns None (fail closed) when the probability is not a clean (0, 1)
    number or the resulting ceiling leaves no positive edge.
    """
    prob = _clean_probability(conservative_probability)
    if prob is None or not 0 < prob < 1:
        return None
    resolved = policy or load_mlb_policy()
    ceiling = prob - float(resolved["min_conservative_edge"])
    return ceiling if 0 < ceiling < 1 else None


def projected_edge(conservative_probability: Any, current_ask: Any) -> float | None:
    """conservative_probability - current_ask, or None on bad inputs."""
    prob = _clean_probability(conservative_probability)
    ask = _clean_probability(current_ask)
    if prob is None or ask is None or not 0 < ask < 1:
        return None
    return prob - ask


# Float tolerance for edge-floor comparisons: 0.60 - 0.55 is 0.04999... in
# binary floating point, which must still count as meeting an 0.05 floor.
EDGE_FLOOR_EPSILON = 1e-9


def edge_meets_floor(
    conservative_probability: Any, current_ask: Any, min_edge: float
) -> bool | None:
    """True when the live conservative edge clears the floor (within epsilon).

    None when the edge cannot be recomputed from the inputs.
    """
    edge = projected_edge(conservative_probability, current_ask)
    if edge is None:
        return None
    return edge >= min_edge - EDGE_FLOOR_EPSILON


def missing_probability_fields(candidate: dict[str, Any]) -> list[str]:
    """Return the required probability/edge fields that are absent or non-numeric.

    ``model_version`` is a string field; the rest must be clean numbers. A
    candidate with ANY missing field is ineligible for standing-authorized
    routing — stale or absent probability data must fail closed.
    """
    missing: list[str] = []
    for field in REQUIRED_EXECUTION_PROBABILITY_FIELDS:
        value = candidate.get(field)
        if field == "model_version":
            if not isinstance(value, str) or not value.strip():
                missing.append(field)
        elif _clean_probability(value) is None:
            missing.append(field)
    return missing
