#!/usr/bin/env python3
"""Conditional Vig review gate shared by MLB and soccer cron wrappers."""

from __future__ import annotations

import copy
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
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
    PROMOTED_STATUS,
    WatchlistFormatError,
    build_recheck_prompt,
    due_entries,
    entry_id as watchlist_entry_id,
    expire_dead_pending_entries,
    fetch_lineup_snapshot,
    overdue_recheck_warnings,
    stale_invalid_watchlist,
    unreachable_first_pitch_ids,
    unreachable_first_pitch_warnings,
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
from vig_run_journal import (  # noqa: E402
    KIND_DATA_DEFECT,
    KIND_OUTAGE,
    OUTCOME_ERROR,
    OUTCOME_NO_SCHEDULE,
    OUTCOME_NO_WORK,
    OUTCOME_RECORDER_FAILED,
    OUTCOME_REVIEWED,
    SOURCE_LINEUP_FEED,
    SOURCE_PRICE_FEED,
    build_record,
    deferral,
    journal_path,
    read_records,
    record_run,
)
import mlb_game_reads  # noqa: E402

HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local/bin/hermes")


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


def _strict_approved_ask(candidate: dict[str, Any]) -> int | float | None:
    """Return only the explicit approved ask required on every MLB approval.

    Legacy captured_polymarket_ask / polymarket_ask fields are deliberately
    NOT a fallback: routing on a slate-time price would bypass the price the
    reviewer actually approved.
    """
    value = candidate.get("approved_polymarket_ask")
    return value if _strict_price(value) else None


_ASK_AGREEMENT_TOLERANCE = 1e-6


def _approved_ask_agreement_errors(candidate: dict[str, Any]) -> list[str]:
    """The stamped approved ask must BE the refreshed live price.

    A strict-numeric approved_polymarket_ask alone proves only that the child
    wrote a number; it is meaningful only when it equals the refreshed price
    contract the same review recorded — the probability trail's current_ask
    and execution_checks.supported_price. Disagreement means the approval was
    priced off something other than the live book — fail closed. A missing or
    non-strict approved ask is reported by the strict check, not here.
    """
    approved = candidate.get("approved_polymarket_ask")
    if not _strict_price(approved):
        return []
    errors: list[str] = []
    current = candidate.get("current_ask")
    if _strict_price(current) and abs(approved - current) > _ASK_AGREEMENT_TOLERANCE:
        errors.append(
            f"approved_polymarket_ask {approved:.6f} does not match the "
            f"refreshed current_ask {current:.6f}"
        )
    checks = candidate.get("execution_checks")
    supported = checks.get("supported_price") if isinstance(checks, dict) else None
    if _strict_price(supported) and abs(approved - supported) > _ASK_AGREEMENT_TOLERANCE:
        errors.append(
            f"approved_polymarket_ask {approved:.6f} does not match "
            f"execution_checks.supported_price {supported:.6f}"
        )
    return errors


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


# The fields that address a candidate at the market: which market, and which
# side of it. A promotion is corroborated against these and never against a
# free-text game name, because the two vocabularies (the slate's and the
# reviewer's) are not guaranteed to spell a club the same way.
PROMOTION_ADDRESS_FIELDS = ("polymarket_slug", "market_slug", "event_id")

AGREE = "agree"
DISAGREE = "disagree"
UNKNOWN = "unknown"


