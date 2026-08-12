#!/usr/bin/env python3
"""Deterministic baseball evidence schema and hard validators.

Phase 2 of the MLB pick-process hardening plan separates baseball gates from
execution gates. A standing-authorized MLB candidate must carry a structured
``baseball_evidence`` object (the thesis/edge inputs) and an ``execution_checks``
object (tradeability confirmation). Baseball evidence is never allowed to make
execution easier; execution checks are never allowed to increase probability.
"""

from __future__ import annotations

import math
from typing import Any


STARTER_ROLES = frozenset({"starter", "opener", "bulk", "piggyback", "unknown"})
MAGNITUDES = frozenset({"none", "small", "moderate", "large"})
NAMED_RISK_STATUSES = frozenset({"resolved", "unresolved"})

# Required fields on every baseball_evidence object. ``expected_pitch_count``
# and ``bulk_path_plan`` are intentionally optional because they are not always
# available/applicable.
REQUIRED_BASEBALL_EVIDENCE_FIELDS = (
    "starter_role",
    "expected_ip",
    "starter_sample_ip",
    "starter_games_started",
    "starter_floor_evidence",
    "opponent_shutdown_path",
    "candidate_failure_path",
    "contact_hr_risk",
    "bullpen_availability",
    "likely_leverage_arms",
    "offense_quality",
    "lineup_quality",
    "environment",
    "named_risks",
)

