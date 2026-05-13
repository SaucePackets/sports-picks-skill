#!/usr/bin/env python3
"""SDK-first Polymarket US sports moneyline executor.

Dry-run by default. Live orders require:
- Polymarket US API credentials in env or ~/.hermes/.env
- authenticated preview with expected outcome match
- approval token from the proposal
- --execute and --i-accept-live-trading

This helper exists because Polymarket US sports slugs/outcome mapping can differ
from public .com URLs. Trust SDK preview metadata, not slug or YES/NO guesses.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

RECEIPT_ROOT = Path(".picks/receipts/polymarket")
WATCH_ROOT = Path(".picks/watchlist/polymarket")
HERMES_ENV = Path.home() / ".hermes/.env"

INTENTS = {
    "ORDER_INTENT_BUY_LONG",
    "ORDER_INTENT_SELL_LONG",
    "ORDER_INTENT_BUY_SHORT",
    "ORDER_INTENT_SELL_SHORT",
}
ORDER_TYPES = {"ORDER_TYPE_LIMIT", "ORDER_TYPE_MARKET"}
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


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug_safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value)[:100] or "polymarket"


def dec(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, dict) and "value" in value:
        value = value.get("value")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        die(f"invalid decimal for {name}: {value!r}")


def load_env_file(path: Path = HERMES_ENV) -> None:
    """Load simple KEY=VALUE lines without adding python-dotenv as a dependency."""
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def sdk_client(require_auth: bool):
    try:
        from polymarket_us import PolymarketUS
    except Exception:
        die("missing dependency: python -m pip install polymarket-us")
    load_env_file()
    key_id = os.environ.get("POLYMARKET_KEY_ID")
    secret_key = os.environ.get("POLYMARKET_SECRET_KEY")
    if require_auth and (not key_id or not secret_key):
        die("missing POLYMARKET_KEY_ID or POLYMARKET_SECRET_KEY")
    kwargs: dict[str, str] = {}
    if key_id:
        kwargs["key_id"] = key_id
    if secret_key:
        kwargs["secret_key"] = secret_key
    return PolymarketUS(**kwargs)


def as_jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, dict):
            return {str(k): as_jsonable(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [as_jsonable(v) for v in obj]
        return repr(obj)


def save_receipt(action: str, slug: str, payload: dict[str, Any]) -> str:
    RECEIPT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = RECEIPT_ROOT / f"{stamp}-{action}-{slug_safe(slug)}.json"
    path.write_text(json.dumps(as_jsonable(payload), indent=2, sort_keys=True) + "\n")
    return str(path)


def save_watchlist(payload: dict[str, Any]) -> str:
    WATCH_ROOT.mkdir(parents=True, exist_ok=True)
    slug = payload.get("market_slug") or payload.get("marketSlug") or "polymarket"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = WATCH_ROOT / f"{stamp}-{slug_safe(str(slug))}.json"
    path.write_text(json.dumps(as_jsonable(payload), indent=2, sort_keys=True) + "\n")
    return str(path)


def amount(value: Decimal | str | int | float) -> dict[str, str]:
    return {"value": str(dec(value, "amount")), "currency": "USD"}


def market_active(market: dict[str, Any]) -> tuple[bool, str]:
    data = market.get("market", market)
    if data.get("closed") is True:
        return False, "market closed"
    if data.get("active") is False:
        return False, "market inactive"
    state = data.get("state")
    if state and state not in {"MARKET_STATE_OPEN", "open"}:
        return False, f"market state is {state}"
    return True, "open"


def moneyline_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for market in event.get("markets", []) or []:
        sport_type = str(market.get("sportsMarketType") or market.get("sports_market_type") or "").lower()
        slug = str(market.get("slug") or "")
        title = str(market.get("title") or market.get("question") or "")
        if sport_type == "moneyline" or sport_type == "money_line" or slug.startswith("aec-") or "moneyline" in title.lower():
            out.append(market)
    return out


def extract_preview_outcome(preview: dict[str, Any]) -> str | None:
    order = preview.get("order", {}) if isinstance(preview, dict) else {}
    metadata = order.get("marketMetadata", {}) if isinstance(order, dict) else {}
    outcome = metadata.get("outcome")
    return str(outcome) if outcome is not None else None


def extract_order_price(preview_or_order: dict[str, Any]) -> Decimal | None:
    order = preview_or_order.get("order", preview_or_order) if isinstance(preview_or_order, dict) else {}
    for key in ("avgPx", "price"):
        price = dec(order.get(key), key)
        if price is not None and price > 0:
            return price
    return None


def outcome_price_from_orderbook(price: Decimal, intent: str) -> Decimal:
    """Convert Polymarket's long-side orderbook price into the selected outcome's price."""
    if "BUY_SHORT" in intent or "SELL_SHORT" in intent:
        return Decimal("1") - price
    return price


def orderbook_price_from_outcome(price: Decimal, intent: str) -> Decimal:
    """Convert a user-facing outcome price into the SDK orderbook price."""
    if "BUY_SHORT" in intent or "SELL_SHORT" in intent:
        return Decimal("1") - price
    return price


def extract_fill_price(response: dict[str, Any], intent: str = "") -> Decimal | None:
    """Find the actual non-zero fill price in selected-outcome terms."""
    for execution in response.get("executions", []) if isinstance(response, dict) else []:
        fill_price = dec(execution.get("lastPx"), "lastPx")
        fill_shares = dec(execution.get("lastShares"), "lastShares")
        if fill_price is not None and fill_price > 0 and fill_shares is not None and fill_shares > 0:
            return outcome_price_from_orderbook(fill_price, intent)
        order_price = extract_order_price(execution.get("order", {}))
        if order_price is not None and fill_shares is not None and fill_shares > 0:
            return outcome_price_from_orderbook(order_price, intent)
    return None


def extract_filled_quantity(response: dict[str, Any]) -> Decimal:
    """Return filled shares from execution reports; zero means no position exists."""
    filled = Decimal("0")
    for execution in response.get("executions", []) if isinstance(response, dict) else []:
        shares = dec(execution.get("lastShares"), "lastShares")
        if shares is not None and shares > 0:
            filled += shares
        order = execution.get("order", {})
        cum = dec(order.get("cumQuantity"), "cumQuantity")
        if cum is not None and cum > filled:
            filled = cum
    return filled


def side_cost(price: Decimal, quantity: Decimal, intent: str) -> Decimal:
    if "BUY_SHORT" in intent:
        return (Decimal("1") - price) * quantity
    return price * quantity


def build_order_request(args: argparse.Namespace) -> dict[str, Any]:
    if args.intent not in INTENTS:
        die(f"bad intent: {args.intent}")
    if args.order_type not in ORDER_TYPES:
        die(f"bad order type: {args.order_type}")
    if args.tif not in TIFS:
        die(f"bad tif: {args.tif}")

    request: dict[str, Any] = {
        "marketSlug": args.market_slug,
        "intent": args.intent,
        "type": args.order_type,
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        "synchronousExecution": True,
    }
    if args.order_type == "ORDER_TYPE_LIMIT":
        if args.price is None or args.quantity is None:
            die("limit orders require --price and --quantity")
        outcome_price = dec(args.price, "price")
        quantity = dec(args.quantity, "quantity")
        if outcome_price is None or quantity is None or outcome_price <= 0 or outcome_price >= 1 or quantity <= 0:
            die("limit --price must be between 0 and 1 and --quantity must be positive")
        orderbook_price = orderbook_price_from_outcome(outcome_price, args.intent)
        request.update({"price": amount(orderbook_price), "quantity": int(quantity), "tif": args.tif})
    else:
        # Polymarket US SDK currently previews sports moneyline "market" bodies as
        # malformed limits. Compile intent-to-enter-now into an IOC limit with a
        # cash cap-derived share quantity. This preserves price discipline and
        # avoids uncapped slippage while still behaving like a taker entry.
        # `--price` is always the selected outcome's acceptable price. For
        # BUY_SHORT outcomes, the SDK orderbook uses the inverse long-side price.
        cash = dec(args.cash_order_qty, "cash_order_qty")
        outcome_price = dec(args.price or args.current_price, "price/current_price")
        if cash is None or cash <= 0:
            die("market-style entries require positive --cash-order-qty")
        if outcome_price is None or outcome_price <= 0 or outcome_price >= 1:
            die("market-style entries require --price or --current-price between 0 and 1")
        unit_cost = outcome_price if "BUY" in args.intent else Decimal("0")
        quantity = int(cash / unit_cost)
        if quantity <= 0:
            die(f"cash order quantity {cash} is too small for price {outcome_price}")
        orderbook_price = orderbook_price_from_outcome(outcome_price, args.intent)
        request["requestedOrderType"] = "ORDER_TYPE_MARKET"
        request["type"] = "ORDER_TYPE_LIMIT"
        request["price"] = amount(orderbook_price)
        request["quantity"] = quantity
        request["tif"] = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
        request["cashCap"] = amount(cash)
    return request


def estimated_notional(request: dict[str, Any], intent: str) -> Decimal | None:
    if request.get("cashOrderQty"):
        return dec(request["cashOrderQty"], "cashOrderQty")
    if request.get("cashCap"):
        return dec(request["cashCap"], "cashCap")
    price = dec(request.get("price"), "price")
    quantity = dec(request.get("quantity"), "quantity")
    if price is None or quantity is None:
        return None
    return side_cost(price, quantity, intent) if "BUY" in intent else None


def canonical_token_payload(proposal: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "request",
        "expected_outcome",
        "preview_outcome",
        "estimated_notional",
        "max_notional",
        "max_price",
        "notes",
    ]
    return {key: proposal.get(key) for key in keys if proposal.get(key) is not None}


def approval_token(proposal: dict[str, Any]) -> str:
    payload = json.dumps(canonical_token_payload(proposal), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_proposal(args: argparse.Namespace) -> dict[str, Any]:
    max_notional = dec(args.max_notional, "max_notional")
    if max_notional is None or max_notional <= 0:
        die("--max-notional is required and must be positive")
    request = build_order_request(args)
    est = estimated_notional(request, args.intent)
    if est is not None and est > max_notional:
        die(f"estimated notional {est} exceeds max notional {max_notional}")

    client = sdk_client(require_auth=True)
    try:
        market = client.markets.retrieve_by_slug(args.market_slug)
        bbo = client.markets.bbo(args.market_slug)
        ok, reason = market_active(market.get("market", market) if isinstance(market, dict) else {})
        if not ok:
            die(reason)
        preview = client.orders.preview({"request": request})
    finally:
        client.close()

    preview_outcome = extract_preview_outcome(preview)
    if not preview_outcome:
        die("preview did not include order.marketMetadata.outcome; refusing to continue")
    if args.expected_outcome and preview_outcome.strip().lower() != args.expected_outcome.strip().lower():
        die(f"preview outcome mismatch: expected {args.expected_outcome!r}, got {preview_outcome!r}")

    preview_price = extract_order_price(preview)
    preview_outcome_price = outcome_price_from_orderbook(preview_price, args.intent) if preview_price is not None else None
    max_price = dec(args.max_price, "max_price")
    if max_price is not None and preview_outcome_price is not None and "BUY" in args.intent:
        if preview_outcome_price > max_price:
            die(f"preview outcome price {preview_outcome_price} exceeds max price {max_price}")

    proposal = {
        "ok": True,
        "mode": "dry_run_sdk_preview",
        "created_at": utc_now(),
        "market_slug": args.market_slug,
        "expected_outcome": args.expected_outcome,
        "preview_outcome": preview_outcome,
        "request": request,
        "preview": preview,
        "market_snapshot": market,
        "bbo_snapshot": bbo,
        "preview_orderbook_price": str(preview_price) if preview_price is not None else None,
        "preview_outcome_price": str(preview_outcome_price) if preview_outcome_price is not None else None,
        "estimated_notional": str(est) if est is not None else None,
        "max_notional": str(max_notional),
        "max_price": str(max_price) if max_price is not None else None,
        "notes": args.notes,
    }
    proposal["approval_token"] = approval_token(proposal)
    if args.write_receipt:
        proposal["receipt_path"] = save_receipt("sdk-proposal", args.market_slug, proposal)
    return proposal


def cmd_health(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file()
    try:
        import polymarket_us  # noqa: F401
        sdk = True
    except Exception:
        sdk = False
    return {
        "ok": sdk,
        "sdk_installed": sdk,
        "env": {
            "POLYMARKET_KEY_ID": bool(os.environ.get("POLYMARKET_KEY_ID")),
            "POLYMARKET_SECRET_KEY": bool(os.environ.get("POLYMARKET_SECRET_KEY")),
        },
        "receipt_root": str(RECEIPT_ROOT),
        "watch_root": str(WATCH_ROOT),
    }


def cmd_search_moneyline(args: argparse.Namespace) -> dict[str, Any]:
    client = sdk_client(require_auth=False)
    try:
        results = client.search.query({"query": args.query, "limit": args.limit})
        events = []
        for event in results.get("events", []) or []:
            markets = moneyline_markets(event)
            if not markets:
                continue
            events.append({
                "id": event.get("id"),
                "slug": event.get("slug"),
                "title": event.get("title"),
                "startTime": event.get("startTime"),
                "active": event.get("active"),
                "closed": event.get("closed"),
                "moneyline_markets": markets,
            })
    finally:
        client.close()
    return {"ok": True, "query": args.query, "events": events}


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

    client = sdk_client(require_auth=True)
    try:
        response = client.orders.create(proposal["request"])
    except Exception as e:
        receipt = {**proposal, "mode": "live_sdk_error", "executed_at": utc_now(), "error": repr(e), "ok": False}
        receipt["receipt_path"] = save_receipt("sdk-order-error", args.market_slug, receipt)
        print(json.dumps(as_jsonable(receipt), indent=2, sort_keys=True))
        raise SystemExit(1)
    finally:
        client.close()

    receipt = {**proposal, "mode": "live_sdk", "executed_at": utc_now(), "response": response, "ok": True}
    receipt["receipt_path"] = save_receipt("sdk-order", args.market_slug, receipt)
    if args.write_watchlist:
        filled_quantity = extract_filled_quantity(response)
        if filled_quantity > 0:
            orderbook_entry = extract_order_price(response) or extract_order_price(proposal.get("preview", {}))
            entry_price = extract_fill_price(response, args.intent) or (
                outcome_price_from_orderbook(orderbook_entry, args.intent) if orderbook_entry is not None else None
            )
            receipt["watchlist_path"] = save_watchlist({
                "active": True,
                "created_at": utc_now(),
                "market_slug": args.market_slug,
                "intent": args.intent,
                "outcome": proposal.get("preview_outcome"),
                "entry_price": str(entry_price) if entry_price is not None else None,
                "quantity": str(filled_quantity),
                "profit_cents": args.profit_cents,
                "loss_cents": args.loss_cents,
                "label": args.notes or proposal.get("preview_outcome"),
                "source_receipt": receipt["receipt_path"],
            })
        else:
            receipt["watchlist_skipped"] = "order accepted/expired without fill; no position exists to watch"
        Path(receipt["receipt_path"]).write_text(json.dumps(as_jsonable(receipt), indent=2, sort_keys=True) + "\n")
    return receipt


def add_trade_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--market-slug", required=True, help="US tradable Polymarket slug, often aec-* for sports")
    p.add_argument("--intent", required=True, choices=sorted(INTENTS), help="Use preview to verify which team this maps to")
    p.add_argument("--expected-outcome", required=True, help="Team/outcome that preview must match exactly")
    p.add_argument("--order-type", default="ORDER_TYPE_MARKET", choices=sorted(ORDER_TYPES))
    p.add_argument("--price", help="Limit/IOC price. Required for ORDER_TYPE_LIMIT and SDK market-style entries.")
    p.add_argument("--quantity", help="Limit share quantity")
    p.add_argument("--cash-order-qty", help="Market order cash/notional cap")
    p.add_argument("--max-notional", required=True, help="Hard max spend/reserved notional")
    p.add_argument("--max-price", help="Optional price discipline threshold checked against preview price")
    p.add_argument("--current-price", help="Optional current price for market-order slippage tolerance")
    p.add_argument("--slippage-bips", type=int, default=100, help="Market-order slippage tolerance in bips; default 100 = 1%%")
    p.add_argument("--tif", default="TIME_IN_FORCE_DAY", choices=sorted(TIFS))
    p.add_argument("--notes", default="")
    p.add_argument("--write-receipt", dest="write_receipt", action="store_true", default=True)
    p.add_argument("--no-write-receipt", dest="write_receipt", action="store_false")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket US SDK sports moneyline helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    health = sub.add_parser("health")
    health.set_defaults(func=cmd_health)

    search = sub.add_parser("search-moneyline")
    search.add_argument("--query", required=True, help="Exact matchup query, e.g. 'Atlanta Braves Los Angeles Dodgers'")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search_moneyline)

    propose = sub.add_parser("propose-moneyline")
    add_trade_args(propose)
    propose.set_defaults(func=cmd_propose)

    order = sub.add_parser("order-moneyline")
    add_trade_args(order)
    order.add_argument("--execute", action="store_true")
    order.add_argument("--approval-token")
    order.add_argument("--i-accept-live-trading", action="store_true")
    order.add_argument("--write-watchlist", action="store_true", help="Write heartbeat watchlist only after live order response")
    order.add_argument("--profit-cents", default="0.08")
    order.add_argument("--loss-cents", default="0.10")
    order.set_defaults(func=cmd_order)

    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(as_jsonable(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
