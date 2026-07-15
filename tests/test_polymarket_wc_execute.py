import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.polymarket_wc_execute import (
    Credentials,
    MarketSelection,
    authenticated_client,
    best_ask,
    load_credentials,
    market_candidates,
    select_market,
    submit_order,
    validate_order,
)


def sample_market(**overrides):
    market = {
        "id": "123",
        "slug": "fifwc-cze-mex-2026-06-24-mex",
        "question": "Will Mexico win on 2026-06-24?",
        "groupItemTitle": "Mexico",
        "conditionId": "0xcondition",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "orderPriceMinTickSize": 0.0025,
        "orderMinSize": 5,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "negRisk": True,
        "bestAsk": 0.505,
    }
    market.update(overrides)
    return market


class MarketResolutionTests(unittest.TestCase):
    def test_event_slug_resolves_team_to_yes_token(self):
        selected = select_market(
            "fifwc-cze-mex-2026-06-24",
            "Mexico",
            [sample_market()],
        )
        self.assertEqual(selected.condition_id, "0xcondition")
        self.assertEqual(selected.token_id, "yes-token")
        self.assertEqual(selected.outcome, "Yes")
        self.assertTrue(selected.neg_risk)
        self.assertEqual(selected.tick_size, "0.0025")

    def test_direct_multi_outcome_market_selects_named_outcome(self):
        market = sample_market(
            slug="world-cup-winner",
            question="Who will win the World Cup?",
            groupItemTitle=None,
            outcomes='["Mexico", "Brazil"]',
            clobTokenIds='["mex-token", "bra-token"]',
        )
        selected = select_market("world-cup-winner", "Brazil", [market])
        self.assertEqual(selected.token_id, "bra-token")
        self.assertEqual(selected.outcome, "Brazil")

    def test_more_markets_slug_ignores_spreads_for_to_advance(self):
        spread = sample_market(
            id="spread",
            slug="fifwc-eng-arg-spread-away-1pt5",
            sportsMarketType="spread",
            groupItemTitle="Argentina +1.5",
            outcomes='["England", "Argentina"]',
            clobTokenIds='["spread-eng", "spread-arg"]',
        )
        advance = sample_market(
            id="advance",
            slug="fifwc-eng-arg-team-to-advance",
            sportsMarketType="soccer_team_to_advance",
            groupItemTitle="Team to Advance",
            outcomes='["England", "Argentina"]',
            clobTokenIds='["advance-eng", "advance-arg"]',
        )
        selected = select_market("fifwc-eng-arg-more-markets", "Argentina", [spread, advance])
        self.assertEqual(selected.market_slug, "fifwc-eng-arg-team-to-advance")
        self.assertEqual(selected.token_id, "advance-arg")

    def test_closed_market_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "closed or inactive"):
            select_market("slug", "Mexico", [sample_market(closed=True)])

    def test_ambiguous_side_is_rejected(self):
        second = sample_market(id="456", slug="another-mexico-market")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            select_market("event", "Mexico", [sample_market(), second])

    def test_gamma_resolver_accepts_event_markets(self):
        responses = {
            "markets?slug=event&closed=false": [],
            "events?slug=event&closed=false": [{"markets": [sample_market()]}],
        }

        def getter(url):
            return next(value for suffix, value in responses.items() if url.endswith(suffix))

        self.assertEqual(market_candidates("event", getter), [sample_market()])


class OrderValidationTests(unittest.TestCase):
    def setUp(self):
        self.selection = select_market("event", "Mexico", [sample_market()])

    def test_world_cup_quarter_cent_tick_is_accepted(self):
        validate_order(Decimal("10"), Decimal("0.5075"), Decimal("0.51"), self.selection)

    def test_nonconforming_tick_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "tick size"):
            validate_order(Decimal("10"), Decimal("0.506"), Decimal("0.51"), self.selection)

    def test_price_over_cap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exceeds max price"):
            validate_order(Decimal("10"), Decimal("0.51"), Decimal("0.50"), self.selection)

    def test_best_ask_uses_lowest_book_price(self):
        ask = best_ask("token", lambda _: {"asks": [{"price": "0.52"}, {"price": "0.51"}]})
        self.assertEqual(ask, Decimal("0.51"))


