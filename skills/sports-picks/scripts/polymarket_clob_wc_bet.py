#!/usr/bin/env python3
"""Guarded Polymarket CLOB helper for World Cup / soccer markets.

Dry-run by default. Live orders require explicit approval flags and CLOB env vars.
This deliberately does not use the Polymarket US SDK/gateway slug endpoints.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn, cast

CLOB_BASE = "https://clob.polymarket.com"
RECEIPT_ROOT = Path(".picks/receipts/polymarket")
WATCH_ROOT = Path(".picks/watchlist/polymarket")
USER_AGENT = "sports-picks-polymarket-clob-wc/1.0"
VALID_OUTCOMES = {"home", "draw", "away"}
VALID_SIDES = {"yes", "no"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_wc_module() -> Any:
    module_path = repo_root() / "scripts" / "polymarket_wc_markets.py"
    if not module_path.exists():
        raise SystemExit(json.dumps({"ok": False, "error": f"missing {module_path}"}))
    spec = importlib.util.spec_from_file_location("polymarket_wc_markets", module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(json.dumps({"ok": False, "error": "could not load polymarket_wc_markets module"}))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def die(message: str, code: int = 2) -> NoReturn:
    print(json.dumps({"ok": False, "error": message}, indent=2), file=sys.stderr)
    raise SystemExit(code)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slug_safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value)[:100] or "polymarket"


def dec(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        die(f"invalid decimal for {name}: {value!r}")


def http_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            # Avoid brotli decoder bugs in unattended jobs.
            "Accept-Encoding": "identity",
        },
    )
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
        raise RuntimeError(json.dumps({"status": e.code, "url": url, "response": payload}, indent=2)) from e


def fetch_order_book(token_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"token_id": token_id})
    return http_json(f"{CLOB_BASE}/book?{query}")


def amount(value: Any) -> Decimal | None:
    if isinstance(value, dict) and "value" in value:
        return dec(value["value"], "amount")
    if value is None:
        return None
    return dec(value, "amount")


def order_price(level: Any) -> Decimal | None:
    if isinstance(level, dict):
        value = level.get("price")
    elif isinstance(level, (list, tuple)) and level:
        value = level[0]
    else:
        return None
    return amount(value)


def sorted_prices(levels: Any, reverse: bool) -> list[Decimal]:
    prices = [p for p in (order_price(level) for level in (levels or [])) if p is not None]
    return sorted(prices, reverse=reverse)


def best_bid_ask(book: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    bids = sorted_prices(book.get("bids"), reverse=True)
    asks = sorted_prices(book.get("asks"), reverse=False)
    return (bids[0] if bids else None, asks[0] if asks else None)


def save_json(root: Path, prefix: str, slug: str, payload: dict[str, Any]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = root / f"{stamp}-{prefix}-{slug_safe(slug)}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return str(path)


def canonical_token_payload(proposal: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "event_slug": proposal["event_slug"],
        "market_slug": proposal["market_slug"],
        "question": proposal["question"],
        "outcome": proposal["outcome"],
        "side": proposal["side"],
        "token_id": proposal["token_id"],
        "limit_price": proposal["limit_price"],
        "quantity": proposal["quantity"],
        "estimated_notional": proposal["estimated_notional"],
        "max_notional": proposal["max_notional"],
        "max_price": proposal["max_price"],
    }
    return {k: v for k, v in keep.items() if v is not None}


def approval_token(proposal: dict[str, Any]) -> str:
    payload = json.dumps(canonical_token_payload(proposal), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_exact_market(event_slug: str, outcome: str, expected_team: str | None = None) -> tuple[Any, Any]:
    if outcome not in VALID_OUTCOMES:
        die(f"bad outcome {outcome!r}; expected one of {sorted(VALID_OUTCOMES)}")
    wc = load_wc_module()
    build_id, data = wc.load_world_cup_data()
    markets = wc.extract_markets(data, build_id=build_id)
    found_match = next((market for market in markets if market.slug == event_slug), None)
    if found_match is None:
        die(f"World Cup event slug not found in public page data: {event_slug}")
    match = cast(Any, found_match)
    selected = match.outcomes.get(outcome)
    if selected is None:
        die(f"outcome {outcome!r} not found for {event_slug}")
    if expected_team:
        actual = selected.team or "draw"
        if actual.lower() != expected_team.lower():
            die(f"team verification failed: expected {expected_team!r}, page data has {actual!r}")
    if not selected.slug.startswith(f"{event_slug}-"):
        die(f"market slug verification failed: {selected.slug!r} does not belong to {event_slug!r}")
    return match, selected


def token_for_side(selected: Any, side: str) -> str:
    if side not in VALID_SIDES:
        die(f"bad side {side!r}; expected yes or no")
    token = selected.yes_clob_token_id if side == "yes" else selected.no_clob_token_id
    if not token:
        die(f"missing {side.upper()} clobTokenId for {selected.slug}; public page data is incomplete")
    return str(token)


def build_proposal(
    *,
    event_slug: str,
    outcome: str,
    side: str,
    limit_price: str,
    quantity: str,
    max_notional: str,
    max_price: str,
    expected_team: str | None = None,
    notes: str | None = None,
    book: dict[str, Any] | None = None,
) -> dict[str, Any]:
    limit = dec(limit_price, "limit_price")
    qty = dec(quantity, "quantity")
    cap = dec(max_notional, "max_notional")
    price_cap = dec(max_price, "max_price")
    if limit <= 0 or limit >= 1:
        die("limit price must be between 0 and 1")
    if price_cap <= 0 or price_cap >= 1:
        die("max price must be between 0 and 1")
    if limit > price_cap:
        die(f"limit price {limit} exceeds max price {price_cap}")
    if qty <= 0:
        die("quantity must be positive")
    notional = limit * qty
    if notional > cap:
        die(f"estimated notional {notional} exceeds max notional {cap}")

    match, selected = load_exact_market(event_slug, outcome, expected_team=expected_team)
    token_id = token_for_side(selected, side)
    live_book = book if book is not None else fetch_order_book(token_id)
    best_bid, best_ask = best_bid_ask(live_book)
    if best_ask is not None and best_ask > price_cap:
        die(f"live best ask {best_ask} exceeds max price {price_cap}")

    proposal: dict[str, Any] = {
        "ok": True,
        "mode": "dry_run",
        "created_at": utc_now(),
        "event_slug": match.slug,
        "event_title": match.title,
        "teams": match.teams,
        "market_slug": selected.slug,
        "question": selected.question,
        "outcome": outcome,
        "team": selected.team,
        "side": side,
        "token_id": token_id,
        "limit_price": str(limit),
        "quantity": str(qty),
        "estimated_notional": str(notional),
        "max_notional": str(cap),
        "max_price": str(price_cap),
        "page_prices": {"yes": selected.yes_price, "no": selected.no_price},
        "live_book": live_book,
        "best_bid": str(best_bid) if best_bid is not None else None,
        "best_ask": str(best_ask) if best_ask is not None else None,
        "notes": notes,
        "manual_approval_only": True,
    }
    proposal["approval_token"] = approval_token(proposal)
    return proposal


def place_live_order(proposal: dict[str, Any]) -> dict[str, Any]:
    required = {
        "POLYMARKET_CLOB_PRIVATE_KEY": os.environ.get("POLYMARKET_CLOB_PRIVATE_KEY"),
        "POLYMARKET_CLOB_FUNDER": os.environ.get("POLYMARKET_CLOB_FUNDER"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        die("missing required CLOB env vars: " + ", ".join(missing))
    try:
        from py_clob_client.client import ClobClient  # type: ignore[import-not-found]
        from py_clob_client.clob_types import OrderArgs  # type: ignore[import-not-found]
        from py_clob_client.order_builder.constants import BUY  # type: ignore[import-not-found]
    except Exception:
        die("missing dependency: python -m pip install py-clob-client")

    host = os.environ.get("POLYMARKET_CLOB_HOST", CLOB_BASE)
    chain_id = int(os.environ.get("POLYMARKET_CLOB_CHAIN_ID", "137"))
    signature_type = int(os.environ.get("POLYMARKET_CLOB_SIGNATURE_TYPE", "1"))
    client = ClobClient(
        host,
        key=os.environ["POLYMARKET_CLOB_PRIVATE_KEY"],
        chain_id=chain_id,
        signature_type=signature_type,
        funder=os.environ["POLYMARKET_CLOB_FUNDER"],
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    order_args = OrderArgs(
        price=float(proposal["limit_price"]),
        size=float(proposal["quantity"]),
        side=BUY,
        token_id=proposal["token_id"],
    )
    signed_order = client.create_order(order_args)
    response = client.post_order(signed_order)
    return {"ok": True, "mode": "live", "posted_at": utc_now(), "response": response}


def cmd_health(args: argparse.Namespace) -> dict[str, Any]:
    deps = {"py_clob_client": False}
    try:
        import py_clob_client  # type: ignore[import-not-found]  # noqa: F401
        deps["py_clob_client"] = True
    except Exception:
        pass
    return {
        "ok": True,
        "clob_base": CLOB_BASE,
        "env": {
            "POLYMARKET_CLOB_PRIVATE_KEY": bool(os.environ.get("POLYMARKET_CLOB_PRIVATE_KEY")),
            "POLYMARKET_CLOB_FUNDER": bool(os.environ.get("POLYMARKET_CLOB_FUNDER")),
            "POLYMARKET_CLOB_HOST": os.environ.get("POLYMARKET_CLOB_HOST", CLOB_BASE),
        },
        "deps": deps,
    }


def cmd_book(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, "token_id": args.token_id, "book": fetch_order_book(args.token_id)}


def cmd_propose(args: argparse.Namespace) -> dict[str, Any]:
    proposal = build_proposal(
        event_slug=args.event_slug,
        outcome=args.outcome,
        side=args.side,
        limit_price=args.price,
        quantity=args.quantity,
        max_notional=args.max_notional,
        max_price=args.max_price,
        expected_team=args.expected_team,
        notes=args.notes,
    )
    proposal["receipt_path"] = save_json(RECEIPT_ROOT, "clob-proposal", proposal["market_slug"], proposal)
    proposal["watchlist_path"] = save_json(WATCH_ROOT, "clob-watch", proposal["market_slug"], proposal)

    if not args.execute:
        return proposal
    if not args.i_accept_live_trading:
        die("live orders require --i-accept-live-trading")
    if args.approval_token != proposal["approval_token"]:
        die("approval token mismatch")
    live = place_live_order(proposal)
    live_receipt = {**proposal, **live}
    live_receipt["receipt_path"] = save_json(RECEIPT_ROOT, "clob-order", proposal["market_slug"], live_receipt)
    return live_receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guarded Polymarket CLOB WC/soccer execution helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")

    book = sub.add_parser("book")
    book.add_argument("--token-id", required=True)

    propose = sub.add_parser("propose")
    propose.add_argument("--event-slug", required=True, help="Exact fifwc-* event slug from public WC page data")
    propose.add_argument("--outcome", required=True, choices=sorted(VALID_OUTCOMES))
    propose.add_argument("--side", default="yes", choices=sorted(VALID_SIDES))
    propose.add_argument("--expected-team", help="Verify selected outcome maps to this team; use 'draw' for draw")
    propose.add_argument("--price", required=True, help="Limit price for selected token")
    propose.add_argument("--quantity", required=True, help="Token quantity")
    propose.add_argument("--max-notional", required=True)
    propose.add_argument("--max-price", required=True)
    propose.add_argument("--notes")
    propose.add_argument("--execute", action="store_true")
    propose.add_argument("--approval-token")
    propose.add_argument("--i-accept-live-trading", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "health":
            result = cmd_health(args)
        elif args.cmd == "book":
            result = cmd_book(args)
        elif args.cmd == "propose":
            result = cmd_propose(args)
        else:  # pragma: no cover
            die(f"unknown command {args.cmd}")
    except RuntimeError as e:
        die(str(e), code=1)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