def _address_value(value: Any) -> str | None:
    """A comparable address, or None when the field carries no address at all."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def promotion_address_agreement(
    candidate: dict[str, Any], promoted_candidate: Any
) -> str:
    """Does this promoted entry's own candidate address the same bet?

    Three answers, not two, and the distinction is the whole point. ``agree``
    means at least one address field is present on both sides and equal, with
    no present-on-both field disagreeing. ``disagree`` means some present-on-
    both field differs — a positive contradiction, which is evidence. ``unknown``
    means the two objects share no comparable field, which is an absence of
    evidence and must never be read as either.

    ``side`` participates as an address field: a promoted entry for the other
    side of the same market is a different bet, and a slug match alone would
    call it corroborated.
    """
    if not isinstance(promoted_candidate, dict):
        return UNKNOWN
    agreed = False
    for field in (*PROMOTION_ADDRESS_FIELDS, "side"):
        mine = _address_value(candidate.get(field))
        theirs = _address_value(promoted_candidate.get(field))
        if mine is None or theirs is None:
            continue
        if mine == theirs:
            agreed = True
        else:
            return DISAGREE
    return AGREE if agreed else UNKNOWN


def _promoted_entries(
    after: dict[str, Any], eligible_ids: set[Any]
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in after.get("lineup_watchlist", [])
        if isinstance(entry, dict)
        and entry.get("status") == PROMOTED_STATUS
        and entry.get("id") in eligible_ids
    ]


def resolve_promotion(
    candidate: dict[str, Any], promoted: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Pair a newly approved candidate with the watchlist entry it came from.

    The defect this replaces: the gate recognised a promotion only when the
    ``candidates[]`` element carried a ``watchlist_id``, and that field is
    hand-copied by the child reviewer from the entry it also hand-writes. On
    2026-09-03 the same reviewer wrote a complete ``promoted_candidate``
    carrying ``LW20260903-TB-001`` and then appended a ``candidates[]`` element
    with no ``watchlist_id`` at all — twice, at 22:46 and 23:01, both refused —
    and got it right on the third pass at 23:16. Nothing raced; a rail was
    keyed on a transcription.

    So the pairing is done by CORROBORATION instead: the promoted entry's own
    ``promoted_candidate`` already carries the slug and the side, and those
    address the same bet or they do not. The rail stays closed — a candidate no
    entry corroborates is still refused — it just no longer depends on a string
    being copied twice.

    A ``watchlist_id`` the reviewer DID write is honoured, but it may not be
    positively contradicted by the entry it names: without that check a
    mis-stamped id would launder itself, because ``normalize_review_routing``
    overwrites the named entry's ``promoted_candidate`` with the candidate it
    matched. An entry that shares no comparable field is neither corroboration
    nor contradiction, so a stamped id survives it; an UNSTAMPED candidate
    needs positive corroboration and ``unknown`` is not enough.
    """
    claimed = candidate.get("watchlist_id")
    identity = candidate_identity(candidate)
    if claimed not in (None, ""):
        named = [entry for entry in promoted if entry.get("id") == claimed]
        if not named:
            return None, [
                f"candidate {identity} names watchlist_id {claimed!r}, which is not a "
                "promoted entry this review created"
            ]
        entry = named[0]
        if promotion_address_agreement(
            candidate, entry.get("promoted_candidate")
        ) == DISAGREE:
            return None, [
                f"candidate {identity} names watchlist_id {claimed!r}, but that entry's "
                "promoted_candidate addresses a different bet "
                f"({_promotion_address_label(entry.get('promoted_candidate'))})"
            ]
        return entry, []

    corroborating = [
        entry
        for entry in promoted
        if promotion_address_agreement(candidate, entry.get("promoted_candidate")) == AGREE
    ]
    if len(corroborating) == 1:
        return corroborating[0], []
    if not corroborating:
        if not promoted:
            return None, [
                f"candidate {identity} was not a targeted candidate and this review "
                "promoted no watchlist entry, so nothing corroborates it"
            ]
        return None, [
            f"candidate {identity} was not a targeted candidate and no promoted "
            "watchlist entry corroborates it; this review promoted "
            + ", ".join(
                f"{entry.get('id')!r} "
                f"({_promotion_address_label(entry.get('promoted_candidate'))})"
                for entry in promoted
            )
        ]
    return None, [
        f"candidate {identity} is corroborated by more than one promoted watchlist "
        "entry ("
        + ", ".join(repr(entry.get("id")) for entry in corroborating)
        + "); an ambiguous promotion has no fact to route on"
    ]