class CredentialTests(unittest.TestCase):
    def test_pk_only_derives_api_credentials(self):
        creds = load_credentials(env={"PK": "0xprivate"}, env_files=[])
        self.assertEqual(creds.private_key_source, "PK")
        self.assertIsNone(creds.api_key)

    def test_existing_aliases_require_passphrase_to_be_complete(self):
        creds = load_credentials(
            env={
                "POLYMARKET_PRIVATE_KEY": "0xprivate",
                "POLYMARKET_KEY_ID": "key",
                "POLYMARKET_SECRET_KEY": "secret",
                "POLYMARKET_PASSPHRASE": "pass",
            },
            env_files=[],
        )
        self.assertEqual(creds.api_key, "key")
        self.assertEqual(creds.api_secret, "secret")
        self.assertEqual(creds.api_passphrase, "pass")
        self.assertEqual(creds.api_key_source, "POLYMARKET_KEY_ID")

    def test_env_file_fallback_supports_pk_and_ak(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text("PK=0xprivate\nAK=api-key\nCLOB_SECRET=secret\nCLOB_PASS_PHRASE=pass\n")
            creds = load_credentials(env={}, env_files=[env_file])
        self.assertEqual(creds.private_key, "0xprivate")
        self.assertEqual(creds.api_key, "api-key")
        self.assertEqual(creds.api_key_source, "AK")

    def test_missing_private_key_is_rejected_even_with_api_pair(self):
        with self.assertRaisesRegex(ValueError, "PRIVATE_KEY"):
            load_credentials(
                env={"POLYMARKET_KEY_ID": "key", "POLYMARKET_SECRET_KEY": "secret"},
                env_files=[],
            )


class FakeApiCreds:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeMarketOrderArgs:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.created = None
        self.posted = None
        FakeClient.instances.append(self)

    def create_or_derive_api_key(self):
        return "derived-creds"

    def create_market_order(self, **kwargs):
        self.created = kwargs
        return "signed-order"

    def post_order(self, order, order_type):
        self.posted = (order, order_type)
        return {"success": True, "orderID": "0xorder"}


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances.clear()
        self.sdk = SimpleNamespace(
            ApiCreds=FakeApiCreds,
            ClobClient=FakeClient,
            MarketOrderArgs=FakeMarketOrderArgs,
            PartialCreateOrderOptions=FakeOptions,
            OrderType=SimpleNamespace(FAK="FAK"),
            BUY="BUY",
        )
        self.selection = select_market("event", "Mexico", [sample_market()])

    def test_missing_l2_credentials_are_derived_from_private_key(self):
        creds = Credentials("0xprivate", None, None, None, "PK", None)
        client = authenticated_client(creds, 3, "0xfunder", self.sdk)
        self.assertEqual(len(FakeClient.instances), 2)
        self.assertEqual(client.kwargs["creds"], "derived-creds")
        self.assertEqual(client.kwargs["signature_type"], 3)
        self.assertEqual(client.kwargs["funder"], "0xfunder")

    def test_order_is_constructed_signed_then_posted_as_fak(self):
        client = FakeClient(host="host")
        response = submit_order(client, self.sdk, self.selection, Decimal("20"), Decimal("0.51"))
        self.assertIsNotNone(client.created)
        self.assertEqual(client.created["order_args"].kwargs["token_id"], "yes-token")
        self.assertEqual(client.created["order_args"].kwargs["amount"], 20.0)
        self.assertEqual(client.created["order_args"].kwargs["order_type"], "FAK")
        self.assertEqual(client.created["options"].kwargs["tick_size"], "0.0025")
        self.assertEqual(client.posted, ("signed-order", "FAK"))
        self.assertTrue(response["success"])


if __name__ == "__main__":
    unittest.main()
