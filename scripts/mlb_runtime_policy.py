#!/usr/bin/env python3
"""Deterministic detection of local MLB standing authorization.

Also hosts the shared, machine-readable MLB selection policy
(``vig-mlb-selection-policy-v1``) loaded from ``risk_limits.json`` so slate
validation, review, watchlist promotion, and execution all read the SAME
edge floor, daily candidate caps, and promotion switches. Fails closed on
any read/parse problem.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path


def resolve_state_dir(home: Path | None = None) -> Path:
    override = os.environ.get("VIG_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return ((home or Path.home()) / ".hermes" / "vig" / "state").resolve()


# Temporary conservative rollout defaults per the 2026-08-11 hardening plan.
# These apply ONLY when risk_limits.json carries a vig-mlb-selection-policy-v1
# block WITHOUT an explicit value; a missing/invalid policy block fails closed.
DEFAULT_MIN_CONSERVATIVE_EDGE = 0.05
DEFAULT_MAX_MLB_OFFICIAL_BETS_PER_DAY = 2
DEFAULT_MAX_SMALL_BETS_PER_DAY_PROBATION = 1
POLICY_SCHEMA = "vig-mlb-selection-policy-v1"

# Numeric probability/edge fields an execution candidate MUST carry as real
# numbers before standing-authorized routing. A missing or non-numeric field
# means the edge cannot be recomputed from live data, so the candidate is
# ineligible — never execute on a stale stored edge.
REQUIRED_EXECUTION_NUMERIC_FIELDS = (
    "dk_fair_prob",
    "raw_probability",
    "uncertainty_haircut",
    "conservative_probability",
    "current_ask",
    "projected_edge_at_current_ask",
)
REQUIRED_EXECUTION_FIELDS = (*REQUIRED_EXECUTION_NUMERIC_FIELDS, "model_version")

# The market-only fallback's own model version. This is the ONE version that is
# executable without a deployment record, because it makes no model claim: it
# sets our probability to the book's de-vigged fair price and charges a zero
# uncertainty haircut. Defined here rather than in ``mlb_probability_model``
# because that module imports this one; both now read this single name so a
# rename cannot leave two spellings of "market-only" disagreeing.
MARKET_MODEL_VERSION = "vig-mlb-market-v1"

DEPLOYED_MODELS_SCHEMA = "vig-mlb-deployed-models-v1"


def load_deployed_model_versions(state_dir: Path | None = None) -> frozenset[str]:
    """Load the set of model versions cleared for execution by the deployment gate.

    Fails CLOSED to the empty set on any read/parse/schema problem: a model
    version can never become executable because the record that would have
    refused it was unreadable. The market-only fallback is deliberately NOT
    included here — it is allowed by ``model_deployment_errors`` on the
    separate ground that it makes no model claim, so an empty or missing
    record leaves exactly today's behaviour rather than halting the slate.
    """
    root = state_dir or resolve_state_dir()
    try:
        data = json.loads((root / "risk_limits.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    block = data.get("mlb_deployed_models")
    if not isinstance(block, dict) or block.get("schema") != DEPLOYED_MODELS_SCHEMA:
        return frozenset()
    versions = block.get("versions")
    if not isinstance(versions, list):
        return frozenset()
    cleared: set[str] = set()
    for entry in versions:
        # One malformed entry invalidates the whole record rather than being
        # skipped: a record we cannot read completely is a record we cannot
        # trust to be the reason a version is absent.
        if not isinstance(entry, str) or not entry.strip():
            return frozenset()
        cleared.add(entry.strip())
    return frozenset(cleared)


def model_deployment_errors(candidate: dict, state_dir: Path | None = None) -> list[str]:
    """Refuse execution on a model version nobody deployed.

    ``stale_probability_field_errors`` checks that ``model_version`` is a
    non-empty string and stops there — against no allowlist and against no
    deployment record. That leaves the money boundary open in exactly the
    direction the versioned deployment gate exists to close: a candidate
    carrying an invented, experimental, or retired non-market version passes
    every downstream check, because "there is a version string" was being
    read as "a model was deployed".

    Eligible versions are the market-only fallback (which asserts no model)
    and whatever the deployment record lists. Everything else is refused, and
    the refusal names the version so the receipt says which model was claimed.
    """
    version = candidate.get("model_version")
    if not isinstance(version, str) or not version.strip():
        # Deliberately silent here: the missing/blank case is already reported
        # by stale_probability_field_errors, and reporting it twice would make
        # a single defect look like two.
        return []
    version = version.strip()
    if version == MARKET_MODEL_VERSION:
        return []
    if version in load_deployed_model_versions(state_dir):
        return []
    return [
        f"model_version {version!r} is not deployed: it is neither the "
        f"market-only fallback ({MARKET_MODEL_VERSION!r}) nor listed in the "
        f"{DEPLOYED_MODELS_SCHEMA} record in risk_limits.json"
    ]


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


def _strict_probability(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 < value < 1
    )


def _strict_nonnegative(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


@dataclass(frozen=True)
class MlbSelectionPolicy:
    """Shared MLB selection rails, loaded from risk_limits.json.

    One machine-readable policy so slate validation, review, watchlist
    promotion, and execution cannot diverge. The uncertainty haircut behind
    ``conservative_probability`` is a model-uncertainty buffer, never a venue
    fee.
    """

    min_conservative_edge: float
    max_mlb_official_bets_per_day: int
    starter_pending_promotions_enabled: bool
    max_small_bets_per_day_probation: int
    policy_version: str
    effective_at: str

    def ceiling_for(self, conservative_probability: float) -> float:
        """Executable price ceiling: conservative probability minus the floor."""
        return round(conservative_probability - self.min_conservative_edge, 6)


def load_mlb_selection_policy(state_dir: Path | None = None) -> MlbSelectionPolicy | None:
    """Load the shared MLB selection policy. Fails closed (None) when the
    risk_limits.json block is missing, malformed, or carries invalid values,
    so no caller can silently fall back to a hard-coded edge floor."""
    root = state_dir or resolve_state_dir()
    try:
        data = json.loads((root / "risk_limits.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # Accept both the reviewed key (mlb_selection_policy) and the deployed
    # state-file key (mlb_policy); reject anything without the schema marker.
    block = data.get("mlb_selection_policy")
    if not isinstance(block, dict):
        block = data.get("mlb_policy")
    if not isinstance(block, dict) or block.get("schema") != POLICY_SCHEMA:
        return None

    min_edge = block.get("min_conservative_edge", DEFAULT_MIN_CONSERVATIVE_EDGE)
    max_bets = block.get(
        "max_mlb_official_bets_per_day", DEFAULT_MAX_MLB_OFFICIAL_BETS_PER_DAY
    )
    max_small = block.get(
        "max_small_bets_per_day_probation",
        block.get(
            "max_small_bets_per_day_during_probation",
            DEFAULT_MAX_SMALL_BETS_PER_DAY_PROBATION,
        ),
    )
    promotions = block.get("starter_pending_promotions_enabled", False)
    version = block.get("policy_version")
    effective = block.get("effective_at") or block.get("policy_effective_at")

    if not _strict_probability(min_edge):
        return None
    if not (isinstance(max_bets, int) and not isinstance(max_bets, bool) and max_bets >= 1):
        return None
    if not (isinstance(max_small, int) and not isinstance(max_small, bool) and max_small >= 0):
        return None
    if not isinstance(promotions, bool):
        return None
    if not isinstance(version, str) or not version.strip():
        return None
    if not isinstance(effective, str) or not effective.strip():
        return None
    return MlbSelectionPolicy(
        min_conservative_edge=float(min_edge),
        max_mlb_official_bets_per_day=max_bets,
        starter_pending_promotions_enabled=promotions,
        max_small_bets_per_day_probation=max_small,
        policy_version=version.strip(),
        effective_at=effective.strip(),
    )


def live_conservative_edge(candidate: dict) -> float | None:
    """Recompute the conservative edge from live fields.

    Edge is ALWAYS ``conservative_probability - current_ask`` recomputed from
    the candidate's numeric fields; a stored ``net_edge`` or morning
    ``projected_edge_at_current_ask`` never overrides live arithmetic.
    Returns None when any required field is missing or non-numeric.
    """
    cons = candidate.get("conservative_probability")
    ask = candidate.get("current_ask")
    if not _strict_probability(cons) or not _strict_probability(ask):
        return None
    return float(cons) - float(ask)  # type: ignore[arg-type]


def stale_probability_field_errors(candidate: dict) -> list[str]:
    """Reject candidates whose probability/edge trail is missing or stale.

    A standing-authorized MLB candidate must carry every required numeric
    probability field plus a model version, and the stored
    ``projected_edge_at_current_ask`` must agree with the live recomputation
    (``conservative_probability - current_ask``) within float tolerance.
    """
    errors: list[str] = []
    for field in REQUIRED_EXECUTION_NUMERIC_FIELDS:
        value = candidate.get(field)
        if field == "uncertainty_haircut":
            if not _strict_nonnegative(value):
                errors.append(f"{field} must be a non-negative number")
        elif not _strict_probability(value):
            errors.append(f"{field} must be a number between 0 and 1")
    version = candidate.get("model_version")
    if not isinstance(version, str) or not version.strip():
        errors.append("model_version must be a non-empty string")
    if errors:
        return errors
    live = live_conservative_edge(candidate)
    stored = float(candidate["projected_edge_at_current_ask"])
    if live is None or abs(live - stored) > 1e-6:
        errors.append(
            "projected_edge_at_current_ask is stale: stored "
            f"{stored:.6f} != live conservative_probability - current_ask "
            f"{live if live is None else round(live, 6)}"
        )
    return errors


def enforce_daily_candidate_limit(
    approved: list[dict], policy: MlbSelectionPolicy
) -> tuple[list[dict], list[dict]]:
    """Fail closed when the day's approved MLB candidates exceed the policy cap.

    Ranks qualified candidates by live conservative edge (descending) and
    keeps only the top ``max_mlb_official_bets_per_day``. Candidates whose
    edge cannot be recomputed are rejected outright. Returns
    ``(kept, rejected)`` with stable schedule order inside each bucket.
    """
    scored: list[tuple[float, int, dict]] = []
    for index, candidate in enumerate(approved):
        edge = live_conservative_edge(candidate)
        if edge is None:
            continue
        scored.append((-edge, index, candidate))
    scored.sort(key=lambda item: (item[0], item[1]))
    kept = [candidate for _, _, candidate in scored[: policy.max_mlb_official_bets_per_day]]
    kept_ids = {id(candidate) for candidate in kept}
    return (
        [candidate for candidate in approved if id(candidate) in kept_ids],
        [candidate for candidate in approved if id(candidate) not in kept_ids],
    )