def _promotion_address_label(promoted_candidate: Any) -> str:
    if not isinstance(promoted_candidate, dict):
        return "no promoted_candidate"
    parts = [
        f"{field}={_address_value(promoted_candidate.get(field))!r}"
        for field in (*PROMOTION_ADDRESS_FIELDS, "side")
        if _address_value(promoted_candidate.get(field)) is not None
    ]
    return ", ".join(parts) or "no addressable fields"


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
    promoted_entries = _promoted_entries(after, before_watchlist_ids)
    newly_approved = [
        candidate
        for candidate in after_candidates
        if candidate.get("vig_approved") is True
        and before_by_id.get(candidate_identity(candidate), {}).get("vig_approved") is not True
    ]
    # Candidates whose approved ask passed the strict + agreement checks.
    # (Formerly (candidate, ask) pairs; the ask itself is fully consumed by
    # the agreement check above — the executable ceiling below is policy-
    # derived, never the ask.)
    routable: list[dict[str, Any]] = []
    # The watchlist entry each routable promotion came from, paired by
    # corroboration above. Kept so the read recorder below knows WHICH game was
    # promoted without re-deriving the pairing from the stamped id it just wrote.
    promotion_of: list[tuple[dict[str, Any], dict[str, Any]]] = []
    errors: list[str] = []
    for candidate in newly_approved:
        identity = candidate_identity(candidate)
        original = before_by_id.get(identity)
        promotion: dict[str, Any] | None = None
        if original is None:
            promotion, promotion_errors = resolve_promotion(candidate, promoted_entries)
            if promotion_errors:
                errors.extend(promotion_errors)
                continue
            # Stamp the corroborated id so every downstream consumer — the
            # promoted_candidate sync below, validate_review_transition's
            # entry-to-candidate map, the report — reads one field instead of
            # re-deriving the pairing three times.
            candidate["watchlist_id"] = promotion.get("id")
            identity = candidate_identity(candidate)
        # Regular card approvals and lineup promotions carry the SAME price
        # contract: the reviewer must stamp the explicit approved ask.
        ask = _strict_approved_ask(candidate)
        if ask is None:
            errors.append(
                f"candidate {candidate_identity(candidate)} has no strict numeric "
                "approved_polymarket_ask"
            )
        else:
            agreement_errors = _approved_ask_agreement_errors(candidate)
            if agreement_errors:
                errors.append(
                    f"candidate {identity} approved ask violation: "
                    + "; ".join(agreement_errors)
                )
            else:
                routable.append(candidate)
                if promotion is not None:
                    promotion_of.append((candidate, promotion))
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
    for candidate in routable:
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

    # A promoted game is now on the card, so its per-game read must say so.
    # The slate writes each read once and nothing had ever updated one on
    # promotion, which is why 2026-09-03's 23:30 recorder check reported "1
    # game_reads entries say 'candidate' but the schedule carries 2
    # candidates". Recorded here, in the same step that routes the promotion,
    # because a record written by a later job is a record that can be skipped.
    # Fails closed: the promotion is refused when the read cannot be found or
    # says the card refused the game.
    read_errors: list[str] = []
    for candidate, entry in promotion_of:
        read_errors.extend(
            mlb_game_reads.record_promotion_as_candidate(
                after,
                label=f"promoted candidate {candidate_identity(candidate)}",
                game_pk=entry.get("game_pk"),
                event_id=candidate.get("event_id") or entry.get("event_id"),
            )
        )
    if read_errors:
        return read_errors

    normalized_by_watchlist_id = {
        candidate.get("watchlist_id"): candidate
        for candidate in routable
        if candidate.get("watchlist_id")
    }
    for entry in after.get("lineup_watchlist", []):
        if not isinstance(entry, dict):
            continue
        normalized = normalized_by_watchlist_id.get(entry.get("id"))
        if normalized is not None and entry.get("status") == PROMOTED_STATUS:
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
    candidate: dict[str, Any],
    sport: str,
    mlb_standing_authorized: bool = False,
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
    # Every standing-authorized approval — regular card or lineup promotion —
    # must carry the explicit approved price; legacy ask fields never satisfy it.
    if not _strict_price(candidate.get("approved_polymarket_ask")):
        errors.append(
            "approved_polymarket_ask must be an unquoted numeric value strictly between 0 and 1"
        )
    # The stamped ask must also agree with the refreshed price contract the
    # same review wrote (current_ask, execution_checks.supported_price).
    errors.extend(_approved_ask_agreement_errors(candidate))
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
                errors.extend(
                    f"watchlist {entry_id} promoted candidate: {message}"
                    for message in approved_candidate_errors(
                        report_candidate, sport, mlb_standing_authorized
                    )
                )
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

APPROVED PRICE FIELD (the routing gate reads this exact key and fails closed
without it): on every approved candidate you MUST set approved_polymarket_ask
to the executable Polymarket US ask you are approving. It must be an unquoted
JSON number strictly between 0 and 1 — for example 0.47 — never a quoted string
such as "0.47", and never American odds such as 110 or -120. A quoted value, a
value at or outside 0 and 1, or a missing key is rejected deterministically and
the entire review is rolled back. approved_polymarket_ask, current_ask, and
execution_checks.supported_price must be the SAME number written identically
in all three fields — copy one value, never round or reformat any of them; a
mismatch is rejected deterministically and rolls back the review.
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


# Per-entry price markers. The defer instruction shown to the child and the
# validator's deferral_eligible_ids are derived from the SAME predicate below,
# so the prompt can never tell the child to defer an entry the transition
# validator will reject (the 08-23 review's P1: closed/unreliable markets
# showed an unavailable price without earning eligibility, deadlocking the
# child and rolling back the whole day's review).
PRICE_UNAVAILABLE_MARKER = "PRICE UNAVAILABLE this cycle: keep status pending_lineup_recheck"
PRICE_DEFECT_MARKER = (
    "DATA DEFECT (no resolvable Polymarket slug): not a transient outage — "
    "set status=passed with recheck_notes naming the missing slug"
)
LINEUP_UNAVAILABLE_MARKER = (
    "LINEUP FEED UNAVAILABLE this cycle: keep status pending_lineup_recheck"
)


def _price_is_usable(price: dict[str, Any] | None) -> bool:
    """A fetched price is usable only when the market is open for trading and
    both side asks are present. A closed market or an unreliable book (missing
    side, crossed, wide spread) returns a dict without raising, but hands the
    child no ask it may promote against — that is machine-verified
    unavailability, exactly like a failed fetch."""
    return (
        isinstance(price, dict)
        and bool(price.get("open"))
        and price.get("long_ask") is not None
        and price.get("no_ask") is not None
    )


