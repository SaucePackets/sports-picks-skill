"""Regression coverage for the approved-pick execution failure receipts.

Each test here pins a failure mode from the 2026 `approved_not_executed`
attribution rows against current main:

- 2026-06-25 HOU@DET — orders.create raised InternalServerError 500 after the
  gates passed and the pick silently died. Main retries with an
  existing-order dedup lookup (commit 02217dc); these tests make that
  behaviour load-bearing.
- 2026-07-19 DET@LAA — POLYMARKET_KEY_ID/SECRET missing from the cron
  environment; the executor must fail closed with the exact named cause and
  place nothing.
- 2026-06-20 CLE@HOU — GTC cancelled, IOC replacement expired unfilled, and
  the receipt recorded no next step. The unfilled follow-up policy now makes
  retry/reprice/stop deterministic, with price/source/timestamp provenance,
  and never places an order itself.

The executor is loaded from its file with httpx stubbed and the venv re-exec
disabled — the same pattern test_deploy_runtime.py established.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = REPO_ROOT / "skills" / "sports-picks" / "scripts" / "polymarket_us_sdk_bet.py"

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 6, 20, 22, 0, tzinfo=UTC)


@pytest.fixture()
def sp(monkeypatch, tmp_path):
    """Load the executor module fresh, sandboxed into tmp_path."""
    monkeypatch.setenv("_SP_VENV_REEXEC", "1")
    monkeypatch.setitem(sys.modules, "httpx", types.ModuleType("httpx"))
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("sp_exec_reliability_test", EXECUTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # No test in this file may sleep through retry backoff.
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    return module


# ---------------------------------------------------------------------------
# Stub SDK client
# ---------------------------------------------------------------------------

PREVIEW = {
    "order": {
        "price": "0.575",
        "marketMetadata": {"outcome": "Houston Astros"},
    }
}

MARKET = {"market": {"state": "MARKET_STATE_OPEN"}}

FILLED_RESPONSE = {
    "executions": [
        {"lastPx": "0.575", "lastShares": "40", "order": {"cumQuantity": "40"}}
    ]
}

UNFILLED_RESPONSE = {"executions": [], "status": "ORDER_STATUS_EXPIRED"}


class StubOrders:
    def __init__(self, create_results, list_result=None, preview_results=None):
        # create_results: list, one per create() call; an Exception instance raises.
        self.create_results = list(create_results)
        self.list_result = list_result if list_result is not None else []
        # preview_results: None -> always PREVIEW; a list is consumed per call,
        # an Exception instance raises.
        self.preview_results = preview_results
        self.create_calls = 0
        self.preview_calls = 0

    def create(self, request):
        self.create_calls += 1
        result = self.create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def list(self, params=None):
        return self.list_result

    def preview(self, body):
        self.preview_calls += 1
        if self.preview_results is None:
            return PREVIEW
        result = self.preview_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class StubMarkets:
    def retrieve_by_slug(self, slug):
        return MARKET

    def bbo(self, slug):
        return {}


class StubClient:
    def __init__(self, orders):
        self.orders = orders
        self.markets = StubMarkets()

    def close(self):
        pass


def install_client(monkeypatch, sp, orders):
    client = StubClient(orders)
    monkeypatch.setattr(sp, "sdk_client", lambda require_auth: client)
    return client


def order_args(**overrides):
    ns = argparse.Namespace(
        market_slug="aec-mlb-cle-hou-2026-06-20",
        intent="ORDER_INTENT_BUY_LONG",
        expected_outcome="Houston Astros",
        order_type="ORDER_TYPE_LIMIT",
        price="0.575",
        quantity="40",
        cash_order_qty=None,
        max_notional="30",
        max_price="0.575",
        current_price=None,
        slippage_bips=100,
        tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        notes="",
        write_receipt=False,
        execute=True,
        approval_token=None,
        i_accept_live_trading=True,
        write_watchlist=False,
        profit_cents="0.08",
        loss_cents="0.10",
        first_pitch_utc=None,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def run_order(monkeypatch, sp, orders, **overrides):
    args = order_args(**overrides)
    install_client(monkeypatch, sp, orders)
    # The approval token is deterministic over the canonical payload (no
    # timestamps), so a token minted from one proposal validates the next.
    args.approval_token = sp.make_proposal(order_args(**overrides))["approval_token"]
    return sp.cmd_order(args)


# ---------------------------------------------------------------------------
# 2026-06-25 HOU@DET — InternalServerError 500 on orders.create
# ---------------------------------------------------------------------------

def test_500_then_success_retries_and_records_attempts(monkeypatch, sp):
    orders = StubOrders([RuntimeError("InternalServerError 500"), FILLED_RESPONSE])
    receipt = run_order(monkeypatch, sp, orders)
    assert receipt["ok"] is True
    assert orders.create_calls == 2
    assert receipt["fill_status"] == "filled"
    errors = [a for a in receipt["order_attempts"] if "error" in a]
    assert len(errors) == 1 and "InternalServerError 500" in errors[0]["error"]


def test_500_with_order_landed_serverside_resolves_via_lookup(monkeypatch, sp):
    # The create response was lost but the order exists server-side. The
    # matching order (outcome + size + price all equal) must be adopted
    # instead of re-created — re-creating is how a 500 becomes a double bet.
    request_price = {"value": "0.575", "currency": "USD"}
    existing = {"outcome": "Houston Astros", "size": 40, "price": request_price}
    orders = StubOrders(
        [RuntimeError("InternalServerError 500")],
        list_result=[{"outcome": "Detroit Tigers"}, existing],
    )
    receipt = run_order(monkeypatch, sp, orders)
    assert receipt["ok"] is True
    assert orders.create_calls == 1
    assert any(a.get("resolved_via") == "existing_order_lookup" for a in receipt["order_attempts"])
    # A listed order carries no execution reports: its fill state is unknown,
    # and the unfilled policy must not run on it.
    assert receipt["fill_status"] == "unknown_existing_order"
    assert "unfilled_followup" not in receipt


def test_three_500s_fail_closed_with_error_receipt(monkeypatch, sp, tmp_path):
    boom = RuntimeError("InternalServerError 500")
    orders = StubOrders([boom, boom, boom])
    with pytest.raises(SystemExit) as exc:
        run_order(monkeypatch, sp, orders)
    assert exc.value.code == 1
    assert orders.create_calls == 3
    receipts = list((tmp_path / ".picks" / "receipts" / "polymarket").glob("*sdk-order-error*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text())
    assert payload["mode"] == "live_sdk_error"
    assert payload["ok"] is False
    assert len([a for a in payload["order_attempts"] if "error" in a]) == 3


# ---------------------------------------------------------------------------
# 2026-07-19 DET@LAA — credentials missing from the environment
# ---------------------------------------------------------------------------

def test_missing_credentials_fail_closed_before_any_order(monkeypatch, sp):
    fake_sdk = types.ModuleType("polymarket_us")

    class PolymarketUS:  # pragma: no cover - must never be constructed
        def __init__(self, **kwargs):
            raise AssertionError("client constructed without credentials")

    fake_sdk.PolymarketUS = PolymarketUS
    monkeypatch.setitem(sys.modules, "polymarket_us", fake_sdk)
    monkeypatch.setattr(sp, "load_env_file", lambda *a, **k: None)
    monkeypatch.delenv("POLYMARKET_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        sp.sdk_client(require_auth=True)
    assert exc.value.code == 2


def test_missing_credentials_message_names_both_keys(monkeypatch, sp, capsys):
    fake_sdk = types.ModuleType("polymarket_us")
    fake_sdk.PolymarketUS = type("PolymarketUS", (), {})
    monkeypatch.setitem(sys.modules, "polymarket_us", fake_sdk)
    monkeypatch.setattr(sp, "load_env_file", lambda *a, **k: None)
    monkeypatch.delenv("POLYMARKET_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET_KEY", raising=False)
    with pytest.raises(SystemExit):
        sp.sdk_client(require_auth=True)
    err = json.loads(capsys.readouterr().err)
    assert err == {"ok": False, "error": "missing POLYMARKET_KEY_ID or POLYMARKET_SECRET_KEY"}


# ---------------------------------------------------------------------------
# 2026-06-20 CLE@HOU — IOC expired unfilled: deterministic follow-up policy
# ---------------------------------------------------------------------------

def decide(sp, **overrides):
    params = dict(
        original_outcome_price=Decimal("0.575"),
        approved_max_price=Decimal("0.60"),
        current_ask=Decimal("0.58"),
        ask_source="sdk_order_preview",
        ask_observed_at_utc="2026-06-20T22:00:00Z",
        first_pitch_utc=NOW + dt.timedelta(hours=1),
        now_utc=NOW,
        prior_unfilled=0,
    )
    params.update(overrides)
    return sp.decide_unfilled_followup(**params)


def test_policy_stops_after_the_single_allowed_followup(sp):
    decision = decide(sp, prior_unfilled=1)
    assert (decision["action"], decision["reason_code"]) == (
        "stop", "prior_unfilled_followup_exhausted")


def test_policy_never_chases_a_started_game(sp):
    decision = decide(sp, first_pitch_utc=NOW - dt.timedelta(minutes=1))
    assert (decision["action"], decision["reason_code"]) == ("stop", "first_pitch_started")
    # An unknown first pitch cannot fire the rule, and says so in provenance.
    open_decision = decide(sp, first_pitch_utc=None)
    assert open_decision["provenance"]["first_pitch_utc"] is None
    assert open_decision["reason_code"] != "first_pitch_started"


def test_policy_refuses_a_blind_reentry_without_a_quote(sp):
    decision = decide(sp, current_ask=None, ask_source="sdk_order_preview_unavailable: boom")
    assert (decision["action"], decision["reason_code"]) == ("stop", "no_fresh_quote")
    assert decision["recommendation"] is None


def test_policy_retries_at_or_below_the_original_price(sp):
    decision = decide(sp, current_ask=Decimal("0.575"))
    assert (decision["action"], decision["reason_code"]) == (
        "retry", "ask_at_or_below_original_price")
    assert "places no order" in decision["recommendation"]


def test_policy_reprices_only_inside_the_approved_ceiling(sp):
    decision = decide(sp, current_ask=Decimal("0.59"))
    assert (decision["action"], decision["reason_code"]) == (
        "reprice", "ask_within_approved_ceiling")


def test_policy_never_chases_above_the_approved_ceiling(sp):
    # The same no-chase rule that stopped 2026-08-08 HOU@SD.
    decision = decide(sp, current_ask=Decimal("0.605"))
    assert (decision["action"], decision["reason_code"]) == (
        "stop", "ask_above_approved_ceiling")


def test_policy_without_a_ceiling_stops_rather_than_chasing(sp):
    decision = decide(sp, approved_max_price=None, current_ask=Decimal("0.58"))
    assert (decision["action"], decision["reason_code"]) == ("stop", "no_approved_ceiling")


def test_policy_provenance_carries_price_source_and_timestamps(sp):
    decision = decide(sp)
    provenance = decision["provenance"]
    assert provenance["current_ask"] == "0.58"
    assert provenance["ask_source"] == "sdk_order_preview"
    assert provenance["ask_observed_at_utc"] == "2026-06-20T22:00:00Z"
    assert provenance["now_utc"] == "2026-06-20T22:00:00Z"
    assert provenance["approved_max_price"] == "0.60"
    assert provenance["original_outcome_price"] == "0.575"
    assert provenance["first_pitch_utc"] == "2026-06-20T23:00:00Z"


# ---------------------------------------------------------------------------
# Unfilled wiring inside cmd_order
# ---------------------------------------------------------------------------

def test_unfilled_ioc_receipt_carries_policy_decision(monkeypatch, sp, tmp_path):
    orders = StubOrders([UNFILLED_RESPONSE])
    receipt = run_order(monkeypatch, sp, orders)
    assert receipt["fill_status"] == "unfilled"
    assert receipt["filled_quantity"] == "0"
    followup = receipt["unfilled_followup"]
    # Fresh quote equals the original price here, so the deterministic answer
    # is retry — and the decision is IN the saved receipt, not only stdout.
    assert (followup["action"], followup["reason_code"]) == (
        "retry", "ask_at_or_below_original_price")
    assert followup["provenance"]["ask_source"] == "sdk_order_preview"
    saved = json.loads(Path(receipt["receipt_path"]).read_text())
    assert saved["unfilled_followup"]["action"] == "retry"
    # The re-quote is the second preview call (make_proposal made the first
    # within cmd_order); no further create was attempted.
    assert orders.create_calls == 1


def test_unfilled_policy_counts_prior_unfilled_receipts_across_invocations(monkeypatch, sp, tmp_path):
    orders = StubOrders([UNFILLED_RESPONSE])
    first = run_order(monkeypatch, sp, orders)
    assert first["unfilled_followup"]["action"] == "retry"
    second = run_order(monkeypatch, sp, StubOrders([UNFILLED_RESPONSE]))
    followup = second["unfilled_followup"]
    assert (followup["action"], followup["reason_code"]) == (
        "stop", "prior_unfilled_followup_exhausted")
    assert followup["provenance"]["prior_unfilled"] == 1


def test_unfilled_prior_count_is_scoped_to_market_and_outcome(monkeypatch, sp):
    run_order(monkeypatch, sp, StubOrders([UNFILLED_RESPONSE]))
    other = run_order(monkeypatch, sp, StubOrders([UNFILLED_RESPONSE]),
                      market_slug="aec-mlb-det-laa-2026-07-19")
    assert other["unfilled_followup"]["provenance"]["prior_unfilled"] == 0


def test_unfilled_with_started_game_stops(monkeypatch, sp):
    receipt = run_order(monkeypatch, sp, StubOrders([UNFILLED_RESPONSE]),
                        first_pitch_utc="2020-01-01T00:00:00Z")
    followup = receipt["unfilled_followup"]
    assert (followup["action"], followup["reason_code"]) == ("stop", "first_pitch_started")


def test_bad_first_pitch_timestamp_dies_before_any_network_call(monkeypatch, sp):
    orders = StubOrders([FILLED_RESPONSE])
    args = order_args(first_pitch_utc="2026-06-20")  # date only: no time, no offset
    install_client(monkeypatch, sp, orders)
    with pytest.raises(SystemExit):
        sp.cmd_order(args)
    assert orders.create_calls == 0 and orders.preview_calls == 0


def test_unfilled_requote_failure_degrades_to_stop(monkeypatch, sp):
    # Preview #1 feeds the token-minting proposal, #2 the in-order proposal,
    # #3 is the post-expiry re-quote — the one that fails here.
    orders = StubOrders([UNFILLED_RESPONSE],
                        preview_results=[PREVIEW, PREVIEW, RuntimeError("preview down")])
    receipt = run_order(monkeypatch, sp, orders)
    followup = receipt["unfilled_followup"]
    assert (followup["action"], followup["reason_code"]) == ("stop", "no_fresh_quote")
    assert "preview down" in followup["provenance"]["ask_source"]


def test_filled_order_receipt_is_labelled_filled_and_runs_no_policy(monkeypatch, sp):
    receipt = run_order(monkeypatch, sp, StubOrders([FILLED_RESPONSE]))
    assert receipt["fill_status"] == "filled"
    assert receipt["filled_quantity"] == "40"
    assert "unfilled_followup" not in receipt


def test_partial_fill_is_labelled_partial_and_runs_no_policy(monkeypatch, sp):
    partial = {"executions": [
        {"lastPx": "0.575", "lastShares": "15", "order": {"cumQuantity": "15"}}
    ]}
    receipt = run_order(monkeypatch, sp, StubOrders([partial]))
    assert receipt["fill_status"] == "partial"
    assert "unfilled_followup" not in receipt


def test_policy_places_no_order_even_when_it_recommends_retry(monkeypatch, sp):
    orders = StubOrders([UNFILLED_RESPONSE])
    receipt = run_order(monkeypatch, sp, orders)
    assert receipt["unfilled_followup"]["action"] == "retry"
    assert orders.create_calls == 1  # the one expired IOC; the policy added none
    recommendation = receipt["unfilled_followup"]["recommendation"]
    assert "fresh approval token" in recommendation
