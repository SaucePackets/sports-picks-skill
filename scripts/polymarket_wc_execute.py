#!/usr/bin/env python3
"""Safely preview or execute Polymarket.com World Cup CLOB buy orders.

The script resolves an event or market slug through Gamma, maps a human team
name to the correct outcome token, checks current CLOB liquidity and price
limits, then uses the official py-clob-client-v2 SDK to sign and POST the order.
Dry-run is the default and never loads credentials, signs, or submits an order.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
USER_AGENT = "sports-picks-polymarket-wc-execute/1.0"
RECEIPT_ROOT = Path(".picks/receipts/polymarket")


@dataclass(frozen=True)
class MarketSelection:
    requested_slug: str
    market_slug: str
    question: str
    side: str
    outcome: str
    condition_id: str
    token_id: str
    tick_size: str
    neg_risk: bool
    minimum_order_size: str | None
    gamma_best_ask: str | None


@dataclass(frozen=True)
class Credentials:
    private_key: str
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    private_key_source: str
    api_key_source: str | None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decimal_arg(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"invalid {name}: {value!r}") from None
    if not result.is_finite():
        raise ValueError(f"invalid {name}: {value!r}")
    return result


def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def slug_safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value)[:100] or "polymarket"


def json_list(value: Any, field: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Gamma returned malformed {field}") from exc
        if isinstance(parsed, list):
            return parsed
    raise ValueError(f"Gamma returned invalid {field}")


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def gamma_url(resource: str, slug: str, *, closed: bool = False) -> str:
    query = urllib.parse.urlencode({"slug": slug, "closed": str(closed).lower()})
    return f"{GAMMA_BASE}/{resource}?{query}"


def market_candidates(slug: str, getter: Callable[[str], Any] = fetch_json) -> list[dict[str, Any]]:
    """Resolve both event slugs and individual market slugs through Gamma."""
    candidates: dict[str, dict[str, Any]] = {}
    for closed in (False, True):
        markets = getter(gamma_url("markets", slug, closed=closed))
        if isinstance(markets, list):
            for market in markets:
                if isinstance(market, dict):
                    candidates[str(market.get("id") or market.get("conditionId") or id(market))] = market
        events = getter(gamma_url("events", slug, closed=closed))
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                for market in event.get("markets", []) or []:
                    if isinstance(market, dict):
                        candidates[str(market.get("id") or market.get("conditionId") or id(market))] = market
        if candidates:
            break
    return list(candidates.values())


def _selection_score(market: Mapping[str, Any], side: str, outcomes: list[Any]) -> tuple[int, int] | None:
    wanted = normalize(side)
    raw_metadata = market.get("marketMetadata")
    metadata: Mapping[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    labels = [market.get("groupItemTitle"), metadata.get("opticOddsSelection")]
    for label in labels:
        if normalize(label) == wanted:
            yes_index = next((i for i, outcome in enumerate(outcomes) if normalize(outcome) == "yes"), None)
            if yes_index is not None:
                return 100, yes_index

    exact_outcome = [i for i, outcome in enumerate(outcomes) if normalize(outcome) == wanted]
    if len(exact_outcome) == 1:
        return 90, exact_outcome[0]

    question = normalize(market.get("question"))
    title = normalize(market.get("title"))
    slug = normalize(market.get("slug"))
    yes_index = next((i for i, outcome in enumerate(outcomes) if normalize(outcome) == "yes"), None)
    if yes_index is not None and wanted and (wanted in question or wanted in title or wanted in slug):
        return 70, yes_index
    return None


def supported_market(market: Mapping[str, Any]) -> bool:
    """Limit execution to World Cup moneyline and team-to-advance markets."""
    market_type = normalize(market.get("sportsMarketType"))
    if not market_type:
        # Older Gamma responses and generic binary markets may omit the type.
        return True
    return market_type == "moneyline" or "advance" in market_type


def select_market(slug: str, side: str, candidates: Iterable[dict[str, Any]]) -> MarketSelection:
    matches: list[tuple[int, dict[str, Any], list[Any], list[Any], int]] = []
    for market in candidates:
        if not supported_market(market):
            continue
        try:
            outcomes = json_list(market.get("outcomes"), "outcomes")
            token_ids = json_list(market.get("clobTokenIds"), "clobTokenIds")
        except ValueError:
            continue
        if len(outcomes) != len(token_ids) or not outcomes:
            continue
        scored = _selection_score(market, side, outcomes)
        if scored:
            score, outcome_index = scored
            matches.append((score, market, outcomes, token_ids, outcome_index))

    if not matches:
        raise ValueError(f"could not map side {side!r} to a market outcome for slug {slug!r}")
    matches.sort(key=lambda item: item[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        slugs = sorted({str(item[1].get("slug")) for item in matches if item[0] == matches[0][0]})
        raise ValueError(f"side {side!r} is ambiguous across markets: {', '.join(slugs)}")

    _, market, outcomes, token_ids, index = matches[0]
    if market.get("closed") is True or market.get("active") is False:
        raise ValueError(f"market {market.get('slug') or slug!r} is closed or inactive")
    if market.get("acceptingOrders") is False:
        raise ValueError(f"market {market.get('slug') or slug!r} is not accepting orders")
    condition_id = str(market.get("conditionId") or "").strip()
    if not condition_id:
        raise ValueError("Gamma market is missing conditionId")

    return MarketSelection(
        requested_slug=slug,
        market_slug=str(market.get("slug") or slug),
        question=str(market.get("question") or market.get("title") or ""),
        side=side,
        outcome=str(outcomes[index]),
        condition_id=condition_id,
        token_id=str(token_ids[index]),
        tick_size=str(market.get("orderPriceMinTickSize") or "0.01"),
        neg_risk=bool(market.get("negRisk", False)),
        minimum_order_size=(str(market["orderMinSize"]) if market.get("orderMinSize") is not None else None),
        gamma_best_ask=(str(market["bestAsk"]) if market.get("bestAsk") is not None else None),
    )


def best_ask(token_id: str, getter: Callable[[str], Any] = fetch_json) -> Decimal | None:
    url = f"{CLOB_HOST}/book?{urllib.parse.urlencode({'token_id': token_id})}"
    book = getter(url)
    asks = book.get("asks", []) if isinstance(book, dict) else []
    prices: list[Decimal] = []
    for ask in asks or []:
        value = ask.get("price") if isinstance(ask, dict) else getattr(ask, "price", None)
        if value is not None:
            prices.append(decimal_arg(value, "orderbook ask"))
    return min(prices) if prices else None


def validate_order(amount: Decimal, price: Decimal, max_price: Decimal, selection: MarketSelection) -> None:
    if amount <= 0:
        raise ValueError("--amount must be positive")
    if price <= 0 or price >= 1 or max_price <= 0 or max_price >= 1:
        raise ValueError("--price and --max-price must be between 0 and 1")
    if price > max_price:
        raise ValueError(f"order price {price} exceeds max price {max_price}")
    tick = decimal_arg(selection.tick_size, "tick size")
    if price % tick != 0:
        raise ValueError(f"price {price} does not conform to tick size {tick}")
    estimated_shares = amount / price
    if selection.minimum_order_size is not None:
        minimum = decimal_arg(selection.minimum_order_size, "minimum order size")
        if estimated_shares < minimum:
            raise ValueError(f"estimated size {estimated_shares:.4f} is below market minimum {minimum}")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def credential_env(env: Mapping[str, str] | None = None, env_files: Iterable[Path] | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    files = list(env_files) if env_files is not None else [Path.cwd() / ".env", Path.home() / ".hermes/.env"]
    for path in files:
        for key, value in parse_env_file(path).items():
            values.setdefault(key, value)
    values.update(dict(os.environ if env is None else env))
    return values


def first_present(values: Mapping[str, str], names: Iterable[str]) -> tuple[str | None, str | None]:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value, name
    return None, None


def load_credentials(env: Mapping[str, str] | None = None, env_files: Iterable[Path] | None = None) -> Credentials:
    values = credential_env(env, env_files)
    private_key, private_source = first_present(values, ("PK", "POLYMARKET_PRIVATE_KEY", "PRIVATE_KEY"))
    api_key, api_source = first_present(values, ("POLYMARKET_KEY_ID", "AK", "CLOB_API_KEY", "POLYMARKET_API_KEY"))
    api_secret, _ = first_present(values, ("POLYMARKET_SECRET_KEY", "CLOB_SECRET", "POLYMARKET_API_SECRET"))
    passphrase, _ = first_present(values, ("POLYMARKET_PASSPHRASE", "CLOB_PASS_PHRASE", "POLYMARKET_API_PASSPHRASE"))
    if not private_key:
        raise ValueError("live CLOB orders require PK, POLYMARKET_PRIVATE_KEY, or PRIVATE_KEY")
    complete = bool(api_key and api_secret and passphrase)
    return Credentials(
        private_key=private_key,
        api_key=api_key if complete else None,
        api_secret=api_secret if complete else None,
        api_passphrase=passphrase if complete else None,
        private_key_source=str(private_source),
        api_key_source=str(api_source) if complete else None,
    )


def load_sdk() -> SimpleNamespace:
    try:
        package = importlib.import_module("py_clob_client_v2")
        constants = importlib.import_module("py_clob_client_v2.order_builder.constants")
    except ImportError as exc:
        raise ValueError("missing dependency: install py-clob-client-v2") from exc
    return SimpleNamespace(
        ApiCreds=package.ApiCreds,
        ClobClient=package.ClobClient,
        MarketOrderArgs=package.MarketOrderArgs,
        OrderType=package.OrderType,
        PartialCreateOrderOptions=package.PartialCreateOrderOptions,
        BUY=constants.BUY,
    )


def authenticated_client(credentials: Credentials, signature_type: int, funder: str | None, sdk: SimpleNamespace) -> Any:
    creds = None
    if credentials.api_key:
        creds = sdk.ApiCreds(
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            api_passphrase=credentials.api_passphrase,
        )
    else:
        l1_client = sdk.ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=credentials.private_key)
        creds = l1_client.create_or_derive_api_key()
    return sdk.ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=credentials.private_key,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
    )


def submit_order(client: Any, sdk: SimpleNamespace, selection: MarketSelection, amount: Decimal, price: Decimal) -> Any:
    options = sdk.PartialCreateOrderOptions(tick_size=selection.tick_size, neg_risk=selection.neg_risk)
    signed_order = client.create_market_order(
        order_args=sdk.MarketOrderArgs(
            token_id=selection.token_id,
            side=sdk.BUY,
            amount=float(amount),
            price=float(price),
            order_type=sdk.OrderType.FAK,
        ),
        options=options,
    )
    return client.post_order(signed_order, sdk.OrderType.FAK)


def save_receipt(action: str, slug: str, payload: dict[str, Any], root: Path = RECEIPT_ROOT) -> str:
    root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = root / f"{stamp}-{action}-{slug_safe(slug)}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview or execute a Polymarket World Cup CLOB buy")
    parser.add_argument("--market-slug", required=True, help="Gamma event slug or individual market slug")
    parser.add_argument("--side", required=True, help="Team/outcome name to buy")
    parser.add_argument("--amount", required=True, help="Cash amount in USDC")
    parser.add_argument("--price", required=True, help="FAK worst execution price")
    parser.add_argument("--max-price", required=True, help="Absolute price cap")
    parser.add_argument("--execute", action="store_true", help="Sign and submit the order; default is dry-run")
    parser.add_argument("--i-accept-live-trading", action="store_true", help="Required confirmation for --execute")
    parser.add_argument("--signature-type", type=int, choices=(0, 1, 2, 3), default=None)
    parser.add_argument("--funder", help="Funder/deposit wallet address; defaults to POLYMARKET_FUNDER or DEPOSIT_WALLET_ADDRESS")
    parser.add_argument("--receipt-dir", type=Path, default=RECEIPT_ROOT)
    parser.add_argument("--no-receipt", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    amount = decimal_arg(args.amount, "amount")
    price = decimal_arg(args.price, "price")
    max_price = decimal_arg(args.max_price, "max price")
    selection = select_market(args.market_slug, args.side, market_candidates(args.market_slug))
    validate_order(amount, price, max_price, selection)
    ask = best_ask(selection.token_id)
    if ask is not None and ask > max_price:
        raise ValueError(f"best ask {ask} exceeds max price {max_price}")

    payload: dict[str, Any] = {
        "ok": True,
        "mode": "live" if args.execute else "dry_run",
        "created_at": utc_now(),
        "clob_host": CLOB_HOST,
        "market": asdict(selection),
        "order": {
            "side": "BUY",
            "cash_amount": str(amount),
            "order_type": "FAK",
            "worst_price": str(price),
            "max_price": str(max_price),
            "estimated_shares_at_limit": str(amount / price),
            "current_best_ask": str(ask) if ask is not None else None,
        },
    }
    if ask is None:
        payload["warning"] = "orderbook has no asks; a live FAK order is unlikely to fill"
    elif ask > price:
        payload["warning"] = f"best ask {ask} is above order price {price}; a live FAK order is unlikely to fill"

    if args.execute:
        if not args.i_accept_live_trading:
            raise ValueError("--execute requires --i-accept-live-trading")
        credentials = load_credentials()
        signature_type = args.signature_type
        if signature_type is None:
            signature_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        funder = args.funder or os.environ.get("POLYMARKET_FUNDER") or os.environ.get("DEPOSIT_WALLET_ADDRESS")
        sdk = load_sdk()
        client = authenticated_client(credentials, signature_type, funder, sdk)
        payload["credential_sources"] = {
            "private_key": credentials.private_key_source,
            "api_key": credentials.api_key_source or "derived_from_private_key",
        }
        payload["response"] = submit_order(client, sdk, selection, amount, price)

    if not args.no_receipt:
        action = "wc-order" if args.execute else "wc-dry-run"
        payload["receipt_path"] = save_receipt(action, selection.market_slug, payload, args.receipt_dir)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        error = {"ok": False, "created_at": utc_now(), "error": str(exc)}
        print(json.dumps(error, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