def _price_context(
    watchlist: list[dict[str, Any]],
    deferrals: list[dict[str, Any]] | None = None,
) -> tuple[str, set[str]]:
    """Return the price prompt section plus the ids whose live price was
    machine-verified unavailable this cycle: the fetch failed, the market is
    not open, or the book is unreliable (a side ask is None). A missing slug
    is a data defect, not a transient outage, so it earns no deferral — its
    prompt line instructs a decisive pass instead.

    ``deferrals`` is an optional sink the caller passes to capture the same
    facts for the run journal. The prompt line and the journal entry are
    built from one branch each so the record can never claim a different
    reason from the one the reviewer was told.
    """
    lines: list[str] = []
    no_price_ids: set[str] = set()

    def _note(entry_id: Any, reason: str, kind: str = KIND_OUTAGE) -> None:
        if deferrals is not None:
            deferrals.append(deferral(entry_id, SOURCE_PRICE_FEED, reason, kind=kind))

    for entry in watchlist:
        slug = _entry_slug(entry)
        if not slug:
            lines.append(
                f"{entry.get('id')}: no Polymarket slug resolvable — {PRICE_DEFECT_MARKER}"
            )
            # A data defect, not an outage: recorded so it is visible, but
            # deliberately not deferral-eligible. The kind carries that, so
            # the record no longer needs a reason string arguing with the
            # field it sits in (Reviewer, PR #60).
            _note(
                entry.get("id"),
                "no Polymarket slug resolvable",
                kind=KIND_DATA_DEFECT,
            )
            continue
        price = fetch_market_price(slug)
        if not price:
            no_price_ids.add(str(entry.get("id")))
            lines.append(
                f"{entry.get('id')}: current price unavailable ({slug}) — "
                f"{PRICE_UNAVAILABLE_MARKER}"
            )
            _note(entry.get("id"), f"current price unavailable ({slug})")
            continue
        if _price_is_usable(price):
            lines.append(
                f"{entry.get('id')} [{slug}]: market open={price['open']} ({price['reason']}), "
                f"book={price['book_state']}, long/YES ask={price['long_ask']}, "
                f"NO-side ask={price['no_ask']}"
            )
        else:
            # Never print tradable-looking asks on an unusable quote — a
            # closed market can still carry numbers in the book, and showing
            # them next to the defer marker invites pricing off a market that
            # cannot be traded.
            no_price_ids.add(str(entry.get("id")))
            lines.append(
                f"{entry.get('id')} [{slug}]: market open={price['open']} ({price['reason']}), "
                f"book={price['book_state']}, no executable ask — "
                f"{PRICE_UNAVAILABLE_MARKER}"
            )
            _note(
                entry.get("id"),
                f"no executable ask ({slug}); open={price['open']} ({price['reason']}), "
                f"book={price['book_state']}",
            )
    if not lines:
        return "", no_price_ids
    return (
        "\n\nDeterministic Polymarket US prices (fetched in-process — DO NOT web-search "
        "or curl for price; match your side to the slate-captured ask in the thesis):\n"
        + "\n".join(lines)
    ), no_price_ids


def build_lineup_recheck_prompt(
    schedule_path: Path,
    watchlist: list[dict[str, Any]],
    deferrals: list[dict[str, Any]] | None = None,
) -> tuple[str, set[str]]:
    """Fetch schedule-mapped MLB feeds + deterministic prices for the review.

    Returns the prompt and the machine-verified deferral-eligible entry ids:
    entries whose live price fetch or lineup snapshot fetch failed this cycle.
    Only these ids may legitimately remain an unchanged pending no-op at the
    review transition; validate_review_transition fails every other unchanged
    pending entry closed.

    ``deferrals`` is an optional sink for the run journal, filled from the
    same branches that build the prompt lines. A returned id says an entry
    was deferred; the sink says which feed reported it, why, and when — which
    is the difference between knowing the 08-16 entries went unreviewed and
    knowing the lineup lane was down when they did.
    """
    snapshots: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    for entry in watchlist:
        entry_id = str(entry.get("id", "<missing-id>"))
        try:
            snapshots[entry_id] = fetch_lineup_snapshot(entry)
        except Exception as exc:
            unavailable.append(entry_id)
            if deferrals is not None:
                deferrals.append(
                    deferral(
                        entry_id,
                        SOURCE_LINEUP_FEED,
                        f"lineup snapshot fetch failed: {type(exc).__name__}: {exc}"[:300],
                    )
                )
    prompt = build_recheck_prompt(schedule_path, watchlist, snapshots)
    price_context, no_price_ids = _price_context(watchlist, deferrals)
    prompt += price_context
    # Phase 3: a promoted candidate must carry the structured probability
    # component contract or the execution gate will reject it deterministically.
    prompt += "\n" + probability_contract_prompt_section() + "\n"
    if unavailable:
        # A feed outage is machine-verified unavailability, symmetric with the
        # price side: these ids are deferral-eligible, so the instruction must
        # be defer — not "fail the gate", which routes a live candidate to a
        # terminal passed (the discard shape that swallowed the 08-16/08-18
        # winners when the lane was down).
        prompt += (
            "\nMLB lineup lookup was unavailable for: "
            + ", ".join(unavailable)
            + f" — {LINEUP_UNAVAILABLE_MARKER} (do not pass, do not"
            " promote) and let a later recheck see the posted lineups. Do NOT"
            " treat a feed outage as unconfirmed lineups.\n"
        )
    return prompt, no_price_ids | set(unavailable)


def _schedule_path(sport: str, day: str) -> Path:
    if sport == "MLB":
        return ROOT / ".picks" / "execute" / f"{day}-schedule.json"
    return ROOT / ".picks" / "execute" / "intl-soccer" / f"{day}-schedule.json"


