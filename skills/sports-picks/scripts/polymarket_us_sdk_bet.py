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

# Self-heal: the Polymarket US SDK (polymarket-us) lives in the repo .venv,
# immune to hermes runtime rebuilds that rotate the shared venv. Re-exec there
# if the current interpreter lacks it. Idempotent (env sentinel stops loops).
#
# The path must follow the invoking user's home. A baked-in /home/<user> literal
# does not merely point somewhere else — it points somewhere that does not
# exist, so os.path.exists is False, the re-exec never fires, and this executor
# runs on an interpreter without polymarket_us. That fails at order time, not at
# import time, which is the worst place to discover it.
#
# The venv lives inside the runtime checkout, so the resolution has to find that
# checkout the same way the rest of this repo does. SPORTS_PICKS_RUNTIME_DIR
# alone was NOT enough: --runtime-dir sets a shell local in deploy-runtime.sh,
# the script exports nothing, and the cron repoint writes workdirs and no
# environment — so the flag never reaches this process and only the env-var path
# worked (Reviewer, PR #59).
#
# What the flag DOES reach is cron's workdir, which the repoint sets to the
# runtime checkout. So the ladder mirrors resolve_root() in the gate scripts —
# explicit env, then the current directory when it looks like a state root, then
# the default — and the flag arrives through the carrier the deploy already
# writes instead of an environment nobody propagates.
#
# Why the skip is recorded rather than just avoided: when the re-exec does not
# happen there is no output at all, and the failure surfaces much later as
# "missing dependency: pip install polymarket-us" — the wrong remedy, because
# the package IS installed, in a venv this process never entered. sdk_client
# reports _SP_VENV_SKIP_REASON so the message names the real cause.
import os as _os, sys as _sys


def _sp_resolve_runtime_dir() -> str:
    """Same ladder as resolve_root() in the gate scripts, for the same reason."""
    for _var in ("SPORTS_PICKS_RUNTIME_DIR", "SPORTS_PICKS_ROOT"):
        _value = _os.environ.get(_var)
        if _value:
            return _os.path.expanduser(_value)
    # Cron sets workdir to the runtime checkout, so cwd is what carries
    # --runtime-dir into this process. The discriminator is .deploy/runtime.marker
    # — the file deploy-runtime.sh writes into a checkout IT created and is the
    # only thing it will hard-reset. ".picks/ exists" was the wrong test: it means
    # "has pick state", not "is the deploy-managed runtime", and this repo's own
    # instructions name a second such directory (--seed-picks-from
    # ~/projects/sports-picks-skill). A dev checkout has .picks/ and no .venv, so
    # that rung captured the resolution and reopened the silent skip with no flag
    # and no env var needed (Reviewer, PR #59).
    _cwd = _os.getcwd()
    if _os.path.isfile(_os.path.join(_cwd, ".deploy", "runtime.marker")):
        return _cwd
    return _os.path.expanduser("~/projects/sports-picks-runtime")


_SP_RUNTIME_DIR = _sp_resolve_runtime_dir()
_SP_VENV = _os.environ.get("SPORTS_PICKS_VENV_PYTHON") or _os.path.join(
    _SP_RUNTIME_DIR, ".venv", "bin", "python"
)
_SP_VENV_SKIP_REASON = ""
if _os.environ.get("_SP_VENV_REEXEC"):
    _SP_VENV_SKIP_REASON = (
        f"already re-execed into {_sys.executable} and polymarket_us is still missing "
        f"— the interpreter at {_SP_VENV} does not have it installed"
    )
elif not _os.path.exists(_SP_VENV):
    _SP_VENV_SKIP_REASON = (
        f"the self-heal re-exec was skipped because {_SP_VENV} does not exist "
        "— point SPORTS_PICKS_RUNTIME_DIR at the runtime checkout, or "
        "SPORTS_PICKS_VENV_PYTHON straight at the interpreter"
    )
else:
    try:
        import polymarket_us as _sp_probe  # noqa: F401
    except ModuleNotFoundError:
        _os.environ["_SP_VENV_REEXEC"] = "1"
        _os.execv(_SP_VENV, [_SP_VENV, *_sys.argv])

import argparse
import datetime as dt
import hashlib
import httpx
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

