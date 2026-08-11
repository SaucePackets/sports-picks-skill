#!/usr/bin/env python3
"""Conditional Vig review gate shared by MLB and soccer cron wrappers."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Make polymarket_us_guard importable for deterministic in-process price fetches,
# from both the repo layout and the deployed profile layout.
for _guard_dir in (
    SCRIPT_DIR.parent / "skills" / "sports-picks" / "scripts",
    SCRIPT_DIR.parent / "skills" / "openclaw-imports" / "sports-betting-markets" / "scripts",
):
    if _guard_dir.is_dir() and str(_guard_dir) not in sys.path:
        sys.path.insert(0, str(_guard_dir))

from mlb_lineup_watchlist import (  # noqa: E402
    WatchlistFormatError,
    build_recheck_prompt,
    due_entries,
    fetch_lineup_snapshot,
    validate_watchlist,
)
from mlb_runtime_policy import (  # noqa: E402
    EDGE_FLOOR_EPSILON,
    executable_price_ceiling,
    load_mlb_policy,
    missing_probability_fields,
    projected_edge,
    standing_authorization_enabled,
)

HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "/home/clawdbot/.local/bin/hermes"


def resolve_root(cwd: Path | None = None, home: Path | None = None) -> Path:
    """Resolve runtime state even when Hermes launches a script from its profile directory."""
    override = os.environ.get("SPORTS_PICKS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = (cwd or Path.cwd()).expanduser().resolve()
    if (current / ".picks").is_dir():
        return current
    default = ((home or Path.home()) / "projects" / "sports-picks-skill").resolve()
    if (default / ".picks").is_dir():
        return default
    return current


ROOT = resolve_root()


class ScheduleFormatError(ValueError):
    """Raised when a review schedule is valid JSON but has no candidate list."""


def parse_candidates(data: object) -> list[dict[str, Any]]:
    """Accept raw candidate arrays and schedule objects with candidates."""
    if isinstance(data, list):
        if not all(isinstance(item, dict) for item in data):
            raise ScheduleFormatError("every candidate must be an object")
        return data
    if not isinstance(data, dict):
        raise ScheduleFormatError(f"expected object or list, got {type(data).__name__}")
    if "candidates" not in data:
        raise ScheduleFormatError("schedule object is missing candidates")
    candidates = data["candidates"]
    if not isinstance(candidates, list):
        raise ScheduleFormatError(f"candidates must be a list, got {type(candidates).__name__}")
    if not all(isinstance(item, dict) for item in candidates):
        raise ScheduleFormatError("every candidate must be an object")
    return candidates


def pending_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [candidate for candidate in candidates if not isinstance(candidate.get("vig_approved"), bool)]


# Fields owned by execution_guard.py; the review gate must never clobber them.
EXECUTION_PROGRESS_FIELDS = (
    "execution_lock", "executed", "executed_at", "execution_status",
    "fill_price", "fill_quantity", "fill_notional", "commission",
    "polymarket_order_id", "polymarket_trade_id", "duplicate_fill_count",
    "duplicate_order_ids", "duplicate_trade_ids", "execution_note",
    "execution_receipt", "execution_receipts",
)


def persist_schedule_locked(schedule_path: Path, desired: dict[str, Any]) -> None:
    """Write the reviewed schedule under the same flock the execution guard uses.

    The guard rewrites the schedule in place under flock; a tmp+os.replace here
    swaps the inode out from under a waiting locker and loses its update. Take
    the same lock, then merge: any execution progress the guard recorded since
    the child reviewer read the file wins over the reviewed copy.
    """
    with schedule_path.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            try:
                on_disk = json.load(handle)
            except json.JSONDecodeError:
                on_disk = {}
            disk_by_id = {
                candidate_identity(item): item
                for item in on_disk.get("candidates", [])
                if isinstance(item, dict)
            }
            for candidate in desired.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                current = disk_by_id.get(candidate_identity(candidate))
                if not isinstance(current, dict):
                    continue
                if current.get("execution_lock") or current.get("executed"):
                    for field in EXECUTION_PROGRESS_FIELDS:
                        if field in current:
                            candidate[field] = current[field]
            handle.seek(0)
            json.dump(desired, handle, indent=2)
            handle.write("\n")
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def candidate_identity(candidate: dict[str, Any]) -> str:
    for field in ("id", "watchlist_id", "polymarket_slug", "market_slug", "event_id"):
        value = candidate.get(field)
        if value not in (None, ""):
            return f"{field}:{value}|side:{candidate.get('side', '')}"
    return f"side:{candidate.get('side', '')}|game:{candidate.get('game', '')}"


def _strict_price(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 < value < 1
    )


def _strict_polymarket_ask(
    candidate: dict[str, Any], original: dict[str, Any] | None
) -> int | float | None:
    prices = [
        candidate[field]
        for field in ("approved_polymarket_ask", "captured_polymarket_ask")
        if _strict_price(candidate.get(field))
    ]
    if original is not None:
        for field in ("approved_polymarket_ask", "captured_polymarket_ask", "polymarket_ask"):
            value = original.get(field)
            if _strict_price(value):
                prices.append(value)
    elif _strict_price(candidate.get("polymarket_ask")):
        prices.append(candidate["polymarket_ask"])
    return min(prices) if prices else None


def _watchlist_supported_price(
    candidate: dict[str, Any], entry: dict[str, Any]
) -> int | float | None:
    """Return only a refreshed signed American price, never the original snapshot."""
    for source, fields in (
        (candidate, ("supported_price", "price", "current_price")),
        (entry, ("supported_price", "current_price")),
    ):
        for field in fields:
            value = source.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
    return None


def _daily_limits_key(candidate: dict[str, Any]) -> str:
    """UTC day the candidate counts against for official/small daily limits."""
    first_pitch = candidate.get("first_pitch_utc")
    if isinstance(first_pitch, str) and len(first_pitch) >= 10:
        return first_pitch[:10]
    slug = candidate.get("polymarket_slug")
    if isinstance(slug, str):
        return slug[-10:]
    return ""


def apply_daily_candidate_limits(
    newly_approved: list[dict[str, Any]],
    all_candidates: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, str]:
    """Enforce max official bets/day and the probation small-bet/day cap.

    Returns {identity: reason} for newly approved candidates that must be
    demoted back to vig_approved=false. Ranks same-day qualified candidates by
    conservative edge (live recomputation when the fields exist, else the
    stored projected edge), ties broken by baseball-gate strength (count of
    true original_gate_results), then keeps only the top
    ``max_mlb_official_bets_per_day`` per UTC day. Small-tier volume during
    probation is separately capped at
    ``max_small_bets_per_day_during_probation`` per day.
    """
    demotions: dict[str, str] = {}
    max_official = int(policy.get("max_mlb_official_bets_per_day") or 0)
    max_small = int(policy.get("max_small_bets_per_day_during_probation") or 0)
    if max_official < 1 and max_small < 1:
        return demotions

    def edge_of(candidate: dict[str, Any]) -> float:
        live = projected_edge(
            candidate.get("conservative_probability"), candidate.get("current_ask")
        )
        if live is not None:
            return live
        stored = candidate.get("projected_edge_at_current_ask")
        if isinstance(stored, (int, float)) and not isinstance(stored, bool):
            return float(stored)
        return float("-inf")

    def gate_strength(candidate: dict[str, Any]) -> int:
        gates = candidate.get("original_gate_results")
        if not isinstance(gates, dict):
            return 0
        return sum(1 for value in gates.values() if value is True)

    def is_small(candidate: dict[str, Any]) -> bool:
        return str(candidate.get("confidence") or "").strip().lower() == "small"

    approved_by_day: dict[str, list[dict[str, Any]]] = {}
    for candidate in all_candidates:
        if (
            isinstance(candidate, dict)
            and candidate.get("vig_approved") is True
            and candidate not in newly_approved
        ):
            approved_by_day.setdefault(_daily_limits_key(candidate), []).append(candidate)

    new_by_day: dict[str, list[dict[str, Any]]] = {}
    for candidate in newly_approved:
        new_by_day.setdefault(_daily_limits_key(candidate), []).append(candidate)

    for day, day_new in new_by_day.items():
        # Existing same-day approvals reserve their slots: they were approved
        # earlier in the day, so new candidates compete only for the remaining
        # capacity. Ranking new candidates against the FULL pool and then only
        # demoting members of day_new lets total approvals exceed the cap.
        existing = approved_by_day.get(day, [])
        if max_official >= 1:
            remaining = max_official - len(existing)
            if len(day_new) > max(remaining, 0):
                ranked = sorted(
                    day_new,
                    key=lambda c: (edge_of(c), gate_strength(c)),
                    reverse=True,
                )
                kept_ids = {id(c) for c in ranked[: max(remaining, 0)]}
                for candidate in day_new:
                    if id(candidate) not in kept_ids:
                        demotions[candidate_identity(candidate)] = (
                            f"daily official-bet limit: day {day or '<unknown>'} has "
                            f"{len(existing) + len(day_new)} qualified candidates, policy allows "
                            f"{max_official}; this candidate ranked below the cutoff "
                            "on conservative edge"
                        )
        if max_small >= 1:
            surviving_new = [
                c for c in day_new if candidate_identity(c) not in demotions
            ]
            existing_small = [c for c in existing if is_small(c)]
            new_small = [c for c in surviving_new if is_small(c)]
            remaining_small = max_small - len(existing_small)
            if len(new_small) > max(remaining_small, 0):
                ranked_small = sorted(
                    new_small,
                    key=lambda c: (edge_of(c), gate_strength(c)),
                    reverse=True,
                )
                kept_small = {id(c) for c in ranked_small[: max(remaining_small, 0)]}
                for candidate in new_small:
                    if id(candidate) not in kept_small:
                        demotions[candidate_identity(candidate)] = (
                            f"probation small-bet daily limit: day {day or '<unknown>'} has "
                            f"{len(existing_small) + len(new_small)} small-tier candidates, policy allows "
                            f"{max_small}; this candidate ranked below the cutoff "
                            "on conservative edge"
                        )
    return demotions


def _validate_newly_approved_probability_contract(
    candidate: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    """Standing-authorized candidates must carry the full probability contract.

    Recomputes the live edge from conservative_probability and current_ask;
    the stored morning number can never substitute for live arithmetic.
    """
    errors: list[str] = []
    missing = missing_probability_fields(candidate)
    if missing:
        errors.append(
            "missing/non-numeric probability contract fields: " + ", ".join(missing)
        )
        return errors
    live_edge = projected_edge(
        candidate.get("conservative_probability"), candidate.get("current_ask")
    )
    min_edge = float(policy["min_conservative_edge"])
    if live_edge is None:
        errors.append("live conservative edge could not be recomputed")
    elif live_edge < min_edge - EDGE_FLOOR_EPSILON:
        errors.append(
            f"live conservative edge {live_edge:.4f} below min_conservative_edge "
            f"{min_edge:.4f}"
        )
    ceiling = executable_price_ceiling(
        candidate.get("conservative_probability"), policy
    )
    if ceiling is None:
        errors.append("executable price ceiling could not be derived")
    return errors


def normalize_review_routing(
    before: dict[str, Any],
    after: dict[str, Any],
    sport: str,
    mlb_standing_authorized: bool = False,
    day: str | None = None,
) -> list[str]:
    """Deterministically route newly approved MLB candidates after child review."""
    if sport.upper() != "MLB" or not mlb_standing_authorized:
        return []

    try:
        before_candidates = parse_candidates(before)
        after_candidates = parse_candidates(after)
    except ScheduleFormatError as exc:
        return [str(exc)]
    seen_identities: set[str] = set()
    duplicate_errors: list[str] = []
    for candidate in after_candidates:
        identity = candidate_identity(candidate)
        if identity in seen_identities:
            duplicate_errors.append(
                f"candidate {identity} appears more than once after review"
            )
        seen_identities.add(identity)
    if duplicate_errors:
        return duplicate_errors
    before_by_id = {candidate_identity(item): item for item in before_candidates}
    before_watchlist_ids = {
        entry.get("id")
        for entry in before.get("lineup_watchlist", [])
        if isinstance(entry, dict) and entry.get("id")
    }
    promoted_watchlist_ids = {
        entry.get("id")
        for entry in after.get("lineup_watchlist", [])
        if isinstance(entry, dict)
        and entry.get("status") == "promoted"
        and entry.get("id") in before_watchlist_ids
    }
    newly_approved = [
        candidate
        for candidate in after_candidates
        if candidate.get("vig_approved") is True
        and before_by_id.get(candidate_identity(candidate), {}).get("vig_approved") is not True
    ]

    policy = load_mlb_policy()

    # Fail loud first: injected candidates and non-numeric asks are review
    # integrity errors, not policy demotions — they must surface as errors and
    # abort routing, never be silently downgraded by the contract check below.
    integrity_errors: list[str] = []
    for candidate in newly_approved:
        identity = candidate_identity(candidate)
        original = before_by_id.get(identity)
        if original is None and candidate.get("watchlist_id") not in promoted_watchlist_ids:
            integrity_errors.append(
                f"candidate {identity} was not a targeted candidate or watchlist promotion"
            )
            candidate["vig_approved"] = False
            continue
        if _strict_polymarket_ask(candidate, original) is None:
            integrity_errors.append(
                f"candidate {identity} has no strict numeric "
                "approved Polymarket ask"
            )
            candidate["vig_approved"] = False
    if integrity_errors:
        return integrity_errors

    # Standing-authorized routing requires the full probability contract with a
    # live recomputed conservative edge at/above the policy floor. Candidates
    # that fail stay in the review as manual-only approvals (vig_approved stays
    # true but they never reach the execution poller).
    routing_eligible: list[dict[str, Any]] = []
    for candidate in newly_approved:
        contract_errors = _validate_newly_approved_probability_contract(candidate, policy)
        if contract_errors:
            candidate["vig_approved"] = False
            note = (
                "rejected by shared MLB policy at routing: " + "; ".join(contract_errors)
            )
            existing = candidate.get("vig_notes")
            candidate["vig_notes"] = (
                f"{existing} [{note}]" if isinstance(existing, str) and existing.strip() else note
            )
            continue
        routing_eligible.append(candidate)

    # Daily volume rails: max N official bets/day and 1 small/day during
    # probation, ranked by conservative edge. Fail closed (demote) when over.
    demotions = apply_daily_candidate_limits(
        routing_eligible, after_candidates, policy
    )
    eligible_identities = set()
    for candidate in routing_eligible:
        identity = candidate_identity(candidate)
        if identity in demotions:
            candidate["vig_approved"] = False
            note = f"rejected by shared MLB policy at routing: {demotions[identity]}"
            existing = candidate.get("vig_notes")
            candidate["vig_notes"] = (
                f"{existing} [{note}]" if isinstance(existing, str) and existing.strip() else note
            )
        else:
            eligible_identities.add(identity)
    newly_approved = [
        candidate
        for candidate in routing_eligible
        if candidate_identity(candidate) in eligible_identities
    ]

    prices: list[tuple[dict[str, Any], int | float]] = []
    errors: list[str] = []
    for candidate in newly_approved:
        identity = candidate_identity(candidate)
        original = before_by_id.get(identity)
        if original is None and candidate.get("watchlist_id") not in promoted_watchlist_ids:
            errors.append(
                f"candidate {identity} was not a targeted candidate or watchlist promotion"
            )
            continue
        ask = _strict_polymarket_ask(candidate, original)
        if ask is None:
            errors.append(
                f"candidate {candidate_identity(candidate)} has no strict numeric "
                "approved Polymarket ask"
            )
        else:
            prices.append((candidate, ask))
    if errors:
        return errors

    after["sport"] = "MLB"
    after["market_type"] = "moneyline"
    if day:
        # The execution gate fails closed on a wrong "date" header; stamp the
        # schedule's own day so approved candidates are actually executable.
        after["date"] = day
    for candidate, ask in prices:
        candidate.update(
            sport="MLB",
            market_type="moneyline",
            execution_mode="standing_authorized",
            execution_status="pending",
            executed=False,
            max_polymarket_price=ask,
        )
        candidate.pop("manual_bet_status", None)

    normalized_by_watchlist_id = {
        candidate.get("watchlist_id"): candidate
        for candidate, _ in prices
        if candidate.get("watchlist_id")
    }
    for entry in after.get("lineup_watchlist", []):
        if not isinstance(entry, dict):
            continue
        normalized = normalized_by_watchlist_id.get(entry.get("id"))
        if normalized is not None and entry.get("status") == "promoted":
            entry["promoted_candidate"] = dict(normalized)
    return []


def manual_candidate_errors(candidate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if candidate.get("execution_mode") != "manual":
        errors.append("execution_mode must be manual")
    if candidate.get("manual_bet_status") != "awaiting_jerry":
        errors.append("manual_bet_status must be awaiting_jerry")
    if candidate.get("executed") is not False:
        errors.append("executed must be false")
    forbidden = sorted(
        field
        for field in ("execution_cron_id", "execution_cron_fire_utc", "approval_token")
        if field in candidate
    )
    if forbidden:
        errors.append(f"forbidden execution fields present: {', '.join(forbidden)}")
    return errors


def approved_candidate_errors(
    candidate: dict[str, Any], sport: str, mlb_standing_authorized: bool = False
) -> list[str]:
    """Validate the post-review routing state for an approved candidate."""
    if sport.upper() != "MLB" or not mlb_standing_authorized:
        return manual_candidate_errors(candidate)

    errors: list[str] = []
    if candidate.get("sport") != "MLB":
        errors.append("sport must be MLB")
    if candidate.get("market_type") != "moneyline":
        errors.append("market_type must be moneyline")
    if candidate.get("execution_mode") != "standing_authorized":
        errors.append("execution_mode must be standing_authorized")
    if candidate.get("execution_status") != "pending":
        errors.append("execution_status must be pending")
    if candidate.get("manual_bet_status") == "awaiting_jerry":
        errors.append("manual_bet_status must not be awaiting_jerry")
    if candidate.get("executed") is not False:
        errors.append("executed must be false")
    max_price = candidate.get("max_polymarket_price")
    if (
        not isinstance(max_price, (int, float))
        or isinstance(max_price, bool)
        or not 0 < max_price < 1
    ):
        errors.append("max_polymarket_price must be between 0 and 1")
    forbidden = sorted(
        field
        for field in ("execution_cron_id", "execution_cron_fire_utc", "approval_token")
        if field in candidate
    )
    if forbidden:
        errors.append(
            f"forbidden execution fields present: {', '.join(forbidden)}; "
            "use the recurring MLB execution poller"
        )
    return errors


def validate_review_transition(
    before: dict[str, Any],
    after: dict[str, Any],
    candidate_ids: list[str],
    watchlist_ids: list[str],
    sport: str = "MLB",
    mlb_standing_authorized: bool = False,
) -> list[str]:
    errors: list[str] = []
    watch_errors = validate_watchlist(after)
    for entry_id, entry_errors in watch_errors.items():
        errors.extend(f"watchlist {entry_id}: {message}" for message in entry_errors)
    try:
        before_candidates = parse_candidates(before)
        after_candidates = parse_candidates(after)
    except ScheduleFormatError as exc:
        return [str(exc), *errors]

    before_by_id = {candidate_identity(item): item for item in before_candidates}
    after_by_id = {candidate_identity(item): item for item in after_candidates}
    targeted_candidates = set(candidate_ids)
    for identity in targeted_candidates:
        candidate = after_by_id.get(identity)
        if candidate is None:
            errors.append(f"candidate {identity} missing after review")
            continue
        if not isinstance(candidate.get("vig_approved"), bool):
            errors.append(f"candidate {identity} has no boolean decision")
        notes = candidate.get("vig_notes")
        if not isinstance(notes, str) or not notes.strip():
            errors.append(f"candidate {identity} has empty vig_notes")
        if candidate.get("vig_approved") is True:
            errors.extend(
                f"candidate {identity}: {message}"
                for message in approved_candidate_errors(
                    candidate, sport, mlb_standing_authorized
                )
            )

    for identity, candidate in before_by_id.items():
        if identity not in targeted_candidates and after_by_id.get(identity) != candidate:
            errors.append(f"untargeted candidate {identity} changed")

    before_watch = {
        item.get("id"): item
        for item in before.get("lineup_watchlist", [])
        if isinstance(item, dict) and item.get("id")
    }
    after_watch = {
        item.get("id"): item
        for item in after.get("lineup_watchlist", [])
        if isinstance(item, dict) and item.get("id")
    }
    targeted_watch = set(watchlist_ids)
    for entry_id in targeted_watch:
        entry = after_watch.get(entry_id)
        if entry is None:
            errors.append(f"watchlist {entry_id} missing after review")
            continue
        status = entry.get("status")
        if status not in ("promoted", "passed"):
            errors.append(f"watchlist {entry_id} did not reach promoted or passed")
            continue
        report_candidate: dict[str, Any] = {}
        if status == "promoted":
            matches = [item for item in after_candidates if item.get("watchlist_id") == entry_id]
            if len(matches) != 1:
                errors.append(f"watchlist {entry_id} must map to exactly one candidate")
            elif matches[0] != entry.get("promoted_candidate"):
                errors.append(f"watchlist {entry_id} promoted_candidate differs from candidates entry")
            else:
                report_candidate = matches[0]
                if report_candidate.get("vig_approved") is not True:
                    errors.append(f"watchlist {entry_id} promoted candidate must be vig_approved")
                reason = entry.get("recheck_notes") or report_candidate.get("vig_notes")
                if not isinstance(reason, str) or not reason.strip():
                    errors.append(f"watchlist {entry_id} promoted candidate has no decisive reason")
        if status == "promoted" and _watchlist_supported_price(report_candidate, entry) is None:
            errors.append(f"watchlist {entry_id} has no refreshed supported price")

    for entry_id, entry in before_watch.items():
        if entry_id not in targeted_watch and after_watch.get(entry_id) != entry:
            errors.append(f"untargeted watchlist {entry_id} changed")
    return errors


def review_work(
    schedule: dict[str, Any], sport: str, now: datetime | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = pending_candidates(parse_candidates(schedule))
    watchlist = due_entries(schedule, now) if sport == "MLB" else []
    return candidates, watchlist


def _policy_clause(mlb: bool) -> str:
    policy = load_mlb_policy()
    edge = policy["min_conservative_edge"]
    if mlb:
        return (
            f"Recompute the conservative probability from the refreshed handicap, then set the\n"
            f"price ceiling max_polymarket_price = conservative_probability - {edge:.3f} (the\n"
            f"shared policy's min_conservative_edge). Judge"
        )
    return (
        f"Recompute win_probability from the refreshed handicap, then set the price ceiling\n"
        f"max_polymarket_price = win_probability - {edge:.3f} (our required edge floor). Judge"
    )


def build_regular_review_prompt(
    sport: str,
    day: str,
    schedule_path: Path,
    candidates: list[dict[str, Any]],
    mlb_standing_authorized: bool = False,
) -> str:
    sides = ", ".join(str(candidate.get("side", "<unknown>")) for candidate in candidates)
    routing = (
        """For an MLB approval under Jerry's standing authorization, set
