#!/usr/bin/env python3
"""Idempotency guard for MLB Polymarket execution schedules.

The execution poller is agent-driven, so this script provides the hard
file-level guard it must call before placing any live order.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


LOCK_STALE_SECONDS = 15 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _write_json_locked(file_obj: Any, data: dict[str, Any]) -> None:
    """Write JSON through the locked descriptor without replacing the inode.

    Replacing the file while another process is waiting on flock can let the
    waiter acquire a lock on the old unlinked inode and then clobber the newer
    path. When a flock is active, rewrite the same descriptor instead.
    """
    file_obj.seek(0)
    json.dump(data, file_obj, indent=2)
    file_obj.write("\n")
    file_obj.truncate()
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dec_value(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("value", "0")
    return Decimal(str(value or "0"))


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _merge_number(existing: Any, incoming: Any) -> Any:
    total = _dec_value(existing) + _dec_value(incoming)
    if total == total.to_integral_value():
        return int(total)
    return _money(total)


def _extract_fills(receipt: dict[str, Any], fallback_slug: str) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    response = receipt.get("response") or {}
    for execution in response.get("executions", []):
        if execution.get("type") != "EXECUTION_TYPE_FILL":
            continue
        order = execution.get("order") or {}
        action = order.get("action")
        intent = order.get("intent", "")
        if action == "ORDER_ACTION_SELL" or str(intent).startswith("ORDER_INTENT_SELL"):
            continue
        slug = order.get("marketSlug") or receipt.get("market_slug") or receipt.get("marketSlug")
        if slug != fallback_slug:
            continue
        fill_price = _dec_value(execution.get("lastPx") or order.get("price"))
        fill_qty = _dec_value(execution.get("lastShares") or order.get("cumQuantity"))
        commission = _dec_value(execution.get("commissionNotionalCollected"))
        fills.append(
            {
                "receipt_path": None,
                "order_id": order.get("id") or response.get("id"),
                "trade_id": execution.get("tradeId") or execution.get("id"),
                "fill_price": float(fill_price),
                "fill_quantity": int(fill_qty) if fill_qty == fill_qty.to_integral_value() else float(fill_qty),
                "fill_notional": _money(fill_price * fill_qty),
                "commission": _money(commission),
                "transact_time": execution.get("transactTime"),
            }
        )
    return fills


def find_filled_receipts(receipts_dir: Path | str, market_slug: str) -> list[dict[str, Any]]:
    """Return filled order details for a market slug from receipt files."""
    root = Path(receipts_dir)
    if not root.exists():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(root.glob(f"*order-{market_slug}.json")):
        try:
            receipt = _load_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        for fill in _extract_fills(receipt, market_slug):
            fill["receipt_path"] = str(path)
            found.append(fill)
    return found


def _candidate_for(schedule: dict[str, Any], market_slug: str) -> dict[str, Any] | None:
    for candidate in schedule.get("candidates", []):
        if candidate.get("polymarket_slug") == market_slug:
            return candidate
    return None


def _standing_authorized_candidate(
    schedule: dict[str, Any], candidate: dict[str, Any], now: datetime
) -> bool:
    first_pitch = _parse_utc_timestamp(candidate.get("first_pitch_utc"))
    schedule_date = schedule.get("date")
    slug = candidate.get("polymarket_slug")
    unit_size = candidate.get("unit_size")
    max_price = candidate.get("max_polymarket_price")
    minutes_to_pitch = (
        (first_pitch - now.astimezone(timezone.utc)).total_seconds() / 60
        if first_pitch
        else 0
    )
    return bool(
        first_pitch
        and 0 < minutes_to_pitch <= 120
        and schedule.get("sport") == "MLB"
        and schedule.get("market_type") == "moneyline"
        and candidate.get("sport") == "MLB"
        and candidate.get("market_type") == "moneyline"
        and candidate.get("vig_approved") is True
        and candidate.get("execution_mode") == "standing_authorized"
        and candidate.get("execution_status") == "pending"
        and candidate.get("executed") is False
        and not candidate.get("skipped")
        and not candidate.get("held")
        and isinstance(schedule_date, str)
        and isinstance(slug, str)
        and slug.startswith("aec-mlb-")
        and slug.endswith(f"-{schedule_date}")
        and isinstance(unit_size, (int, float))
        and not isinstance(unit_size, bool)
        and unit_size > 0
        and isinstance(max_price, (int, float))
        and not isinstance(max_price, bool)
        and 0 < max_price < 1
    )


RISK_LIMITS_PATH = Path("/home/clawdbot/.hermes/vig/state/risk_limits.json")
CANONICAL_PICKS_PATH = Path("/home/clawdbot/notes/Sports/picks/picks.json")


def _risk_limit_violation(
    candidate: dict[str, Any], picks_path: Path | str | None, now: datetime
) -> str | None:
    """Deterministic money rails: cap, unit and price ceilings from one JSON.

    Fails CLOSED — unreadable limits or ledger refuse the lock. These rails
    were previously prompt-enforced only (and contradictory across documents).
    """
    try:
        limits = json.loads(RISK_LIMITS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return f"risk limits unreadable ({exc})"
    unit_size = float(candidate.get("unit_size") or 0)
    max_unit = float(limits.get("max_unit_usd_absolute") or 0)
    if max_unit and unit_size > max_unit:
        return f"unit_size {unit_size} exceeds max_unit_usd_absolute {max_unit}"
    price_ceiling = limits.get("max_polymarket_price")
    max_price = candidate.get("max_polymarket_price")
    if price_ceiling is not None and isinstance(max_price, (int, float)) and max_price > float(price_ceiling):
        return f"max_polymarket_price {max_price} exceeds ceiling {price_ceiling}"
    daily_cap = float(limits.get("daily_cap_usd") or 0)
    if daily_cap:
        ledger = Path(picks_path) if picks_path else CANONICAL_PICKS_PATH
        try:
            picks = json.loads(ledger.read_text()).get("picks", [])
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            return f"canonical picks ledger unreadable for cap check ({exc})"
        today = now.astimezone(timezone.utc).date().isoformat()
        spent = 0.0
        for pick in picks:
            if not isinstance(pick, dict):
                continue
            stamp = str(pick.get("execution_timestamp") or pick.get("created_at") or "")
            if stamp[:10] == today and pick.get("status") != "void":
                spent += float(pick.get("entry_notional") or pick.get("unit_size") or 0)
        if spent + unit_size > daily_cap:
            return (
                f"daily cap breach: spent {spent:.2f} + unit {unit_size:.2f} "
                f"> cap {daily_cap:.2f}"
            )
    return None


def acquire_execution_lock(
    schedule_path: Path | str,
    market_slug: str,
    attempt_id: str,
    *,
    require_standing_authorized: bool = False,
    now: datetime | None = None,
    picks_path: Path | str | None = None,
) -> bool:
    """Set execution_lock for a candidate, refusing executed/skipped/locked rows."""
    path = Path(schedule_path)
    with path.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            schedule = json.load(f)
            candidate = _candidate_for(schedule, market_slug)
            if not candidate or candidate.get("executed") or candidate.get("skipped"):
                return False
            moment = now or datetime.now(timezone.utc)
            if require_standing_authorized and not _standing_authorized_candidate(
                schedule, candidate, moment
            ):
                return False
            violation = _risk_limit_violation(candidate, picks_path, moment)
            if violation:
                print(json.dumps({"ok": False, "risk_limit_violation": violation}, indent=2))
                return False
            lock = candidate.get("execution_lock")
            if lock and lock.get("attempt_id") != attempt_id:
                return False
            candidate["execution_lock"] = {"attempt_id": attempt_id, "locked_at": utc_now()}
            _write_json_locked(f, schedule)
            return True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def active_pick_exists(picks_path: Path | str, market_slug: str) -> bool:
    """Return true when picks.json already has an unsettled row for a market."""
    path = Path(picks_path)
    data = _load_json(path)
    picks = data.get("picks", []) if isinstance(data, dict) else data
    if not isinstance(picks, list):
        raise ValueError("canonical picks ledger must contain a picks list")
    final_results = {"win", "loss", "lost", "won", "void", "push", "cancelled", "canceled"}
    inactive_statuses = {"settled", "closed", "void", "cancelled", "canceled"}
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        slug = pick.get("market_slug") or pick.get("polymarket_slug")
        result = str(pick.get("result") or "").strip().casefold()
        status = str(pick.get("status") or "active").strip().casefold()
        active = result not in final_results and not pick.get("settled_at") and status not in inactive_statuses
        if slug == market_slug and active:
            return True
    return False


def append_pick_with_dedup(picks_path: Path | str, new_pick: dict[str, Any], window_seconds: int = 60) -> dict[str, Any]:
    """Append a pick row, or merge near-identical duplicate fills into an existing row.

    Duplicate key: same market_slug and execution_timestamp within +/- window_seconds.
    Merged fields: fill_shares and entry_notional are summed, duplicate_count increments,
    duplicate_batch is set, and duplicate_pick_ids captures the absorbed row identity.
    """
    path = Path(picks_path)
    with path.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            data = json.load(f)
            picks = data.setdefault("picks", [])
            new_slug = new_pick.get("market_slug")
            new_ts = _parse_utc_timestamp(new_pick.get("execution_timestamp"))
            if new_slug and new_ts:
                for existing in picks:
                    if existing.get("market_slug") != new_slug:
                        continue
                    existing_ts = _parse_utc_timestamp(existing.get("execution_timestamp"))
                    if not existing_ts:
                        continue
                    delta = abs((existing_ts - new_ts).total_seconds())
                    if delta <= window_seconds:
                        existing["fill_shares"] = _merge_number(existing.get("fill_shares"), new_pick.get("fill_shares"))
                        existing["entry_notional"] = _money(_dec_value(existing.get("entry_notional")) + _dec_value(new_pick.get("entry_notional")))
                        existing["duplicate_count"] = int(existing.get("duplicate_count", 1)) + 1
                        existing["duplicate_batch"] = True
                        absorbed = existing.setdefault("duplicate_pick_ids", [])
                        if new_pick.get("pick_id"):
                            absorbed.append(new_pick["pick_id"])
                        existing["last_duplicate_execution_timestamp"] = new_pick.get("execution_timestamp")
                        _write_json_locked(f, data)
                        return {"action": "merged", "pick": existing}
            picks.append(new_pick)
            _write_json_locked(f, data)
            return {"action": "appended", "pick": new_pick}
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def mark_execution_from_receipts(
    schedule_path: Path | str,
    market_slug: str,
    receipts_dir: Path | str,
    note: str = "existing filled receipt found before execution",
) -> bool:
    """Mark candidate executed using existing filled receipts, preventing retries."""
    fills = find_filled_receipts(receipts_dir, market_slug)
    if not fills:
        return False
    path = Path(schedule_path)
    with path.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            schedule = json.load(f)
            candidate = _candidate_for(schedule, market_slug)
            if not candidate:
                return False
            qty: Decimal = sum((Decimal(str(fill["fill_quantity"])) for fill in fills), Decimal("0"))
            notional: Decimal = sum((Decimal(str(fill["fill_notional"])) for fill in fills), Decimal("0"))
            commission: Decimal = sum((Decimal(str(fill["commission"])) for fill in fills), Decimal("0"))
            first = fills[0]
            last = fills[-1]
            avg_price = notional / qty if qty else Decimal("0")

            candidate["executed"] = True
            candidate["executed_at"] = last.get("transact_time") or utc_now()
            candidate["fill_price"] = float(avg_price.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
            candidate["fill_quantity"] = int(qty) if qty == qty.to_integral_value() else float(qty)
            candidate["fill_notional"] = _money(notional)
            candidate["commission"] = _money(commission)
            candidate["polymarket_order_id"] = last.get("order_id")
            candidate["polymarket_trade_id"] = last.get("trade_id")
            if len(fills) > 1:
                candidate["duplicate_fill_count"] = len(fills)
                candidate["duplicate_order_ids"] = [fill.get("order_id") for fill in fills]
                candidate["duplicate_trade_ids"] = [fill.get("trade_id") for fill in fills]
            candidate["execution_lock"] = None
            candidate["execution_note"] = note
            if len(fills) == 1:
                candidate.setdefault("execution_receipt", first.get("receipt_path"))
            else:
                candidate["execution_receipts"] = [fill.get("receipt_path") for fill in fills]
            _write_json_locked(f, schedule)
            return True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def clear_execution_lock(schedule_path: Path | str, market_slug: str, attempt_id: str | None = None) -> bool:
    path = Path(schedule_path)
    with path.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            schedule = json.load(f)
            candidate = _candidate_for(schedule, market_slug)
            if not candidate or not candidate.get("execution_lock"):
                return False
            if attempt_id and candidate["execution_lock"].get("attempt_id") != attempt_id:
                return False
            candidate["execution_lock"] = None
            _write_json_locked(f, schedule)
            return True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def main() -> int:
    # Profile gate: only execute under vig profile (or default/unset for backward compat)
    profile = os.environ.get("HERMES_PROFILE", "")
    if profile and profile != "vig":
        print(json.dumps({"ok": False, "error": f"Profile gate rejected: HERMES_PROFILE={profile}, only vig may execute live orders"}, indent=2))
        return 10

    ap = argparse.ArgumentParser(description="Guard MLB Polymarket execution against duplicate fills")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--schedule", required=True)
    common.add_argument("--market-slug", required=True)
    common.add_argument("--receipts-dir", default=".picks/receipts/polymarket")

    check = sub.add_parser("check", parents=[common])
    check.add_argument("--mark", action="store_true", help="mark schedule executed if fills exist")
    check.add_argument(
        "--picks-file",
        default=str(CANONICAL_PICKS_PATH),
        help="canonical picks ledger; active slug blocks execution",
    )

    lock = sub.add_parser("lock", parents=[common])
    lock.add_argument("--attempt-id", required=True)
    lock.add_argument("--require-standing-authorized", action="store_true")
    lock.add_argument("--now", help="UTC/offset timestamp override for deterministic checks")
    lock.add_argument(
        "--picks-file",
        default=str(CANONICAL_PICKS_PATH),
        help="canonical picks ledger for the deterministic daily-cap check",
    )

    clear = sub.add_parser("clear", parents=[common])
    clear.add_argument("--attempt-id")

    args = ap.parse_args()
    if args.cmd == "check":
        fills = find_filled_receipts(args.receipts_dir, args.market_slug)
        try:
            active_pick = bool(args.picks_file) and active_pick_exists(
                args.picks_file, args.market_slug
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": f"could not read canonical picks: {exc}"}, indent=2))
            return 4
        if fills and args.mark:
            mark_execution_from_receipts(args.schedule, args.market_slug, args.receipts_dir)
        print(json.dumps({"ok": True, "has_filled_receipt": bool(fills), "has_active_pick": active_pick, "fills": fills}, indent=2))
        return 2 if fills or active_pick else 0
    if args.cmd == "lock":
        lock_now = _parse_utc_timestamp(args.now) if args.now else None
        if args.now and lock_now is None:
            print(json.dumps({"ok": False, "error": "--now must be a valid timestamp"}, indent=2))
            return 4
        ok = acquire_execution_lock(
            args.schedule,
            args.market_slug,
            args.attempt_id,
            require_standing_authorized=args.require_standing_authorized,
            now=lock_now,
            picks_path=args.picks_file,
        )
        print(json.dumps({"ok": ok, "locked": ok}, indent=2))
        return 0 if ok else 3
    if args.cmd == "clear":
        ok = clear_execution_lock(args.schedule, args.market_slug, args.attempt_id)
        print(json.dumps({"ok": ok, "cleared": ok}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
