#!/usr/bin/env python3
"""Guarded Polymarket US execution helper for sports-picks.

Dry-run by default. Live orders require:
- POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in env
- --execute
- --approval-token matching the deterministic proposal token
- --i-accept-live-trading
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

API_BASE = "https://api.polymarket.us"
GATEWAY_BASE = "https://gateway.polymarket.us"
RECEIPT_ROOT = Path(".picks/receipts/polymarket")

ORDER_TYPES = {"ORDER_TYPE_LIMIT", "ORDER_TYPE_MARKET"}
ORDER_ACTIONS = {"ORDER_ACTION_BUY", "ORDER_ACTION_SELL"}
OUTCOME_SIDES = {"OUTCOME_SIDE_YES", "OUTCOME_SIDE_NO"}
TIFS = {
    "TIME_IN_FORCE_DAY",
    "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    "TIME_IN_FORCE_GOOD_TILL_DATE",
    "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "TIME_IN_FORCE_FILL_OR_KILL",
}


def die(message: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": message}, indent=2), file=sys.stderr)
    raise SystemExit(code)


def dec(value: str | int | float | None, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        die(f"invalid decimal for {name}: {value!r}")


def http_json(method: str, base: str, path: str, body: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
    data = None
    headers = {"User-Agent": "sports-picks-polymarket-guard/1.0"}
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    if auth:
        headers.update(auth_headers(method, path))
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        raise RuntimeError(json.dumps({"status": e.code, "path": path, "response": payload}, indent=2)) from e
    except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
        raise RuntimeError(json.dumps({"status": "network_error", "path": path, "error": str(e)}, indent=2)) from e


def auth_headers(method: str, path: str) -> dict[str, str]:
    key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if not key_id or not secret:
        die("missing POLYMARKET_KEY_ID or POLYMARKET_SECRET_KEY")
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except Exception as e:  # pragma: no cover - depends on local env
        die("missing dependency: pip install cryptography")
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path}"
    try:
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)[:32])
    except Exception as e:
        die(f"invalid POLYMARKET_SECRET_KEY format: {e}")
    signature = base64.b64encode(private_key.sign(message.encode())).decode()
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": signature,
    }


def slug_safe(slug: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", slug)[:80]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def save_receipt(action: str, slug: str, payload: dict[str, Any]) -> str:
    RECEIPT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = RECEIPT_ROOT / f"{stamp}-{action}-{slug_safe(slug)}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return str(path)


def canonical_token_payload(proposal: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "marketSlug": proposal["request"]["marketSlug"],
        "outcomeSide": proposal["request"].get("outcomeSide"),
        "action": proposal["request"].get("action"),
        "type": proposal["request"].get("type"),
        "price": proposal["request"].get("price"),
        "quantity": proposal["request"].get("quantity"),
        "cashOrderQty": proposal["request"].get("cashOrderQty"),
        "tif": proposal["request"].get("tif"),
        "max_notional": proposal.get("max_notional"),
        "estimated_notional": proposal.get("estimated_notional"),
    }
    return {k: v for k, v in keep.items() if v is not None}


def approval_token(proposal: dict[str, Any]) -> str:
    payload = json.dumps(canonical_token_payload(proposal), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def market_snapshots(market_slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    quoted = urllib.parse.quote(market_slug, safe="")
    market = http_json("GET", GATEWAY_BASE, f"/v1/market/slug/{quoted}")
    bbo = http_json("GET", GATEWAY_BASE, f"/v1/markets/{quoted}/bbo")
    return market, bbo


def market_is_open(market: dict[str, Any], bbo: dict[str, Any]) -> tuple[bool, str]:
    m = market.get("market", market)
    md = bbo.get("marketData", {})
    if m.get("closed") is True:
        return False, "market closed"
    if m.get("active") is False:
        return False, "market inactive"
    state = md.get("state")
    if state and state != "MARKET_STATE_OPEN":
        return False, f"market state is {state}"
    return True, "open"


def build_request(args: argparse.Namespace) -> dict[str, Any]:
    if args.order_type not in ORDER_TYPES:
        die(f"bad order type: {args.order_type}")
    if args.action not in ORDER_ACTIONS:
        die(f"bad action: {args.action}")
    if args.outcome_side not in OUTCOME_SIDES:
        die(f"bad outcome side: {args.outcome_side}")
    if args.tif not in TIFS:
        die(f"bad tif: {args.tif}")

    body: dict[str, Any] = {
        "marketSlug": args.market_slug,
        "outcomeSide": args.outcome_side,
        "action": args.action,
        "type": args.order_type,
        "tif": args.tif,
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }

    if args.order_type == "ORDER_TYPE_LIMIT":
        if args.price is None or args.quantity is None:
            die("limit orders require --price and --quantity")
        price = dec(args.price, "price")
        quantity = dec(args.quantity, "quantity")
        if price is None or quantity is None or price <= 0 or quantity <= 0:
            die("price and quantity must be positive")
        if price > Decimal("1"):
            die("limit price must be <= 1.00")
        body["price"] = {"value": str(price), "currency": "USD"}
        body["quantity"] = float(quantity)
    else:
        if args.cash_order_qty is None:
            die("market orders require --cash-order-qty")
        cash = dec(args.cash_order_qty, "cash_order_qty")
        if cash is None or cash <= 0:
            die("cash order quantity must be positive")
        body["cashOrderQty"] = {"value": str(cash), "currency": "USD"}
        if args.slippage_bips is not None:
            body["slippageTolerance"] = {"bips": int(args.slippage_bips)}

    return body


def exposure_estimate(body: dict[str, Any]) -> Decimal | None:
    if body["type"] == "ORDER_TYPE_LIMIT" and body["action"] == "ORDER_ACTION_BUY":
        return dec(body["price"]["value"], "price") * dec(body["quantity"], "quantity")
    if body["type"] == "ORDER_TYPE_MARKET" and body["action"] == "ORDER_ACTION_BUY":
        return dec(body["cashOrderQty"]["value"], "cashOrderQty")
    return None


def amount_value(obj: Any) -> Decimal | None:
    if isinstance(obj, dict) and "value" in obj:
        return dec(obj.get("value"), "amount")
    if obj is None:
        return None
    return dec(obj, "amount")


def market_prices_for_side(bbo: dict[str, Any], outcome_side: str) -> dict[str, str | None]:
    md = bbo.get("marketData", bbo)
    bid = amount_value(md.get("bestBid"))
    ask = amount_value(md.get("bestAsk"))
    current = amount_value(md.get("currentPx"))
    one = Decimal("1")
    if outcome_side == "OUTCOME_SIDE_NO":
        # Polymarket US BBO is quoted for the long/YES side. NO is approximated
        # as the complement: sell NO ~= 1 - YES ask, buy NO ~= 1 - YES bid.
        yes_bid, yes_ask = bid, ask
        bid = (one - yes_ask) if yes_ask is not None else None
        ask = (one - yes_bid) if yes_bid is not None else None
        current = (one - current) if current is not None else None
    return {
        "exit_bid": str(bid) if bid is not None else None,
        "entry_ask": str(ask) if ask is not None else None,
        "current": str(current) if current is not None else None,
    }


def make_proposal(args: argparse.Namespace) -> dict[str, Any]:
    market, bbo = market_snapshots(args.market_slug)
    ok, state_reason = market_is_open(market, bbo)
    if not ok:
        die(state_reason)
    body = build_request(args)
    est = exposure_estimate(body)
    max_notional = dec(args.max_notional, "max_notional")
    if body["action"] == "ORDER_ACTION_BUY" and max_notional is None:
        die("buy orders require --max-notional")
    if est is not None and max_notional is not None and est > max_notional:
        die(f"estimated notional {est} exceeds max notional {max_notional}")
    proposal = {
        "ok": True,
        "mode": "dry_run",
        "created_at": utc_now(),
        "request": body,
        "estimated_notional": str(est) if est is not None else None,
        "max_notional": str(max_notional) if max_notional is not None else None,
        "market_snapshot": market,
        "bbo_snapshot": bbo,
        "notes": args.notes,
    }
    proposal["approval_token"] = approval_token(proposal)
    if args.write_receipt:
        proposal["receipt_path"] = save_receipt("proposal", args.market_slug, proposal)
    return proposal


def cmd_health(args: argparse.Namespace) -> dict[str, Any]:
    deps = {"cryptography": False}
    try:
        import cryptography  # noqa: F401
        deps["cryptography"] = True
    except Exception:
        pass
    return {
        "ok": True,
        "gateway_base": GATEWAY_BASE,
        "api_base": API_BASE,
        "env": {
            "POLYMARKET_KEY_ID": bool(os.environ.get("POLYMARKET_KEY_ID")),
            "POLYMARKET_SECRET_KEY": bool(os.environ.get("POLYMARKET_SECRET_KEY")),
        },
        "deps": deps,
    }


def cmd_market(args: argparse.Namespace) -> dict[str, Any]:
    market, bbo = market_snapshots(args.market_slug)
    return {"ok": True, "market": market, "bbo": bbo}


def cmd_balances(args: argparse.Namespace) -> dict[str, Any]:
    data = http_json("GET", API_BASE, "/v1/account/balances", auth=True)
    return {"ok": True, "balances": data}


def cmd_positions(args: argparse.Namespace) -> dict[str, Any]:
    try:
        data = http_json("GET", API_BASE, "/v1/portfolio/positions", auth=True)
        return {"ok": True, "positions": data}
    except RuntimeError as e:
        return {"ok": False, "positions": None, "error": str(e)}


def cmd_open_orders(args: argparse.Namespace) -> dict[str, Any]:
    data = http_json("GET", API_BASE, "/v1/orders/open", auth=True)
    return {"ok": True, "open_orders": data}


def cmd_preview(args: argparse.Namespace) -> dict[str, Any]:
    proposal = make_proposal(args)
    preview = http_json("POST", API_BASE, "/v1/order/preview", body=proposal["request"], auth=True)
    receipt = {**proposal, "mode": "preview", "preview": preview, "ok": True}
    receipt["receipt_path"] = save_receipt("preview", args.market_slug, receipt)
    return receipt


def cmd_watch_once(args: argparse.Namespace) -> dict[str, Any]:
    market, bbo = market_snapshots(args.market_slug)
    prices = market_prices_for_side(bbo, args.outcome_side)
    entry = dec(args.entry_price, "entry_price")
    quantity = dec(args.quantity, "quantity")
    profit_cents = dec(args.profit_cents, "profit_cents")
    loss_cents = dec(args.loss_cents, "loss_cents")
    if entry is None or quantity is None or profit_cents is None or loss_cents is None:
        die("entry, quantity, profit-cents, and loss-cents are required")
    exit_bid = dec(prices.get("exit_bid"), "exit_bid")
    if exit_bid is None:
        status = "no_bid"
        unrealized = None
        move = None
        alert = "No usable exit bid. Keep watching."
    else:
        move = exit_bid - entry
        unrealized = move * quantity
        if move >= profit_cents:
            status = "profit_exit_candidate"
            alert = "Profit threshold reached; propose a sell/exit, do not auto-sell."
        elif move <= -loss_cents:
            status = "adverse_move"
            alert = "Loss threshold reached; review the position and game context."
        else:
            status = "hold"
            alert = "No action threshold hit."
    result = {
        "ok": True,
        "mode": "watch_once",
        "checked_at": utc_now(),
        "market_slug": args.market_slug,
        "outcome_side": args.outcome_side,
        "entry_price": str(entry),
        "quantity": str(quantity),
        "prices": prices,
        "move_per_share": str(move) if move is not None else None,
        "unrealized_before_fees": str(unrealized) if unrealized is not None else None,
        "status": status,
        "alert": alert,
        "market_snapshot": market,
        "bbo_snapshot": bbo,
    }
    if args.write_receipt:
        result["receipt_path"] = save_receipt("watch", args.market_slug, result)
    return result


def cmd_propose(args: argparse.Namespace) -> dict[str, Any]:
    return make_proposal(args)


def cmd_order(args: argparse.Namespace) -> dict[str, Any]:
    proposal = make_proposal(args)
    if not args.execute:
        proposal["warning"] = "dry run only; add --execute --approval-token <token> --i-accept-live-trading for live order"
        return proposal
    if not args.i_accept_live_trading:
        die("missing --i-accept-live-trading")
    if not args.approval_token:
        die("missing --approval-token")
    if args.approval_token != proposal["approval_token"]:
        die(f"approval token mismatch; expected {proposal['approval_token']}")
    try:
        response = http_json("POST", API_BASE, "/v1/orders", body=proposal["request"], auth=True)
        receipt = {
            **proposal,
            "mode": "live",
            "executed_at": utc_now(),
            "response": response,
            "ok": True,
        }
        receipt["receipt_path"] = save_receipt("order", args.market_slug, receipt)
        return receipt
    except Exception as e:
        receipt = {
            **proposal,
            "mode": "live_error",
            "executed_at": utc_now(),
            "error": str(e),
            "ok": False,
        }
        receipt["receipt_path"] = save_receipt("order-error", args.market_slug, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        raise SystemExit(1)


def add_order_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--market-slug", required=True)
    p.add_argument("--outcome-side", required=True, choices=sorted(OUTCOME_SIDES))
    p.add_argument("--action", required=True, choices=sorted(ORDER_ACTIONS))
    p.add_argument("--order-type", default="ORDER_TYPE_LIMIT", choices=sorted(ORDER_TYPES))
    p.add_argument("--price")
    p.add_argument("--quantity")
    p.add_argument("--cash-order-qty")
    p.add_argument("--tif", default="TIME_IN_FORCE_DAY", choices=sorted(TIFS))
    p.add_argument("--max-notional", required=True)
    p.add_argument("--slippage-bips", type=int)
    p.add_argument("--notes", default="")
    p.add_argument("--write-receipt", dest="write_receipt", action="store_true", default=True)
    p.add_argument("--no-write-receipt", dest="write_receipt", action="store_false")


def add_watch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--market-slug", required=True)
    p.add_argument("--outcome-side", required=True, choices=sorted(OUTCOME_SIDES))
    p.add_argument("--entry-price", required=True)
    p.add_argument("--quantity", required=True)
    p.add_argument("--profit-cents", default="0.08")
    p.add_argument("--loss-cents", default="0.10")
    p.add_argument("--write-receipt", dest="write_receipt", action="store_true", default=True)
    p.add_argument("--no-write-receipt", dest="write_receipt", action="store_false")


def main() -> None:
    parser = argparse.ArgumentParser(description="Guarded Polymarket US helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    health = sub.add_parser("health")
    health.set_defaults(func=cmd_health)

    market = sub.add_parser("market")
    market.add_argument("--market-slug", required=True)
    market.set_defaults(func=cmd_market)

    balances = sub.add_parser("balances")
    balances.set_defaults(func=cmd_balances)

    positions = sub.add_parser("positions")
    positions.set_defaults(func=cmd_positions)

    open_orders = sub.add_parser("open-orders")
    open_orders.set_defaults(func=cmd_open_orders)

    preview = sub.add_parser("preview")
    add_order_args(preview)
    preview.set_defaults(func=cmd_preview)

    watch_once = sub.add_parser("watch-once")
    add_watch_args(watch_once)
    watch_once.set_defaults(func=cmd_watch_once)

    propose = sub.add_parser("propose")
    add_order_args(propose)
    propose.set_defaults(func=cmd_propose)

    order = sub.add_parser("order")
    add_order_args(order)
    order.add_argument("--execute", action="store_true")
    order.add_argument("--approval-token")
    order.add_argument("--i-accept-live-trading", action="store_true")
    order.set_defaults(func=cmd_order)

    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