execution_mode=standing_authorized, execution_status=pending, and executed=false.
Set sport=MLB, market_type=moneyline, and an explicit max_polymarket_price
between 0 and 1 from the approved price
discipline rail. Remove any legacy manual reminder status. Do not create a one-shot cron,
approval token, or trading command here. The recurring MLB execution poller will
refresh every gate and handle capped execution with canonical receipts.
"""
        if sport.upper() == "MLB" and mlb_standing_authorized
        else """Every approval is manual-only: set execution_mode=manual,
manual_bet_status=awaiting_jerry, and executed=false. Include no execution cron,
approval token, or trading command. An approved candidate is only a reminder for
Jerry and must never place or schedule a bet.
"""
    )
    contract_clause = ""
    if sport.upper() == "MLB" and mlb_standing_authorized:
        policy = load_mlb_policy()
        min_edge = policy["min_conservative_edge"]
        max_official = policy["max_mlb_official_bets_per_day"]
        max_small = policy["max_small_bets_per_day_during_probation"]
        contract_clause = f"""
MLB PROBABILITY CONTRACT (shared policy v{policy['policy_version']} — enforced
deterministically at routing; a candidate missing any piece is NOT routed to
execution):
- Provide dk_fair_prob (de-vigged DraftKings fair probability), raw_probability,
  uncertainty_haircut, conservative_probability (raw minus the haircut), the live
  current_ask, projected_edge_at_current_ask (= conservative_probability -
  current_ask), and model_version.