def refused_review_path(sport: str, day: str, stamp: str) -> Path:
    return ROOT / ".picks" / "refused" / f"{day}-{sport.lower()}-{stamp}.json"


def persist_refused_review(
    sport: str,
    day: str,
    reviewed: dict[str, Any],
    stage: str,
    detail: str,
    now: datetime | None = None,
) -> Path | None:
    """Keep the reviewed state a refusal is about to throw away.

    ``_restore_pre_review_state`` overwrites the schedule with the pre-review
    copy, which is right — a rejected review must not stay live where the
    execution poller reads it — but it was also the only copy. Diagnosing the
    2026-09-03 refusals meant reading an agent session database on the VPS to
    recover what the reviewer had actually written, and nothing in ``.picks``
    recorded that a review had been refused at all. The journal records the
    refusal; this records the ARTIFACT, which is the half you need to tell a
    reviewer mistake from a gate defect.

    Never raises and never changes the verdict: a refusal that could not be
    archived is still a refusal, and an observability write that can fail the
    gate is a new failure mode bolted onto an old one (PR #60).
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    path = refused_review_path(sport, day, stamp)
    payload = {
        "day": day,
        "sport": sport.upper(),
        "stage": stage,
        "detail": detail,
        "refused_at_utc": stamp,
        # The reviewed state as written, unrepaired. It is evidence, not a
        # schedule: nothing reads this back into the pipeline.
        "reviewed_schedule": reviewed,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        return None
    return path


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


def schedule_day_now() -> str:
    """The Chicago calendar day whose schedule file this run reads.

    Extracted so a caller — in practice a test — can derive the day from the
    SAME function the gate uses instead of making its own clock call. Two
    independent now() calls straddling Chicago midnight write one day's schedule
    file and read another, which is a rare flake that looks like a gate bug
    (Reviewer, PR #57; pre-existing from #56).
    """
    return datetime.now(ZoneInfo("America/Chicago")).date().isoformat()


def journal_gate_run(
    sport: str,
    day: str,
    outcome: str,
    stage: str,
    *,
    detail: str = "",
    schedule_path: Path | None = None,
    counts: dict[str, int] | None = None,
    notices: list[str] | None = None,
    deferrals: list[dict[str, Any]] | None = None,
) -> str | None:
    """Journal one gate outcome and report — never raise, never re-decide.

    A journal failure is printed as its own CRITICAL line and nothing else:
    the gate's verdict is authoritative, and failing a review because a log
    line could not be written would add an outage mode to the lane whose
    actual problem is losing work silently. Returns the error text so a test
    can assert on it directly rather than parsing stdout.
    """
    try:
        record = build_record(
            sport=sport,
            day=day,
            outcome=outcome,
            stage=stage,
            detail=detail,
            schedule_path=schedule_path,
            counts=counts or {},
            notices=notices or [],
            deferrals=deferrals or [],
        )
        error = record_run(ROOT, record)
    except Exception as exc:  # defensive: journalling must never propagate
        error = f"could not build run journal record: {type(exc).__name__}: {exc}"
    if error:
        print(f"{sport} review gate JOURNAL CRITICAL: {error}")
    return error


RECORDER_GAP_STAGE = "recorder_missing"
# The once-per-day check keys on THIS prefix, not on the stage, because the
# gap is reported from two branches and only one of them journals a
# `recorder_missing` stage. Keying on the stage alone would leave the
# has-work branch printing on every one of the day's ninety-six cycles while
# looking, in the code, exactly as throttled as the other one.
RECORDER_GAP_NOTICE_PREFIX = "game_reads gap:"


def recorder_gap_notice(errors: list[str]) -> str:
    """One notice text for both report sites, so the throttle can recognise it."""
    return (
        f"{RECORDER_GAP_NOTICE_PREFIX} {len(errors)} defect(s) on today's schedule; "
        "the day's refusals were not recorded: " + "; ".join(errors[:3])
    )


def _recorder_gap_already_reported(sport: str, day: str) -> bool:
    """True when today's journal already carries a recorder-gap report.

    Fails OPEN (False, so the notice prints) on any read problem: an
    unreadable journal must not be able to silence the one report that says
    the day's refusals went unrecorded. Duplicating a notice is cheap; losing
    it is the failure mode this whole lane is about.
    """
    try:
        records, _errors = read_records(journal_path(ROOT, day))
    except Exception:
        return False
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("sport") != sport or record.get("day") != day:
            continue
        if record.get("stage") == RECORDER_GAP_STAGE:
            return True
        entries = record.get("notices")
        if isinstance(entries, list) and any(
            isinstance(text, str) and text.startswith(RECORDER_GAP_NOTICE_PREFIX)
            for text in entries
        ):
            return True
    return False


def run_gate(sport: str) -> int:
    day = schedule_day_now()
    schedule_path = _schedule_path(sport, day)
    notices: list[str] = []
    if not schedule_path.exists():
        # A no-card day and a day the job never fired were previously the
        # same observation: nothing on disk either way. This is the record
        # that separates them.
        journal_gate_run(
            sport, day, OUTCOME_NO_SCHEDULE, "schedule_missing",
            detail="no schedule file for this day; nothing was collected",
            schedule_path=schedule_path,
        )
        return 0
    try:
        data = json.loads(schedule_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"{sport} review gate ERROR: invalid schedule JSON: {exc}")
        journal_gate_run(
            sport, day, OUTCOME_ERROR, "schedule_parse",
            detail=f"invalid schedule JSON: {exc}", schedule_path=schedule_path,
        )
        return 1
    if isinstance(data, list):
        if not data:
            journal_gate_run(
                sport, day, OUTCOME_NO_WORK, "schedule_empty",
                detail="empty legacy array schedule; no candidates",
                schedule_path=schedule_path,
            )
            return 0
        print(f"{sport} review gate ERROR: non-empty legacy array schedule requires migration")
        journal_gate_run(
            sport, day, OUTCOME_ERROR, "schedule_legacy_array",
            detail="non-empty legacy array schedule requires migration",
            schedule_path=schedule_path,
        )
        return 1
    elif isinstance(data, dict):
        schedule = data
    else:
        print(f"{sport} review gate ERROR: expected object or list, got {type(data).__name__}")
        journal_gate_run(
            sport, day, OUTCOME_ERROR, "schedule_type",
            detail=f"expected object or list, got {type(data).__name__}",
            schedule_path=schedule_path,
        )
        return 1
    try:
        candidates, watchlist = review_work(schedule, sport)
    except (ScheduleFormatError, WatchlistFormatError) as exc:
        print(f"{sport} review gate ERROR: {exc}")
        journal_gate_run(
            sport, day, OUTCOME_ERROR, "review_work",
            detail=str(exc), schedule_path=schedule_path,
        )
        return 1
    if sport == "MLB":
        # Rechecks belong to this lane, so this lane surfaces the zombies: a
        # valid pending entry that slid past its due window is otherwise
        # visible to no running job. `watchlist` is exactly this run's due set,
        # and those entries are being rechecked right now, so they are
        # excluded: overdue_recheck_warnings explains why an old due stamp on
        # live work is normal rather than a warning.
        #
        # This sits ABOVE the no-work early return, unlike the quarantine
        # notice below. The zombie the detector exists for is precisely an
        # abandoned entry with nothing else on the schedule — the last stuck
        # entry of the day, or a one-game slate — and below the return it is
        # reported only on cycles that happen to have other work, which is the
        # opposite of when it is needed. The cost is a delivery on otherwise
        # silent cycles. That is not the 08-11 agent-prompt hazard: run_gate
        # spawns the reviewer child itself, so this stdout is the job's
        # delivery, never a prompt handed to an agent.
        # entry_id, not a third normalisation: the exclusion set has to use the
        # same key both detectors skip on.
        in_flight = {watchlist_entry_id(entry) for entry in watchlist}
        # A previous-day typo satisfies BOTH detectors, and printing both put
        # the unreachable notice one line under the overdue warning for the same
        # entry — duplication, which is the alarm-fatigue axis this notice's
        # scoping exists to protect (Reviewer, PR #57). The unreachable notice
        # wins because it is strictly more informative: overdue says a deadline
        # passed, unreachable says the window can never open and names the day
        # the entry disagrees with. Nothing is lost, and the pair is now one
        # line per entry.
        unreachable = unreachable_first_pitch_ids(schedule, day, exclude_ids=in_flight)
        # A valid pending entry past its window is not just worth a warning —
        # it is dead: due_entries selects on the first-pitch window, so nothing
        # can ever recheck it again, and "pending" on disk kept it re-alerting
        # every cycle for the rest of the day. Expire it to an explicit
        # terminal state BEFORE the child runs, so the child reads the file
        # with the transition already applied and the untargeted-unchanged
        # rule holds. The persist failure path reverts the in-memory mutation
        # so before/after comparisons stay honest and the plain overdue
        # warning below takes over — a bookkeeping failure must degrade to the
        # old noise, never change what the gate accepts (same asymmetry as the
        # journal).
        expired_now = expire_dead_pending_entries(
            schedule, exclude_ids=in_flight | unreachable
        )
        if expired_now:
            try:
                persist_schedule_locked(schedule_path, schedule)
            except OSError as exc:
                for entry, _notice in expired_now:
                    entry["status"] = PENDING_STATUS
                    entry.pop("expired_at_utc", None)
                    entry.pop("expired_reason", None)
                notice = (
                    f"could not persist watchlist expiry ({exc}); entries left "
                    "pending for the next cycle"
                )
                print(f"{sport} review gate NOTICE: {notice}")
                notices.append(notice)
            else:
                for _entry, notice in expired_now:
                    print(f"{sport} review gate NOTICE: {notice}")
                    notices.append(notice)
        for warning in overdue_recheck_warnings(
            schedule, exclude_ids=in_flight | unreachable
        ):
            print(f"{sport} review gate NOTICE: {warning}")
            notices.append(warning)
        # Same lane, same reason, different way of going invisible: an entry
        # whose first pitch cannot fall on this schedule day has a recheck
        # window that never opens, so overdue_recheck_warnings — whose deadline
        # is derived from that same wrong number — stays silent on it too.
        # `day` is the one field the slate agent did not write.
        for warning in unreachable_first_pitch_warnings(
            schedule, day, exclude_ids=in_flight
        ):
            print(f"{sport} review gate NOTICE: {warning}")
            notices.append(warning)
    recorder_errors: list[str] = []
    if sport == "MLB":
        # A slate that reviews nothing still owes a row per scheduled game.
        # On 2026-09-01 the first slate carrying the recorder wrote a schedule
        # with no `game_reads` and no `slate_denominator`, reported success,
        # and this gate recorded `no_work` — the same record a genuinely empty
        # card produces. "We refused fifteen games" and "we recorded nothing"
        # became one observation, which is exactly the confusion the recorder
        # exists to end. The check is stdlib arithmetic over the file already
        # in hand: no network, no order behaviour, no change to what the gate
        # accepts or routes.
        #
        # WITH the denominator cross-check, not without it (PR #77 review).
        # `validate_game_reads` alone can only see a MISSING record. The
        # failure this lane was opened for is a TRIMMED one — a run that cut
        # `game_reads` and `slate_denominator` to the same short set is
        # internally consistent, passes that check, and journals
        # `no_reviewable_work`: the identical record the 09-01 run produced,
        # reached by a different route. Only the scan artifact, which the run
        # did not write, is an independent witness. Leaving that half in
        # commands nothing schedules would have reproduced this PR's own
        # diagnosis — a sentence in a prompt rather than a rail.
        #
        # Via `mlb_game_reads`, deliberately: it is in deploy-runtime.sh's
        # PROFILE_MANIFEST and `mlb_slate_receipt` is not, so importing the
        # receipt here would pass every test in this repo and ImportError on
        # the runtime's profile-local copies.
        #
        # The policy is loaded and PASSED, not defaulted. `price_discipline` is
        # the rail the slate refuses on most often and the only one whose truth
        # depends on a number this repo does not hold; a validator that guessed
        # the floor, or quietly skipped the rail when it could not find one,
        # would report a clean record for a day whose refusals nobody checked.
        recorder_errors = mlb_game_reads.validate_with_denominator(
            schedule_path, schedule, load_mlb_selection_policy()
        )
    if not candidates and not watchlist:
        # An explicit PASS: the slate was collected and produced nothing to
        # review. Journalled with the notices this cycle raised, so a quiet
        # cycle that still surfaced a zombie keeps that evidence on disk.
        if recorder_errors:
            # Reported ONCE per day, not every fifteen minutes: a warning that
            # repeats ninety-six times is the alarm-fatigue failure this lane
            # already paid for with the stuck watchlist entry. The journal
            # carries it on every cycle regardless — disk is the durable
            # record, stdout is the notification.
            notice = recorder_gap_notice(recorder_errors)
            if not _recorder_gap_already_reported(sport, day):
                print(f"{sport} review gate NOTICE: {notice}")
            notices = notices + [notice]
            journal_gate_run(
                # Not `no_work`. That word is what hid this on 2026-09-02: the
                # gap was caught on forty-one cycles and every one of them
                # journalled the outcome an honest empty card produces, so the
                # only surface carrying the difference was `stage` — which
                # nothing reads.
                sport, day, OUTCOME_RECORDER_FAILED, "recorder_missing",
                detail=(
                    "schedule present with no candidates and no due watchlist "
                    "entries, and its per-game record is invalid: "
                    + "; ".join(recorder_errors)
                ),
                schedule_path=schedule_path, notices=notices,
            )
            # Exit code deliberately unchanged. Nothing about review routing is
            # wrong on this cycle, and failing the gate closed here would take
            # the reviewer offline for a defect in a measurement artifact.
            return 0
        journal_gate_run(
            sport, day, OUTCOME_NO_WORK, "no_reviewable_work",
            detail="schedule present with no candidates and no due watchlist entries",
            schedule_path=schedule_path, notices=notices,
        )
        return 0
    if recorder_errors:
        # Same defect, the other branch: a day WITH review work can be just as
        # unrecorded, and the early return above would never see it. Reported
        # under the same once-per-day rule so the two paths cannot double up.
        notice = recorder_gap_notice(recorder_errors)
        if not _recorder_gap_already_reported(sport, day):
            print(f"{sport} review gate NOTICE: {notice}")
        notices.append(notice)
    if sport == "MLB":
        # Invalid entries whose first pitch already passed are dead as routing
        # inputs (due_entries quarantines them); surface them alongside real
        # review work instead of failing every remaining run of the day closed.
        # Printed only when the gate has work, so quiet cycles stay silent —
        # deliberately kept below the return: an invalid entry still fails the
        # gate loudly on any cycle with a routable sibling, so it does not have
        # the zombie's report-or-never property.
        for label, messages in sorted(stale_invalid_watchlist(schedule).items()):
            notice = (
                f"quarantined invalid historical watchlist entry {label} "
                f"(first pitch passed, never routable): {'; '.join(messages)}"
            )
            print(f"{sport} review gate NOTICE: {notice}")
            notices.append(notice)

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
    deferrals: list[dict[str, Any]] = []
    if watchlist:
        recheck_prompt, deferral_eligible_ids = build_lineup_recheck_prompt(
            schedule_path, watchlist, deferrals
        )
        prompts.append(recheck_prompt)
    counts = {
        "candidates": len(candidate_ids),
        "watchlist_due": len(watchlist_ids),
        "deferral_eligible": len(deferral_eligible_ids),
        "notices": len(notices),
    }
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
        journal_gate_run(
            sport, day, OUTCOME_ERROR, "child_timeout",
            detail="child reviewer timed out; reviewed state was not accepted",
            schedule_path=schedule_path, counts=counts,
            notices=notices, deferrals=deferrals,
        )
        return 1
    except OSError:
        print(
            f"{sport} review gate ERROR: child reviewer could not start; reviewed state was "
            "not accepted. Verify the Hermes CLI and retry the job."
        )
        journal_gate_run(
            sport, day, OUTCOME_ERROR, "child_start",
            detail="child reviewer could not start; reviewed state was not accepted",
            schedule_path=schedule_path, counts=counts,
            notices=notices, deferrals=deferrals,
        )
        return 1
    if proc.returncode:
        print(
            f"{sport} review gate ERROR: child reviewer exited {proc.returncode}; "
            "reviewed state was not accepted. Retry the job and inspect Vig session logs."
        )
        journal_gate_run(
            sport, day, OUTCOME_ERROR, "child_exit",
            detail=f"child reviewer exited {proc.returncode}; reviewed state was not accepted",
            schedule_path=schedule_path, counts=counts,
            notices=notices, deferrals=deferrals,
        )
        return proc.returncode

    def _journal_failure(stage: str, detail: str) -> None:
        journal_gate_run(
            sport, day, OUTCOME_ERROR, stage, detail=detail,
            schedule_path=schedule_path, counts=counts,
            notices=notices, deferrals=deferrals,
        )

    try:
        updated = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{sport} review gate ERROR: could not validate reviewed state: {exc}")
        _journal_failure("reviewed_state_parse", f"could not validate reviewed state: {exc}")
        return 1
    if not isinstance(updated, dict):
        print(f"{sport} review gate ERROR: reviewed schedule must remain an object")
        _journal_failure("reviewed_state_type", "reviewed schedule must remain an object")
        return 1
    # The reviewer's own output, snapshotted before any normalization touches
    # it. Diagnosing a refusal means reading what the CHILD wrote, not a
    # half-normalized copy of it, and normalize_review_routing legitimately
    # mutates `updated` before some of its refusals.
    reviewed_as_written = copy.deepcopy(updated)

    def _archive_refused(stage: str, detail: str) -> None:
        """Archive BEFORE the restore, because the restore is what destroys it."""
        archived = persist_refused_review(
            sport, day, reviewed_as_written, stage, detail
        )
        if archived is not None:
            print(f"{sport} review gate: refused review archived to {archived}")
        else:
            print(
                f"{sport} review gate: could not archive the refused review; the "
                "reviewed state is about to be replaced and will not be recoverable"
            )

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
        _archive_refused(
            "routing_normalization",
            f"routing normalization failed closed: {'; '.join(normalization_errors)}",
        )
        _restore_pre_review_state("normalization failure")
        _journal_failure(
            "routing_normalization",
            f"routing normalization failed closed: {'; '.join(normalization_errors)}",
        )
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
        _archive_refused(
            "review_transition",
            f"invalid review transition: {'; '.join(transition_errors)}",
        )
        _restore_pre_review_state("transition validation failure")
        _journal_failure(
            "review_transition",
            f"invalid review transition: {'; '.join(transition_errors)}",
        )
        return 1
    try:
        write_latest_action(sport, day, updated, mlb_standing_authorized)
        persist_schedule_locked(schedule_path, updated)
    except (OSError, ScheduleFormatError) as exc:
        print(f"{sport} review gate ERROR: could not persist reviewed state: {exc}")
        _journal_failure("persist", f"could not persist reviewed state: {exc}")
        return 1

    reviewed = parse_candidates(updated)
    counts = {
        **counts,
        "approved": sum(candidate.get("vig_approved") is True for candidate in reviewed),
        "rejected": sum(candidate.get("vig_approved") is False for candidate in reviewed),
        # Named for its POPULATION, because it is not the same one as
        # watchlist_due on this record: that counts THIS RUN's due set, this
        # counts every pending entry left on the whole watchlist afterwards.
        # Called "deferred" they invited subtraction, and two counts over one
        # source whose populations differ silently are indistinguishable from
        # a stale counter (Reviewer, PR #60).
        "watchlist_pending_after": sum(
            isinstance(entry, dict) and entry.get("status") == PENDING_STATUS
            for entry in updated.get("lineup_watchlist", [])
        ),
    }
    journal_gate_run(
        sport, day, OUTCOME_REVIEWED, "complete",
        detail="review persisted", schedule_path=schedule_path,
        counts=counts, notices=notices, deferrals=deferrals,
    )

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
