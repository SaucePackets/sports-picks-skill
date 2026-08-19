#!/usr/bin/env python3
"""Conditional Vig review gate shared by MLB and soccer cron wrappers."""

from __future__ import annotations

import fcntl
import json
import math
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
    PENDING_STATUS,
    WatchlistFormatError,
    build_recheck_prompt,
    due_entries,
    fetch_lineup_snapshot,
    stale_invalid_watchlist,
    validate_watchlist,
)
from mlb_runtime_policy import (  # noqa: E402
    enforce_daily_candidate_limit,
    live_conservative_edge,
    load_mlb_selection_policy,
    stale_probability_field_errors,
    standing_authorization_enabled,
)
from mlb_baseball_evidence import (  # noqa: E402
    baseball_evidence_errors,
    execution_checks_errors,
    review_prompt_evidence_section,
)
from mlb_probability_model import (  # noqa: E402
    probability_component_errors,
    probability_contract_prompt_section,
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
        and math.isfinite(value)
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
        # Probability contract at ROUTING time: a standing-authorized candidate
        # must already carry the full numeric probability trail with a live
        # recomputed edge. NaN/Inf fields are rejected here (not just at the
        # execution gate and final lock) so a poisoned candidate never reaches
        # the rewrite below. `stale_probability_field_errors` rejects missing,
        # non-numeric, non-finite, out-of-range, and stale-edge fields.
        contract_errors = stale_probability_field_errors(candidate)
        if contract_errors:
            errors.append(
                f"candidate {identity} probability contract violation: "
                + "; ".join(contract_errors)
            )
        # Baseball evidence (Phase 2 hard validators): a standing-authorized
        # candidate must have deterministic starter role, resolved named risks,
        # and a valid support layer. Fails closed before daily-cap ranking.
        baseball_errors = baseball_evidence_errors(candidate)
        if baseball_errors:
            errors.append(
                f"candidate {identity} baseball evidence violation: "
                + "; ".join(baseball_errors)
            )
        # Probability components (Phase 3): every point of disagreement with
        # the DK-fair market prior must be an explicit named component, and
        # the components must reconcile with the stated probability trail.
        component_errors = probability_component_errors(candidate)
        if component_errors:
            errors.append(
                f"candidate {identity} probability component violation: "
                + "; ".join(component_errors)
            )
        # Execution checks (Phase 2): confirm tradeability without touching
        # probability. A candidate missing these fails closed at routing.
        exec_errors = execution_checks_errors(candidate)
        if exec_errors:
            errors.append(
                f"candidate {identity} execution checks violation: "
                + "; ".join(exec_errors)
            )
    if errors:
        return errors

    # Daily candidate limit: approved candidates for the day (pre-existing plus
    # newly approved) may not exceed the shared policy cap. Rank by live
    # conservative edge and reject the tail even when each price passes alone.
    # Missing/invalid policy FAILS CLOSED: no standing-authorized routing can
    # proceed without the shared machine-readable rails.
    #
    # The cap pool is the set of candidates that WILL route to standing
    # authorization: candidates already standing-authorized plus every newly
    # approved MLB child. Newly approved children arrive in manual state and
    # are rewritten to standing_authorized below, so they must count against
    # the cap BEFORE the rewrite — filtering on execution_mode here would let
    # three manual-state approvals bypass the cap and then all be rewritten.
    # Genuinely manual-only candidates (never rewritten) are excluded by
    # membership in newly_approved, not by transient execution_mode.
    policy = load_mlb_selection_policy()
    if policy is None:
        return [
            "shared MLB selection policy missing or invalid in risk_limits.json; "
            "standing-authorized routing is disabled until the policy block loads"
        ]
    newly_approved_identities = {
        candidate_identity(candidate) for candidate in newly_approved
    }
    day_approved = [
        candidate
        for candidate in after_candidates
        if candidate.get("vig_approved") is True
        and (
            candidate.get("execution_mode") == "standing_authorized"
            or candidate_identity(candidate) in newly_approved_identities
        )
    ]
    kept, rejected = enforce_daily_candidate_limit(day_approved, policy)
    if rejected:
        return [
            f"daily candidate limit {policy.max_mlb_official_bets_per_day} "
            f"exceeded: {len(day_approved)} approved, rejected "
            + ", ".join(candidate_identity(c) for c in rejected)
        ]

    after["sport"] = "MLB"
    after["market_type"] = "moneyline"
    if day:
        # The execution gate fails closed on a wrong "date" header; stamp the
        # schedule's own day so approved candidates are actually executable.
        after["date"] = day
    for candidate, ask in prices:
        # The executable ceiling is the shared-policy ceiling
        # (conservative_probability - min_conservative_edge), never the current
        # ask: a valid later fill between the original ask and the true ceiling
        # must not be rejected. Fail closed when the candidate cannot produce
        # a ceiling (missing/non-numeric conservative probability).
        ceiling = candidate.get("conservative_probability")
        if not (
            isinstance(ceiling, (int, float))
            and not isinstance(ceiling, bool)
            and 0 < ceiling < 1
        ):
            return [
                f"candidate {candidate_identity(candidate)} cannot derive an "
                "executable ceiling: conservative_probability missing or invalid"
            ]
        executable_ceiling = policy.ceiling_for(float(ceiling))
        if not 0 < executable_ceiling < 1:
            return [
                f"candidate {candidate_identity(candidate)} has a non-positive "
                f"executable ceiling ({executable_ceiling}); edge does not clear "
                "the policy floor"
            ]
        candidate.update(
            sport="MLB",
            market_type="moneyline",
            execution_mode="standing_authorized",
            execution_status="pending",
            executed=False,
            max_polymarket_price=executable_ceiling,
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
    if max_price is None or (
        not isinstance(max_price, (int, float))
        or isinstance(max_price, bool)
        or not 0 < max_price < 1
    ):
        errors.append("max_polymarket_price must be between 0 and 1")
    # Probability contract: a standing-authorized approval must carry the full
    # numeric probability trail and a stored edge that matches the live
    # recomputation. Missing or stale fields make the approval invalid.
    errors.extend(stale_probability_field_errors(candidate))
    # Baseball evidence hard validators (Phase 2): separate baseball gates from
    # execution checks so a candidate cannot route on price/liquidity alone.
    errors.extend(
        f"baseball evidence: {message}"
        for message in baseball_evidence_errors(candidate)
    )
    # Probability components (Phase 3): the structured component contract must
    # reconcile with the stated probability trail before approval is valid.
    errors.extend(
        f"probability components: {message}"
        for message in probability_component_errors(candidate)
    )
    # Execution checks hard validators (Phase 2): separate tradeability gates.
    errors.extend(
        f"execution checks: {message}"
        for message in execution_checks_errors(candidate)
    )
    # Edge floor: the live conservative edge must clear the shared policy
    # floor (default 5 points). The haircut is an uncertainty buffer, never a fee.
    # A missing/invalid policy FAILS CLOSED — the approval is invalid without
    # the shared machine-readable rail.
    policy = load_mlb_selection_policy()
    if policy is None:
        errors.append(
            "shared MLB selection policy missing or invalid in risk_limits.json; "
            "standing-authorized approval is invalid until the policy block loads"
        )
    else:
        live = live_conservative_edge(candidate)
        if live is not None and live + 1e-9 < policy.min_conservative_edge:
            errors.append(
                f"live conservative edge {live:.4f} is below the shared policy "
                f"floor min_conservative_edge={policy.min_conservative_edge}"
            )
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
    deferral_eligible_ids: set[str] | None = None,
) -> list[str]:
    # deferral_eligible_ids: entry ids whose live inputs (price or lineup
    # feed) were machine-verified unavailable while building the recheck
    # prompt. Only these may remain an unchanged pending no-op; the default
    # None means no entry is eligible, so callers that do not supply the
    # evidence fail closed.
    errors: list[str] = []
    watch_errors = validate_watchlist(after)
    # A review may never INTRODUCE or EDIT an invalid watchlist entry, but a
    # pre-existing invalid entry the review did not touch (byte-identical in
    # before and after — e.g. historical garbage written outside this gate)
    # must not wedge an unrelated review all day. due_entries already refuses
    # to route such entries.
    before_entries = {
        json.dumps(item, sort_keys=True)
        for item in before.get("lineup_watchlist", [])
        if isinstance(item, dict)
    }
    def _entries_for_label(label: str) -> list[dict[str, Any]]:
        raw = after.get("lineup_watchlist", [])
        if not isinstance(raw, list):
            return []
        matches: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id == label or (not item_id and str(index) == label):
                matches.append(item)
        return matches
    for entry_id, entry_errors in watch_errors.items():
        matches = _entries_for_label(entry_id)
        # Suppress only the unambiguous case: exactly one entry carries this
        # label and it is byte-identical to a pre-review entry. Duplicated ids
        # or anything the review touched still fail.
        if len(matches) == 1 and json.dumps(matches[0], sort_keys=True) in before_entries:
            continue
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
        if status == PENDING_STATUS and entry == before_watch.get(entry_id):
            # Deferred no-op: acceptable ONLY when this cycle machine-verified
            # an unavailable input (live price fetch or lineup snapshot failed)
            # for this exact entry. The entry stays due and is routed again
            # next cycle. An unchanged pending entry whose inputs WERE
            # available is an unreviewed entry, not a defer, and fails closed;
            # a pending entry that was EDITED still fails below.
            if entry_id in (deferral_eligible_ids or ()):
                continue
            errors.append(
                f"watchlist {entry_id} was left pending without a machine-verified "
                "unavailable input (price and lineup feeds resolved); the recheck "
                "must reach promoted or passed"
            )
            continue
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
    policy = load_mlb_selection_policy()
    if policy is None:
        # Fail closed in the prompt too: with no loadable shared policy the
        # reviewer must know every approval will be rejected at validation, not
        # be quoted a hard-coded floor that suggests routing can succeed.
        edge_floor_text = (
            "UNAVAILABLE — the shared MLB selection policy in risk_limits.json is "
            "missing or invalid, so every standing-authorized approval will be "
            "rejected deterministically at validation; do not approve for execution"
        )
    else:
        edge_floor_text = f"{policy.min_conservative_edge}"
    edge_floor = policy.min_conservative_edge if policy is not None else 0.05
    # Phase 2: the hard validators reject any standing-authorized approval that
    # lacks structured baseball_evidence/execution_checks, so the reviewer must
    # be handed the schema in the same prompt. Soccer/manual reviews carry no
    # evidence contract and stay unchanged.
    evidence_section = (
        "\n"
        + review_prompt_evidence_section()
        + "\n\n"
        + probability_contract_prompt_section()
        + "\n"
        if sport.upper() == "MLB" and mlb_standing_authorized
        else ""
    )
    return f"""You are Vig performing the independent {sport} card review for {day}.
Read {schedule_path}. Review only pending candidates: {sides}. Refresh decisive
inputs and current supported-market prices, then apply every original hard gate.
Update each reviewed candidate with boolean vig_approved and concise vig_notes.

PRICE DISCIPLINE (the ceiling is the ONLY guardrail — do NOT do fee arithmetic):
Start from de-vigged DraftKings fair probability (dk_fair_prob) as the market
prior, apply your adjustments as explicit components to get raw_probability, then
apply the documented uncertainty_haircut to get conservative_probability — the
ONLY probability used for edge and execution. The haircut is a model-uncertainty
buffer, NEVER a venue fee. Recompute the edge from the refreshed handicap, then
set the price ceiling
max_polymarket_price = conservative_probability - {edge_floor} (the shared policy
minimum conservative edge, currently {edge_floor_text}). Judge
price on the REAL cost to buy: approve whenever the current executable ask — what
you would actually pay right now — is at or under that ceiling; reject on price
only when the live ask is above the ceiling. This is fee-agnostic on purpose: any
venue fee is already baked into the executable price, so paying it is fine AS LONG
AS the all-in price stays at or under the ceiling. Polymarket US currently charges
ZERO fees (confirmed 0 bps on every executed receipt), so today the executable ask
equals the quoted ask. Never invent, add, or subtract a phantom fee (no 0.024, no
2.4% rail). Execution is an IOC limit placed AT the ceiling, so the book can only
ever fill at or under your number and can never push you past the suggested odds.

PROBABILITY CONTRACT (required on every approved MLB candidate — an approval
missing any of these is invalid and will be rejected deterministically):
dk_fair_prob, raw_probability, uncertainty_haircut, conservative_probability,
current_ask, projected_edge_at_current_ask, model_version. Set
projected_edge_at_current_ask = conservative_probability - current_ask from the
REFRESHED price; the morning net_edge is never carried forward as the executed
edge. The edge must clear the shared {edge_floor:.2f} floor AFTER the haircut.
{evidence_section}
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


def _price_context(watchlist: list[dict[str, Any]]) -> tuple[str, set[str]]:
    """Return the price prompt section plus the ids whose live price was
    machine-verified unavailable (slug resolved, fetch failed). A missing slug
    is a data defect, not a transient outage, so it earns no deferral."""
    lines: list[str] = []
    no_price_ids: set[str] = set()
    for entry in watchlist:
        slug = _entry_slug(entry)
        if not slug:
            lines.append(f"{entry.get('id')}: no Polymarket slug resolvable")
            continue
        price = fetch_market_price(slug)
        if not price:
            no_price_ids.add(str(entry.get("id")))
            lines.append(f"{entry.get('id')}: current price unavailable ({slug})")
            continue
        lines.append(
            f"{entry.get('id')} [{slug}]: market open={price['open']} ({price['reason']}), "
            f"book={price['book_state']}, long/YES ask={price['long_ask']}, "
            f"NO-side ask={price['no_ask']}"
        )
    if not lines:
        return "", no_price_ids
    return (
        "\n\nDeterministic Polymarket US prices (fetched in-process — DO NOT web-search "
        "or curl for price; match your side to the slate-captured ask in the thesis):\n"
        + "\n".join(lines)
    ), no_price_ids


def build_lineup_recheck_prompt(
    schedule_path: Path, watchlist: list[dict[str, Any]]
) -> tuple[str, set[str]]:
    """Fetch schedule-mapped MLB feeds + deterministic prices for the review.

    Returns the prompt and the machine-verified deferral-eligible entry ids:
    entries whose live price fetch or lineup snapshot fetch failed this cycle.
    Only these ids may legitimately remain an unchanged pending no-op at the
    review transition; validate_review_transition fails every other unchanged
    pending entry closed.
    """
    snapshots: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    for entry in watchlist:
        entry_id = str(entry.get("id", "<missing-id>"))
        try:
            snapshots[entry_id] = fetch_lineup_snapshot(entry)
        except Exception:
            unavailable.append(entry_id)
    prompt = build_recheck_prompt(schedule_path, watchlist, snapshots)
    price_context, no_price_ids = _price_context(watchlist)
    prompt += price_context
    # Phase 3: a promoted candidate must carry the structured probability
    # component contract or the execution gate will reject it deterministically.
    prompt += "\n" + probability_contract_prompt_section() + "\n"
    if unavailable:
        prompt += (
            "\nMLB lineup lookup was unavailable for: "
            + ", ".join(unavailable)
            + ". Fail the lineup-confirmation gate unless another live source verifies it.\n"
        )
    return prompt, no_price_ids | set(unavailable)


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
        deferred = entry.get("status") == PENDING_STATUS
        candidate = entry.get("promoted_candidate") if approved else {}
        if not isinstance(candidate, dict):
            candidate = {}
        decision = "APPROVED" if approved else ("DEFERRED" if deferred else "PASSED")
        side = _concise_text(candidate.get("side") or entry.get("side"), 80)
        supported_price = _watchlist_supported_price(candidate, entry)
        bettable_to = candidate.get("bettable_to_price", entry.get("bettable_to_price"))
        reason = entry.get("recheck_notes") or candidate.get("vig_notes") or "No reason recorded."
        if approved:
            status = _approved_status(sport, candidate, mlb_standing_authorized)
        elif deferred:
            status = "still pending recheck; no bet"
        else:
            status = "passed; no bet"
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
    if sport == "MLB":
        # Invalid entries whose first pitch already passed are dead as routing
        # inputs (due_entries quarantines them); surface them alongside real
        # review work instead of failing every remaining run of the day closed.
        # Printed only when the gate has work, so quiet cycles stay silent.
        for label, messages in sorted(stale_invalid_watchlist(schedule).items()):
            print(
                f"{sport} review gate NOTICE: quarantined invalid historical watchlist "
                f"entry {label} (first pitch passed, never routable): {'; '.join(messages)}"
            )

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
    deferral_eligible_ids: set[str] = set()
    if watchlist:
        recheck_prompt, deferral_eligible_ids = build_lineup_recheck_prompt(
            schedule_path, watchlist
        )
        prompts.append(recheck_prompt)
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
        deferral_eligible_ids,
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