- The executable ceiling is max_polymarket_price = conservative_probability -
  min_conservative_edge ({min_edge:.3f}). A live recomputed conservative edge
  below {min_edge:.3f} is ineligible no matter what the morning number said.
- At most {max_official} official MLB bet(s) per day and {max_small} Small-tier
  bet(s) per day during probation; qualified candidates are ranked by
  conservative edge and baseball-gate strength and the rest are cut.
"""
    price_clause = _policy_clause(sport.upper() == "MLB")
    return f"""You are Vig performing the independent {sport} card review for {day}.
Read {schedule_path}. Review only pending candidates: {sides}. Refresh decisive
inputs and current supported-market prices, then apply every original hard gate.
Update each reviewed candidate with boolean vig_approved and concise vig_notes.
{contract_clause}

PRICE DISCIPLINE (the ceiling is the ONLY guardrail — do NOT do fee arithmetic):
{price_clause}
price on the REAL cost to buy: approve whenever the current executable ask — what
you would actually pay right now — is at or under that ceiling; reject on price
only when the live ask is above the ceiling. This is fee-agnostic on purpose: any
venue fee is already baked into the executable price, so paying it is fine AS LONG
AS the all-in price stays at or under the ceiling. Polymarket US currently charges
ZERO fees (confirmed 0 bps on every executed receipt), so today the executable ask
equals the quoted ask. Never invent, add, or subtract a phantom fee (no 0.024, no
2.4% rail). Execution is an IOC limit placed AT the ceiling, so the book can only
ever fill at or under your number and can never push you past the suggested odds.