REQUIRED_EXECUTION_CHECK_FIELDS = (
    "exact_event_slug_side_mapping",
    "supported_price",
    "price_timestamp",
    "current_ask_inside_ceiling",
    "liquidity",
    "bankroll_and_daily_cap_ok",
    "lineup_confirmation",
    "injury_scratch_refresh",
    "receipt_dedup_ready",
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _magnitude_ok(value: Any) -> bool:
    return isinstance(value, dict) and value.get("magnitude") in MAGNITUDES


def validate_baseball_evidence(
    evidence: Any, candidate: dict[str, Any] | None = None
) -> list[str]:
    """Return a list of hard-failure messages for a baseball_evidence object.

    An empty list means the evidence satisfies every deterministic hard gate
    defined in Phase 2 of the hardening plan. The function is intentionally
    strict: missing or malformed evidence fails closed.
    """
    errors: list[str] = []
    if not isinstance(evidence, dict):
        return ["baseball_evidence must be an object"]

    missing = [field for field in REQUIRED_BASEBALL_EVIDENCE_FIELDS if field not in evidence]
    if missing:
        return [f"baseball_evidence missing required fields: {', '.join(missing)}"]

    # --- starter role / length ---
    role = evidence.get("starter_role")
    if role not in STARTER_ROLES:
        errors.append(f"starter_role must be one of {sorted(STARTER_ROLES)}")
    if role == "unknown":
        errors.append("starter_role is unknown")

    expected_ip = evidence.get("expected_ip")
    if not _is_number(expected_ip) or expected_ip <= 0:
        errors.append("expected_ip must be a positive number")

    sample_ip = evidence.get("starter_sample_ip")
    if not _is_number(sample_ip) or sample_ip < 0:
        errors.append("starter_sample_ip must be a non-negative number")

    games_started = evidence.get("starter_games_started")
    if not _is_non_negative_int(games_started):
        errors.append("starter_games_started must be a non-negative integer")

    # Non-starter roles require an explicit early-to-middle innings plan.
    if role in {"opener", "bulk", "piggyback"}:
        if not _is_non_empty_string(evidence.get("bulk_path_plan")):
            errors.append(
                f"{role} game requires a bulk_path_plan describing early-to-middle innings coverage"
            )

    # --- primary thesis pillar rules ---
    primary = evidence.get("primary_thesis_pillar") is True
    if primary:
        if _is_number(expected_ip) and expected_ip < 5:
            errors.append("primary thesis pillar requires expected_ip >= 5")
        if _is_non_negative_int(games_started) and games_started < 6:
            errors.append(
                "starter sample too small to be a primary thesis pillar "
                f"(games_started={games_started})"
            )

    # --- named risks ---
    named_risks = evidence.get("named_risks")
    if not isinstance(named_risks, list):
        errors.append("named_risks must be a list")
    else:
        for index, risk in enumerate(named_risks):
            if not isinstance(risk, dict):
                errors.append(f"named_risks[{index}] must be an object")
                continue
            name = risk.get("name")
            if not _is_non_empty_string(name):
                errors.append(f"named_risks[{index}] missing name")
            status = risk.get("status")
            if status not in NAMED_RISK_STATUSES:
                errors.append(
                    f"named_risks[{index}] status must be one of {sorted(NAMED_RISK_STATUSES)}"
                )
            elif status == "unresolved":
                errors.append(f"unresolved named risk: {name or index}")
            if not _is_non_empty_string(risk.get("evidence")):
                errors.append(f"named_risks[{index}] missing evidence")

    # --- per-pillar magnitudes ---
    for pillar in ("contact_hr_risk", "bullpen_availability", "offense_quality", "lineup_quality"):
        value = evidence.get(pillar)
        if not _magnitude_ok(value):
            errors.append(f"{pillar} must be an object with magnitude in {sorted(MAGNITUDES)}")

    # --- contact-dependent starter used as main edge ---
    contact = evidence.get("contact_hr_risk") if isinstance(evidence.get("contact_hr_risk"), dict) else {}
    contact_mag = contact.get("magnitude")
    if primary and contact_mag in {"moderate", "large"}:
        support_layers = evidence.get("support_layers") or []
        if not any(
            isinstance(layer, dict)
            and layer.get("magnitude") == "large"
            for layer in support_layers
        ):
            errors.append(
                "contact-dependent primary thesis pillar requires a separate large support layer"
            )

    # --- bullpen leverage-arm availability ---
    bullpen = evidence.get("bullpen_availability") if isinstance(evidence.get("bullpen_availability"), dict) else {}
    if not bullpen.get("leverage_arms_available"):
        errors.append("bullpen_availability requires leverage_arms_available=true")

    # --- environment ---
    if not isinstance(evidence.get("environment"), dict):
        errors.append("environment must be an object")

    # --- quantified explanation when raw probability differs materially from market ---
    if candidate is not None:
        dk = candidate.get("dk_fair_prob")
        raw = candidate.get("raw_probability")
        if (
            _is_number(dk)
            and _is_number(raw)
            and raw - dk > 0.04
            and not _is_non_empty_string(evidence.get("probability_delta_explanation"))
        ):
            errors.append(
                "raw_probability exceeds dk_fair_prob by >0.04 without a quantified explanation"
            )

    return errors


def validate_execution_checks(checks: Any) -> list[str]:
    """Return a list of hard-failure messages for an execution_checks object."""
    errors: list[str] = []
    if not isinstance(checks, dict):
        return ["execution_checks must be an object"]

    missing = [field for field in REQUIRED_EXECUTION_CHECK_FIELDS if field not in checks]
    if missing:
        return [f"execution_checks missing required fields: {', '.join(missing)}"]

    boolean_fields = (
        "exact_event_slug_side_mapping",
        "current_ask_inside_ceiling",
        "bankroll_and_daily_cap_ok",
        "lineup_confirmation",
        "injury_scratch_refresh",
        "receipt_dedup_ready",
    )
    for field in boolean_fields:
        if checks.get(field) is not True:
            errors.append(f"execution_checks.{field} must be true")

    supported_price = checks.get("supported_price")
    if not _is_number(supported_price) or not 0 < supported_price < 1:
        errors.append("execution_checks.supported_price must be a number between 0 and 1")

    if not _is_non_empty_string(checks.get("price_timestamp")):
        errors.append("execution_checks.price_timestamp must be a non-empty timestamp")

    if not isinstance(checks.get("liquidity"), dict):
        errors.append("execution_checks.liquidity must be an object")

    return errors


def baseball_evidence_errors(candidate: dict[str, Any]) -> list[str]:
    """Convenience wrapper for a candidate dict."""
    return validate_baseball_evidence(candidate.get("baseball_evidence"), candidate)


def execution_checks_errors(candidate: dict[str, Any]) -> list[str]:
    """Convenience wrapper for a candidate dict."""
    return validate_execution_checks(candidate.get("execution_checks"))


def valid_baseball_evidence(**overrides: Any) -> dict[str, Any]:
    """Return a valid baseball_evidence fixture for tests."""
    evidence: dict[str, Any] = {
        "starter_role": "starter",
        "expected_ip": 6.0,
        "expected_pitch_count": 95,
        "starter_sample_ip": 52.0,
        "starter_games_started": 9,
        "starter_floor_evidence": "Last two starts 6 IP/2 ER, K-BB% 18%, FIP 3.60",
        "opponent_shutdown_path": "Opposing starter has 6+ IP in 6 of last 7; bullpen taxed yesterday",
        "candidate_failure_path": "Early hook + cold offense against a shutdown path erases the edge",
        "contact_hr_risk": {"magnitude": "small", "notes": "GB-heavy, limited hard contact"},
        "bullpen_availability": {
            "magnitude": "small",
            "leverage_arms_available": True,
            "notes": "Core leverage arms rested",
        },
        "likely_leverage_arms": "Closer + setup rested",
        "offense_quality": {"magnitude": "moderate", "notes": "Top-7 wOBA over last 7 games"},
        "lineup_quality": {"magnitude": "moderate", "notes": "Confirmed lineup, no key scratches"},
        "environment": {"park_factor": 100, "temperature": 72, "notes": "Neutral park"},
        "named_risks": [
            {
                "name": "opener/bulk uncertainty",
                "status": "resolved",
                "evidence": "Confirmed conventional starter via pre-game probables",
            }
        ],
        "primary_thesis_pillar": True,
        "support_layers": [
            {"pillar": "offense", "magnitude": "large"},
            {"pillar": "bullpen", "magnitude": "moderate"},
        ],
        "probability_delta_explanation": "Raw 0.57 vs DK fair 0.55: +0.02 from starter floor",
    }
    evidence.update(overrides)
    return evidence


def valid_execution_checks(**overrides: Any) -> dict[str, Any]:
    """Return a valid execution_checks fixture for tests."""
    checks: dict[str, Any] = {
        "exact_event_slug_side_mapping": True,
        "supported_price": 0.59,
        "price_timestamp": "2026-08-11T20:00:00Z",
        "current_ask_inside_ceiling": True,
        "liquidity": {"book_state": "reliable", "fillable_notional_usd": 50},
        "bankroll_and_daily_cap_ok": True,
        "lineup_confirmation": True,
        "injury_scratch_refresh": True,
        "receipt_dedup_ready": True,
    }
    checks.update(overrides)
    return checks