RECEIPT_ROOT = Path(".picks/receipts/polymarket")
WATCH_ROOT = Path(".picks/watchlist/polymarket")
# Follows the invoking user's home. The previous candidate list ended in a
# baked-in /home/<other-account> literal, which on this production box is simply
# a directory that does not exist, so that fallback could only ever miss.
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

# Polymarket US sports AMM slugs are `aec-mlb-{away}-{home}-YYYY-MM-DD` with
# lowercase team codes that mostly match MLB abbreviations. Known exception
# (confirmed 2026-06-11 via search-moneyline): Arizona Diamondbacks -> "az".
# Ported from skills/sports-picks/references/polymarket-slug-abbreviations.md.
# resolve-market always verifies a built slug via the SDK before reporting it
# usable, so an unexpected code mismatch fails loud instead of mis-mapping.
_MLB_TEAM_SLUG_CODES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("az", ("ari", "az", "arizona diamondbacks", "diamondbacks", "dbacks")),
    ("ath", ("ath", "oak", "athletics", "oakland athletics", "sacramento athletics")),
    ("atl", ("atl", "atlanta braves", "braves")),
    ("bal", ("bal", "baltimore orioles", "orioles")),
    ("bos", ("bos", "boston red sox", "red sox")),
    ("chc", ("chc", "chicago cubs", "cubs")),
    ("cws", ("cws", "chw", "chicago white sox", "white sox")),
    ("cin", ("cin", "cincinnati reds", "reds")),
    ("cle", ("cle", "cleveland guardians", "guardians")),
    ("col", ("col", "colorado rockies", "rockies")),
    ("det", ("det", "detroit tigers", "tigers")),
    ("hou", ("hou", "houston astros", "astros")),
    ("kc", ("kc", "kcr", "kansas city royals", "royals")),
    ("laa", ("laa", "los angeles angels", "angels")),
    ("lad", ("lad", "los angeles dodgers", "dodgers")),
    ("mia", ("mia", "miami marlins", "marlins")),
    ("mil", ("mil", "milwaukee brewers", "brewers")),
    ("min", ("min", "minnesota twins", "twins")),
    ("nym", ("nym", "new york mets", "mets")),
    ("nyy", ("nyy", "new york yankees", "yankees")),
    ("phi", ("phi", "philadelphia phillies", "phillies")),
    ("pit", ("pit", "pittsburgh pirates", "pirates")),
    ("sd", ("sd", "sdp", "san diego padres", "padres")),
    ("sea", ("sea", "seattle mariners", "mariners")),
    ("sf", ("sf", "sfg", "san francisco giants", "giants")),
    ("stl", ("stl", "st louis cardinals", "saint louis cardinals", "cardinals")),
    ("tb", ("tb", "tbr", "tampa bay rays", "rays")),
    ("tex", ("tex", "texas rangers", "rangers")),
    ("tor", ("tor", "toronto blue jays", "blue jays")),
    ("wsh", ("wsh", "wsn", "was", "washington nationals", "nationals")),
)