{routing}

Return a concise card review with approved/rejected count, decisive reason per
candidate, and total proposed exposure.
"""


_SLUG_RE = re.compile(r"aec-mlb-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}")


def _entry_slug(entry: dict[str, Any]) -> str | None:
    """Structured polymarket_slug if stamped, else parse it from thesis prose."""
    slug = entry.get("polymarket_slug")
    if isinstance(slug, str) and slug.startswith("aec-mlb-"):
        return slug
    match = _SLUG_RE.search(str(entry.get("thesis", "")))
    return match.group(0) if match else None


def fetch_market_price(slug: str) -> dict[str, Any] | None:
    """Deterministic in-process Polymarket US price snapshot for a slug.

    urllib-based (bypasses the cron sandbox exactly like the lineup fetch), so
    the recheck no longer depends on the reviewer's broken web tools. Returns
    both the long/YES ask and the NO-side complement so the reviewer can match
    the team to a side against the slate-captured ask. None on any failure —
    the caller then carries the stored ceiling to the execution poller, which
    enforces the live price deterministically at order time.
    """
    try:
        import importlib

        guard = importlib.import_module("polymarket_us_guard")
        market, bbo = guard.market_snapshots(slug)
        is_open, reason = guard.market_is_open(market, bbo)
        long_side = guard.market_prices_for_side(bbo, "OUTCOME_SIDE_LONG")
        no_side = guard.market_prices_for_side(bbo, "OUTCOME_SIDE_NO")
        return {
            "slug": slug,
            "open": bool(is_open),
            "reason": reason,
            "long_ask": long_side.get("entry_ask"),
            "no_ask": no_side.get("entry_ask"),
            "book_state": long_side.get("book_state"),
        }
    except Exception:
        return None


def _price_context(watchlist: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in watchlist:
        slug = _entry_slug(entry)
        if not slug:
            lines.append(f"{entry.get('id')}: no Polymarket slug resolvable")
            continue
        price = fetch_market_price(slug)
        if not price:
            lines.append(f"{entry.get('id')}: current price unavailable ({slug})")
            continue
        lines.append(
            f"{entry.get('id')} [{slug}]: market open={price['open']} ({price['reason']}), "
            f"book={price['book_state']}, long/YES ask={price['long_ask']}, "
            f"NO-side ask={price['no_ask']}"
        )
    if not lines:
        return ""
    return (
        "\n\nDeterministic Polymarket US prices (fetched in-process — DO NOT web-search "
        "or curl for price; match your side to the slate-captured ask in the thesis):\n"
        + "\n".join(lines)
    )


def build_lineup_recheck_prompt(
    schedule_path: Path, watchlist: list[dict[str, Any]]
) -> str:
    """Fetch schedule-mapped MLB feeds + deterministic prices for the review."""
    snapshots: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    for entry in watchlist:
        entry_id = str(entry.get("id", "<missing-id>"))
        try:
            snapshots[entry_id] = fetch_lineup_snapshot(entry)
        except Exception:
            unavailable.append(entry_id)
    prompt = build_recheck_prompt(schedule_path, watchlist, snapshots)
    prompt += _price_context(watchlist)
    if unavailable:
        prompt += (
            "\nMLB lineup lookup was unavailable for: "
            + ", ".join(unavailable)
            + ". Fail the lineup-confirmation gate unless another live source verifies it.\n"
        )
    return prompt


def _schedule_path(sport: str, day: str) -> Path:
    if sport == "MLB":
        return ROOT / ".picks" / "execute" / f"{day}-schedule.json"
    return ROOT / ".picks" / "execute" / "intl-soccer" / f"{day}-schedule.json"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or f"{singular}s")


def write_latest_action(
    sport: str,
    day: str,
    schedule: dict[str, Any],
    mlb_standing_authorized: bool = False,
) -> Path:
    candidates = parse_candidates(schedule)
    approved = sum(candidate.get("vig_approved") is True for candidate in candidates)
    rejected = sum(candidate.get("vig_approved") is False for candidate in candidates)
    pending_watch = sum(
        isinstance(entry, dict) and entry.get("status") == "pending_lineup_recheck"
        for entry in schedule.get("lineup_watchlist", [])
    )
    label = sport.upper()
    if label == "MLB" and mlb_standing_authorized:
        text = (
            f"{day}: MLB review complete. {approved} approved standing-authorized "
            f"{_plural(approved, 'candidate')} routed to execution poller; "
            f"{rejected} rejected. "
        )
    else:
        text = (
            f"{day}: {label} review complete. {approved} approved manual-only "
            f"{_plural(approved, 'candidate')} awaiting Jerry; {rejected} rejected. "
        )
    if label == "MLB":
        text += (
            f"{pending_watch} lineup watchlist {_plural(pending_watch, 'recheck')} pending. "
        )
        exposure = sum(
            float(candidate.get("unit_size", 0))
            for candidate in candidates
            if candidate.get("vig_approved") is True
            and isinstance(candidate.get("unit_size"), (int, float))
            and not isinstance(candidate.get("unit_size"), bool)
        )
        cap = schedule.get("daily_cap")
        if isinstance(cap, (int, float)) and not isinstance(cap, bool):
            text += f"Approved exposure ${exposure:g} / ${cap:g}. "
    text += "Review gate placed no bet.\n"

    path = ROOT / ".picks" / "latest-action.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return path


def _american_price(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"+{value:g}" if value > 0 else f"{value:g}"
    text = str(value).strip()
    return text if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text) else "not recorded"


def _concise_text(value: Any, limit: int = 240) -> str:
    """Render a bounded plain-text field without tool, JSON, diff, or path artifacts."""
    text = " ".join(str(value or "not recorded").split())
    text = re.sub(r"[┊┃│|]?\s*review diff\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\*{3}\s*(?:Begin|End) Patch\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\*{3}\s*Update File:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\{[^{}]*\}", "[structured data omitted]", text)
    text = re.sub(r"(?<!\w)/(?:[\w.-]+/)+[\w.-]+", "[path omitted]", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    shortened = text[: limit - 3].rsplit(" ", 1)[0]
    return f"{shortened or text[: limit - 3]}..."


def _size(candidate: dict[str, Any], entry: dict[str, Any] | None = None) -> str:
    value = candidate.get("unit_size")
    if value is None and entry is not None:
        value = entry.get("unit_size")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"${value:g}"
    return "not recorded"


def _approved_status(
    sport: str, candidate: dict[str, Any], mlb_standing_authorized: bool
) -> str:
    if (
        sport.upper() == "MLB"
        and mlb_standing_authorized
        and candidate.get("execution_mode") == "standing_authorized"
    ):
        return "pending execution"
    return "awaiting Jerry"


def build_lineup_recheck_report(
    schedule: dict[str, Any],
    watchlist_ids: list[str],
    sport: str,
    mlb_standing_authorized: bool,
) -> str:
    entries = {
        str(entry.get("id")): entry
        for entry in schedule.get("lineup_watchlist", [])
        if isinstance(entry, dict)
    }
    sections: list[str] = []
    for entry_id in watchlist_ids:
        entry = entries[entry_id]
        approved = entry.get("status") == "promoted"
        candidate = entry.get("promoted_candidate") if approved else {}
        if not isinstance(candidate, dict):
            candidate = {}
        decision = "APPROVED" if approved else "PASSED"
        side = _concise_text(candidate.get("side") or entry.get("side"), 80)
        supported_price = _watchlist_supported_price(candidate, entry)
        bettable_to = candidate.get("bettable_to_price", entry.get("bettable_to_price"))
        reason = entry.get("recheck_notes") or candidate.get("vig_notes") or "No reason recorded."
        status = (
            _approved_status(sport, candidate, mlb_standing_authorized)
            if approved
            else "passed; no bet"
        )
        sections.append(
            "\n".join(
                [
                    f"MLB lineup recheck — {decision}",
                    f"Side: {side}",
                    f"Supported price: {_american_price(supported_price)}",
                    f"Bettable to: {_american_price(bettable_to)}",
                    f"Reason: {_concise_text(reason)}",
                    f"Size: {_size(candidate, entry)}",
                    f"Status: {status}",
                ]
            )
        )
    return "\n\n".join(sections)


def build_regular_review_report(
    schedule: dict[str, Any],
    sport: str,
    candidate_ids: list[str],
    mlb_standing_authorized: bool,
) -> str:
    candidates = {candidate_identity(item): item for item in parse_candidates(schedule)}
    reviewed = [candidates[identity] for identity in candidate_ids]
    approved = sum(candidate.get("vig_approved") is True for candidate in reviewed)
    rejected = sum(candidate.get("vig_approved") is False for candidate in reviewed)
    lines = [f"{sport} card review — {approved} approved, {rejected} rejected"]
    for candidate in reviewed:
        is_approved = candidate.get("vig_approved") is True
        decision = "APPROVED" if is_approved else "PASSED"
        side = _concise_text(candidate.get("side") or candidate.get("game"), 80)
        reason = _concise_text(candidate.get("vig_notes") or "No reason recorded.")
        line = f"- {decision} {side}: {reason}"
        if is_approved:
            line += (
                f" Size: {_size(candidate)}; "
                f"status: {_approved_status(sport, candidate, mlb_standing_authorized)}"
            )
        lines.append(line)
    return "\n".join(lines)


def build_validated_review_report(
    schedule: dict[str, Any],
    sport: str,
    candidate_ids: list[str],
    watchlist_ids: list[str],
    mlb_standing_authorized: bool,
) -> str:
    """Build output exclusively from the validated persisted schedule state."""
    sections: list[str] = []
    if candidate_ids:
        sections.append(
            build_regular_review_report(
                schedule, sport, candidate_ids, mlb_standing_authorized
            )
        )
    if watchlist_ids:
        sections.append(
            build_lineup_recheck_report(
                schedule, watchlist_ids, sport, mlb_standing_authorized
            )
        )
    return "\n\n".join(sections)


def run_gate(sport: str) -> int:
    day = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    schedule_path = _schedule_path(sport, day)
    if not schedule_path.exists():
        return 0
    try:
        data = json.loads(schedule_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"{sport} review gate ERROR: invalid schedule JSON: {exc}")
        return 1
    if isinstance(data, list):
        if not data:
            return 0
        print(f"{sport} review gate ERROR: non-empty legacy array schedule requires migration")
        return 1
    elif isinstance(data, dict):
        schedule = data
    else:
        print(f"{sport} review gate ERROR: expected object or list, got {type(data).__name__}")
        return 1
    try:
        candidates, watchlist = review_work(schedule, sport)
    except (ScheduleFormatError, WatchlistFormatError) as exc:
        print(f"{sport} review gate ERROR: {exc}")
        return 1
    if not candidates and not watchlist:
        return 0

    candidate_ids = [candidate_identity(candidate) for candidate in candidates]
    watchlist_ids = [str(entry["id"]) for entry in watchlist]
    mlb_standing_authorized = sport.upper() == "MLB" and standing_authorization_enabled()

    prompts: list[str] = []
    if candidates:
        prompts.append(
            build_regular_review_prompt(
                sport, day, schedule_path, candidates, mlb_standing_authorized
            )
        )
    if watchlist:
        prompts.append(build_lineup_recheck_prompt(schedule_path, watchlist))
    prompt = "\n\n".join(prompts)
    cmd = [
        HERMES,
        "--profile",
        "vig",
        "--skills",
        "sports-betting-markets,sports-data-apis",
        "chat",
        "-q",
        prompt,
        "-t",
        "file,web,skills,sports-data",
        "--quiet",
    ]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print(
            f"{sport} review gate ERROR: child reviewer timed out; reviewed state was not "
            "accepted. Retry the job and inspect Vig session logs."
        )
        return 1
    except OSError:
        print(
            f"{sport} review gate ERROR: child reviewer could not start; reviewed state was "
            "not accepted. Verify the Hermes CLI and retry the job."
        )
        return 1
    if proc.returncode:
        print(
            f"{sport} review gate ERROR: child reviewer exited {proc.returncode}; "
            "reviewed state was not accepted. Retry the job and inspect Vig session logs."
        )
        return proc.returncode

    try:
        updated = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{sport} review gate ERROR: could not validate reviewed state: {exc}")
        return 1
    if not isinstance(updated, dict):
        print(f"{sport} review gate ERROR: reviewed schedule must remain an object")
        return 1
    def _restore_pre_review_state(reason: str) -> None:
        """A rejected review must not stay live on disk where the poller reads it."""
        try:
            persist_schedule_locked(schedule_path, schedule)
            print(f"{sport} review gate: pre-review schedule restored after {reason}")
        except OSError as exc:
            print(
                f"{sport} review gate CRITICAL: could not restore pre-review schedule "
                f"after {reason}: {exc}; manual inspection required: {schedule_path}"
            )

    normalization_errors = normalize_review_routing(
        schedule, updated, sport, mlb_standing_authorized, day=day
    )
    if normalization_errors:
        print(
            f"{sport} review gate ERROR: routing normalization failed closed: "
            f"{'; '.join(normalization_errors)}"
        )
        _restore_pre_review_state("normalization failure")
        return 1
    transition_errors = validate_review_transition(
        schedule,
        updated,
        candidate_ids,
        watchlist_ids,
        sport,
        mlb_standing_authorized,
    )
    if transition_errors:
        print(f"{sport} review gate ERROR: invalid review transition: {'; '.join(transition_errors)}")
        _restore_pre_review_state("transition validation failure")
        return 1
    try:
        write_latest_action(sport, day, updated, mlb_standing_authorized)
        persist_schedule_locked(schedule_path, updated)
    except (OSError, ScheduleFormatError) as exc:
        print(f"{sport} review gate ERROR: could not persist reviewed state: {exc}")
        return 1

    report = build_validated_review_report(
        updated,
        sport,
        candidate_ids,
        watchlist_ids,
        mlb_standing_authorized,
    )
    if report:
        print(report)
    return 0
