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


def acquire_execution_lock(schedule_path: Path | str, market_slug: str, attempt_id: str) -> bool:
    """Set execution_lock for a candidate, refusing executed/skipped/locked rows."""
    path = Path(schedule_path)
    with path.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            schedule = json.load(f)
            candidate = _candidate_for(schedule, market_slug)
            if not candidate or candidate.get("executed") or candidate.get("skipped"):
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
    for pick in data.get("picks", []):
        if pick.get("market_slug") == market_slug and pick.get("status") != "settled":
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

    lock = sub.add_parser("lock", parents=[common])
    lock.add_argument("--attempt-id", required=True)

    clear = sub.add_parser("clear", parents=[common])
    clear.add_argument("--attempt-id")

    args = ap.parse_args()
    if args.cmd == "check":
        fills = find_filled_receipts(args.receipts_dir, args.market_slug)
        if fills and args.mark:
            mark_execution_from_receipts(args.schedule, args.market_slug, args.receipts_dir)
        print(json.dumps({"ok": True, "has_filled_receipt": bool(fills), "fills": fills}, indent=2))
        return 2 if fills else 0
    if args.cmd == "lock":
        ok = acquire_execution_lock(args.schedule, args.market_slug, args.attempt_id)
        print(json.dumps({"ok": ok, "locked": ok}, indent=2))
        return 0 if ok else 3
    if args.cmd == "clear":
        ok = clear_execution_lock(args.schedule, args.market_slug, args.attempt_id)
        print(json.dumps({"ok": ok, "cleared": ok}, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
