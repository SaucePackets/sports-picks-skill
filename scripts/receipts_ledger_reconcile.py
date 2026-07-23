#!/usr/bin/env python3
"""Cross-check Polymarket fill receipts against the canonical picks ledger.

Every filled BUY receipt must have a picks.json row carrying its trade id.
Off-ledger fills are how the June 23-27 bets went missing; this makes that
class of gap deterministic to detect. Prints one line per discrepancy and
exits 1 when any exist (0 when clean).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from execution_guard import _extract_fills, _load_json  # noqa: E402

DEFAULT_RECEIPTS = Path("/home/clawdbot/projects/sports-picks-skill/.picks/receipts/polymarket")
DEFAULT_PICKS = Path("/home/clawdbot/notes/Sports/picks/picks.json")
# picks.json era began 2026-06-23; earlier receipts belong to the retired
# "vault" record and are intentionally out of scope.
DEFAULT_EPOCH = "2026-06-23T00:00:00Z"


def receipt_fills(receipts_dir: Path) -> list[dict]:
    fills = []
    for path in sorted(receipts_dir.glob("*order-*.json")):
        try:
            receipt = _load_json(path)
        except (OSError, json.JSONDecodeError):
            print(f"WARNING: unreadable receipt {path.name}")
            continue
        slug = receipt.get("market_slug") or receipt.get("marketSlug") or ""
        if not slug:
            name = path.name
            marker = "order-"
            slug = name[name.index(marker) + len(marker):].rsplit(".json", 1)[0] if marker in name else ""
        for fill in _extract_fills(receipt, slug):
            fill["receipt_path"] = str(path)
            fill["market_slug"] = slug
            fills.append(fill)
    return fills


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--receipts-dir", type=Path, default=DEFAULT_RECEIPTS)
    ap.add_argument("--picks-file", type=Path, default=DEFAULT_PICKS)
    ap.add_argument("--since", default=DEFAULT_EPOCH,
                    help="ignore receipts filled before this UTC instant (ledger epoch)")
    args = ap.parse_args()

    try:
        picks = json.loads(args.picks_file.read_text()).get("picks", [])
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: picks ledger unreadable: {exc}")
        return 1
    ledger_trades = {str(p.get("polymarket_trade_id")) for p in picks if p.get("polymarket_trade_id")}
    ledger_orders = {str(p.get("polymarket_order_id")) for p in picks if p.get("polymarket_order_id")}

    problems = 0
    if not args.receipts_dir.is_dir():
        print(f"NOTE: receipts dir missing: {args.receipts_dir}")
        return 0
    for fill in receipt_fills(args.receipts_dir):
        tid = str(fill.get("trade_id") or "")
        oid = str(fill.get("order_id") or "")
        stamp = str(fill.get("transact_time") or "")
        if not stamp:
            # Some receipts lack transactTime; the filename leads with the
            # fill's UTC stamp (YYYYMMDD-HHMMSS-...).
            name = Path(fill["receipt_path"]).name
            raw = name.split("-sdk-order-")[0].split("-direct-order-")[0]
            digits = raw.replace("-", "")
            if len(digits) >= 14 and digits[:14].isdigit():
                stamp = (
                    f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
                    f"T{digits[8:10]}:{digits[10:12]}:{digits[12:14]}Z"
                )
        if stamp and stamp < args.since:
            continue
        if tid in ledger_trades or oid in ledger_orders:
            continue
        problems += 1
        print(
            "OFF-LEDGER FILL: "
            f"slug={fill['market_slug']} trade={tid or '?'} order={oid or '?'} "
            f"notional=${fill.get('fill_notional')} @ {fill.get('fill_price')} "
            f"({fill.get('transact_time')}) receipt={Path(fill['receipt_path']).name}"
        )
    if problems:
        print(f"RECONCILE FAILED: {problems} filled receipt(s) missing from the picks ledger")
        return 1
    print("RECONCILE OK: every filled receipt has a ledger row")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