def _normalize_team(value: Any) -> str:
    text = re.sub(r"[^a-z0-9 ]", "", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


_TEAM_CODE_LOOKUP: dict[str, str] = {
    alias: code for code, aliases in _MLB_TEAM_SLUG_CODES for alias in aliases
}


def team_slug_code(value: Any) -> str | None:
    """Map a team name or MLB abbreviation to its Polymarket AMM slug code."""
    return _TEAM_CODE_LOOKUP.get(_normalize_team(value))


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
        # Name the real cause. On the production box the package IS installed —
        # in the runtime venv — so "pip install polymarket-us" sends whoever
        # reads this to install it a second time in the wrong interpreter.
        if _SP_VENV_SKIP_REASON:
            die(f"polymarket_us is not importable from {sys.executable}: {_SP_VENV_SKIP_REASON}")
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


# One deterministic follow-up per approved pick, then stop. The 2026-06-20
# CLE@HOU miss was a cancelled GTC order followed by an IOC replacement that
# expired unfilled with nothing recording what should happen next; unbounded
# re-entry is the "chase" the ask-ceiling policy forbids, so the bound is 1.
MAX_UNFILLED_FOLLOWUPS = 1


def parse_utc_timestamp(value: str | None, name: str) -> dt.datetime | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        die(f"invalid UTC timestamp for {name}: {value!r}")
    if parsed.tzinfo is None:
        die(f"{name} must carry an explicit UTC offset, got naive {value!r}")
    return parsed.astimezone(dt.timezone.utc)


def prior_unfilled_order_count(market_slug: str, outcome: str | None) -> int:
    """Count earlier live-order receipts for this market+outcome that went unfilled.

    Read from the receipts on disk so the bound holds across separate CLI
    invocations run from the same working directory — the 06-20 pattern was
    two orders in two processes. RECEIPT_ROOT is cwd-relative, so a different
    cwd sees a different receipt store; production cron pins cwd to the
    deploy-managed runtime checkout, which is what makes the bound hold there.
    Receipts predating the fill_status field carry no signal and are not
    counted; unreadable receipt files are skipped rather than guessed at.
    """
    count = 0
    if not RECEIPT_ROOT.is_dir():
        return 0
    for path in sorted(RECEIPT_ROOT.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("mode") != "live_sdk":
            continue
        if payload.get("market_slug") != market_slug:
            continue
        if outcome is not None and payload.get("preview_outcome") != outcome:
            continue
        if payload.get("fill_status") == "unfilled":
            count += 1
    return count


def decide_unfilled_followup(
    *,
    original_outcome_price: Decimal | None,
    approved_max_price: Decimal | None,
    current_ask: Decimal | None,
    ask_source: str,
    ask_observed_at_utc: str | None,
    first_pitch_utc: dt.datetime | None,
    now_utc: dt.datetime,
    prior_unfilled: int,
    intent: str | None = None,
) -> dict[str, Any]:
    """Deterministic retry/reprice/stop decision for an order that did not fill.

    This function only decides — it never places an order. A retry/reprice
    recommendation means one manual re-entry through the full
    propose -> fresh approval token -> order path; the expired order's
    approval token is dead. Rules, in order:
      1. stop  prior_unfilled >= MAX_UNFILLED_FOLLOWUPS (the follow-up already ran)
      2. stop  first pitch has started (never chase live; unknown first pitch
               cannot fire this rule and is recorded as such)
      2b. stop non-BUY intent: the price-direction rules below are defined for
               buy-side asks only, matching make_proposal's ceiling gate which
               applies only when "BUY" is in the intent — on a sell the same
               comparison would call an adverse move a retry
      3. stop  no fresh executable quote to decide on
      4. retry current ask at or below the original order price
      5. stop  ask moved up and no approved ceiling exists to bound a chase
      6. stop  ask above the approved ceiling (no chase — same rule that
               stopped 2026-08-08 HOU@SD)
      7. reprice  ask above original price but within the approved ceiling
    """
    provenance = {
        "original_outcome_price": str(original_outcome_price) if original_outcome_price is not None else None,
        "approved_max_price": str(approved_max_price) if approved_max_price is not None else None,
        "current_ask": str(current_ask) if current_ask is not None else None,
        "ask_source": ask_source,
        "ask_observed_at_utc": ask_observed_at_utc,
        "first_pitch_utc": first_pitch_utc.isoformat().replace("+00:00", "Z") if first_pitch_utc else None,
        "now_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "prior_unfilled": prior_unfilled,
        "max_unfilled_followups": MAX_UNFILLED_FOLLOWUPS,
        "intent": intent,
    }

    def decision(action: str, reason_code: str, reason: str) -> dict[str, Any]:
        recommendation = None
        if action in ("retry", "reprice"):
            recommendation = (
                f"manual re-entry only: propose-moneyline -> fresh approval token -> "
                f"order-moneyline at limit <= {current_ask}; this policy places no order"
            )
        return {
            "action": action,
            "reason_code": reason_code,
            "reason": reason,
            "recommendation": recommendation,
            "provenance": provenance,
        }

    if prior_unfilled >= MAX_UNFILLED_FOLLOWUPS:
        return decision(
            "stop", "prior_unfilled_followup_exhausted",
            f"{prior_unfilled} earlier unfilled order(s) already recorded for this market/outcome; "
            f"the single allowed follow-up has been used",
        )
    if first_pitch_utc is not None and now_utc >= first_pitch_utc:
        return decision(
            "stop", "first_pitch_started",
            "first pitch has started; never chase a live game",
        )
    if intent is not None and "BUY" not in intent:
        return decision(
            "stop", "non_buy_intent",
            f"price-direction rules are defined for buy-side asks only and {intent} is a sell; "
            f"refusing to recommend a re-entry whose comparison could call an adverse move a retry",
        )
    if current_ask is None:
        return decision(
            "stop", "no_fresh_quote",
            f"no fresh executable quote available ({ask_source}); refusing to recommend a blind re-entry",
        )
    if original_outcome_price is not None and current_ask <= original_outcome_price:
        return decision(
            "retry", "ask_at_or_below_original_price",
            f"current ask {current_ask} is at or below the original order price {original_outcome_price}",
        )
    if approved_max_price is None:
        return decision(
            "stop", "no_approved_ceiling",
            f"ask moved to {current_ask} above the original order price and no approved "
            f"--max-price ceiling exists to bound a chase",
        )
    if current_ask > approved_max_price:
        return decision(
            "stop", "ask_above_approved_ceiling",
            f"current ask {current_ask} exceeds the approved ceiling {approved_max_price}; no chase",
        )
    return decision(
        "reprice", "ask_within_approved_ceiling",
        f"current ask {current_ask} is above the original order price but within the "
        f"approved ceiling {approved_max_price}",
    )


def fresh_outcome_ask(args: argparse.Namespace, request: dict[str, Any]) -> tuple[Decimal | None, str, str]:
    """Read-only re-quote for the unfilled follow-up decision.

    Reuses the SDK order preview — the same parser make_proposal trusts — so
    the quote is the executable price for OUR side, not a raw book level. Any
    failure degrades to (None, reason, now): the policy then stops rather than
    recommending a re-entry on a stale or guessed price.
    """
    observed_at = utc_now()
    try:
        client = sdk_client(require_auth=True)
        try:
            preview = client.orders.preview({"request": request})
        finally:
            client.close()
    except SystemExit:
        raise
    except Exception as exc:
        return None, f"sdk_order_preview_unavailable: {exc!r}", observed_at
    price = extract_order_price(preview)
    if price is None:
        return None, "sdk_order_preview_returned_no_price", observed_at
    return outcome_price_from_orderbook(price, args.intent), "sdk_order_preview", observed_at


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


def cmd_balance(args: argparse.Namespace) -> dict[str, Any]:
    """Query account USDC balance from Polymarket."""
    client = sdk_client(require_auth=True)
    try:
        balances = client.account.balances()
        return {"ok": True, "balances": balances}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    finally:
        client.close()


def cmd_search_moneyline(args: argparse.Namespace) -> dict[str, Any]:
    sdk_error: str | None = None
    try:
        return _search_via_sdk(args)
    except httpx.DecodingError as exc:
        # brotli decode bug — fall through to raw API
        sdk_error = f"httpx.DecodingError (brotli decode bug): {exc!r}"
    except Exception as exc:
        # any SDK error — fall through to raw API
        sdk_error = repr(exc)
    result = _search_via_raw_api(args)
    result["sdk_error"] = sdk_error
    return result


def _search_via_sdk(args: argparse.Namespace) -> dict[str, Any]:
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


def _search_via_raw_api(args: argparse.Namespace) -> dict[str, Any]:
    """Fallback search that bypasses brotli by using urllib with Accept-Encoding: identity."""
    import urllib.request
    import json as _json

    url = f"https://gateway.polymarket.us/v1/search?query={urllib.parse.quote(args.query)}&limit={args.limit}"
    req = urllib.request.Request(url, headers={
        "Accept-Encoding": "identity",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            results = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "query": args.query, "error": repr(e), "fallback": "raw_api"}

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
    return {"ok": True, "query": args.query, "events": events, "fallback": "raw_api"}


def _parse_market_outcomes(market: dict[str, Any]) -> list[str]:
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = [outcomes]
    if not isinstance(outcomes, list):
        outcomes = []
    names = [str(outcome) for outcome in outcomes]
    if not names:
        for side in market.get("marketSides", []) or []:
            team_name = side.get("team", {}).get("name") if isinstance(side, dict) else None
            if team_name:
                names.append(str(team_name))
    return names


def cmd_resolve_market(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic slug construction + SDK verification for MLB moneylines.

    Builds `aec-mlb-{away}-{home}-YYYY-MM-DD` from the in-code team-code map,
    then confirms via retrieve_by_slug that the market exists and its outcome
    names match the requested teams. Free-text search-moneyline remains the
    fallback whenever this fails.
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        die("--date must be YYYY-MM-DD")
    away_code = team_slug_code(args.away)
    home_code = team_slug_code(args.home)
    unknown = [team for team, code in ((args.away, away_code), (args.home, home_code)) if not code]
    if unknown:
        return {
            "ok": False,
            "slug": None,
            "verified": False,
            "outcomes": [],
            "startTime": None,
            "error": f"unknown team(s): {', '.join(unknown)}",
            "fallback": "use search-moneyline free-text search",
        }
    slug = f"aec-mlb-{away_code}-{home_code}-{args.date}"
    client = sdk_client(require_auth=False)
    try:
        response = client.markets.retrieve_by_slug(slug)
    except Exception as exc:
        return {
            "ok": False,
            "slug": slug,
            "verified": False,
            "outcomes": [],
            "startTime": None,
            "error": repr(exc),
            "fallback": "market not found by built slug; use search-moneyline free-text search",
        }
    finally:
        client.close()

    market = response.get("market", response) if isinstance(response, dict) else {}
    outcomes = _parse_market_outcomes(market)
    outcome_codes = {team_slug_code(outcome) for outcome in outcomes}
    verified = (
        len(outcomes) == 2
        and {away_code, home_code} <= outcome_codes
        and str(market.get("slug") or slug) == slug
    )
    result: dict[str, Any] = {
        "ok": bool(verified),
        "slug": slug,
        "verified": bool(verified),
        "outcomes": outcomes,
        "startTime": market.get("gameStartTime") or market.get("startDate"),
        "active": market.get("active"),
        "closed": market.get("closed"),
    }
    if not verified:
        result["error"] = (
            f"market found but outcome names {outcomes!r} did not match requested teams "
            f"{args.away!r}/{args.home!r}"
        )
        result["fallback"] = "use search-moneyline free-text search"
    return result


def cmd_propose(args: argparse.Namespace) -> dict[str, Any]:
    return make_proposal(args)


def _find_existing_order(market_slug: str, proposal: dict[str, Any]) -> Any | None:
    """Check if an order matching this proposal already exists (e.g. after a 500 on create).

    All three fields must match: outcome, size/quantity, and price.
    Size and price are taken from the request (what was sent to the server)
    with a fallback to the preview response."""
    client = sdk_client(require_auth=True)
    try:
        orders = client.orders.list(params={"market": market_slug})
    except Exception:
        return None
    finally:
        client.close()
    if not isinstance(orders, list):
        return None
    expected_outcome = proposal.get("preview_outcome")
    request = proposal.get("request", {})
    preview = proposal.get("preview", {})
    # Derive size and price from the actual request first, falling back to preview
    expected_size = request.get("quantity") or preview.get("size") or preview.get("quantity")
    expected_price = request.get("price") or preview.get("price")
    # If we can't determine both size and price, refuse to match — too risky
    if expected_outcome is None or expected_size is None or expected_price is None:
        return None
    for order in orders:
        if not isinstance(order, dict):
            continue
        if order.get("outcome") != expected_outcome:
            continue
        if order.get("size") != expected_size and order.get("quantity") != expected_size:
            continue
        if order.get("price") != expected_price:
            continue
        return order
    return None


def cmd_order(args: argparse.Namespace) -> dict[str, Any]:
    # Parse before any network call so a malformed timestamp fails the command
    # here, not after a live order has already been placed.
    first_pitch = parse_utc_timestamp(getattr(args, "first_pitch_utc", None), "--first-pitch-utc")
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

    response = None
    order_attempts: list[dict[str, Any]] = []
    for attempt in range(1, 4):
        client = sdk_client(require_auth=True)
        try:
            response = client.orders.create(proposal["request"])
            break
        except Exception as exc:
            order_attempts.append({"attempt": attempt, "at": utc_now(), "error": repr(exc)})
            client.close()
            # Before retrying, check if the order landed despite the error.
            # A 5xx / transport failure can mean the order was created server-side
            # but the response was lost. If we find a matching order, treat as success.
            if attempt < 3:
                try:
                    dup = _find_existing_order(args.market_slug, proposal)
                except Exception:
                    dup = None
                if dup:
                    response = dup
                    order_attempts.append({"attempt": attempt, "at": utc_now(),
                                           "resolved_via": "existing_order_lookup"})
                    break
            if attempt == 3:
                receipt = {
                    **proposal,
                    "mode": "live_sdk_error",
                    "executed_at": utc_now(),
                    "error": repr(exc),
                    "order_attempts": order_attempts,
                    "ok": False,
                }
                receipt["receipt_path"] = save_receipt("sdk-order-error", args.market_slug, receipt)
                print(json.dumps(as_jsonable(receipt), indent=2, sort_keys=True))
                raise SystemExit(1)
            time.sleep(10 * attempt)
        finally:
            client.close()

    receipt = {**proposal, "mode": "live_sdk", "executed_at": utc_now(), "response": response,
               "order_attempts": order_attempts, "ok": True}

    # Fill accounting happens on every live order, not only under
    # --write-watchlist: an accepted-but-unfilled IOC used to leave a receipt
    # indistinguishable from a fill (ok: true and nothing else), which is how
    # 2026-06-20 CLE@HOU died with no recorded next step.
    resolved_via_lookup = any(
        attempt.get("resolved_via") == "existing_order_lookup" for attempt in order_attempts
    )
    filled_quantity = extract_filled_quantity(response)
    requested_quantity = dec(proposal.get("request", {}).get("quantity"), "quantity")
    receipt["filled_quantity"] = str(filled_quantity)
    if resolved_via_lookup:
        # orders.list rows carry no execution reports, so zero here means
        # "unknown", not "no position" — never run the unfilled policy on it.
        receipt["fill_status"] = "unknown_existing_order"
        receipt["fill_status_note"] = (
            "order matched via existing-order lookup after a create error; "
            "verify the order's fill state manually before any follow-up"
        )
    elif filled_quantity <= 0:
        receipt["fill_status"] = "unfilled"
        current_ask, ask_source, ask_observed_at = fresh_outcome_ask(args, proposal["request"])
        original_orderbook_price = dec(proposal.get("request", {}).get("price"), "price")
        original_outcome_price = (
            outcome_price_from_orderbook(original_orderbook_price, args.intent)
            if original_orderbook_price is not None else None
        )
        receipt["unfilled_followup"] = decide_unfilled_followup(
            original_outcome_price=original_outcome_price,
            approved_max_price=dec(args.max_price, "max_price"),
            current_ask=current_ask,
            ask_source=ask_source,
            ask_observed_at_utc=ask_observed_at,
            first_pitch_utc=first_pitch,
            now_utc=dt.datetime.now(dt.timezone.utc),
            prior_unfilled=prior_unfilled_order_count(
                args.market_slug, proposal.get("preview_outcome")
            ),
            intent=args.intent,
        )
    elif requested_quantity is not None and filled_quantity < requested_quantity:
        receipt["fill_status"] = "partial"
    else:
        receipt["fill_status"] = "filled"

    receipt["receipt_path"] = save_receipt("sdk-order", args.market_slug, receipt)
    if args.write_watchlist:
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

    balance = sub.add_parser("balance")
    balance.set_defaults(func=cmd_balance)

    search = sub.add_parser("search-moneyline")
    search.add_argument("--query", required=True, help="Exact matchup query, e.g. 'Atlanta Braves Los Angeles Dodgers'")
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search_moneyline)

    resolve = sub.add_parser("resolve-market", help="Deterministic aec-mlb slug lookup verified via the SDK")
    resolve.add_argument("--away", required=True, help="Away team name or MLB abbreviation, e.g. ARI or 'Arizona Diamondbacks'")
    resolve.add_argument("--home", required=True, help="Home team name or MLB abbreviation")
    resolve.add_argument("--date", required=True, help="Slug game date YYYY-MM-DD")
    resolve.set_defaults(func=cmd_resolve_market)

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
    order.add_argument("--first-pitch-utc", default=None,
                       help="Game first pitch as UTC ISO timestamp; lets the unfilled follow-up "
                            "policy refuse to recommend a re-entry once the game has started")
    order.set_defaults(func=cmd_order)

    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(as_jsonable(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
