import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "skills" / "sports-picks" / "scripts" / "polymarket_clob_wc_bet.py"
spec = importlib.util.spec_from_file_location("polymarket_clob_wc_bet", MODULE_PATH)
assert spec is not None and spec.loader is not None
clob = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clob)


class PolymarketClobWcBetTests(unittest.TestCase):
    def market_pair(self):
        match = SimpleNamespace(
            slug="fifwc-eng-mex-2026-06-18",
            title="England vs. Mexico",
            teams=["England", "Mexico"],
        )
        selected = SimpleNamespace(
            slug="fifwc-eng-mex-2026-06-18-mex",
            question="Will Mexico win on 2026-06-18?",
            team="Mexico",
            yes_price=0.42,
            no_price=0.58,
            yes_clob_token_id="YES_TOKEN",
            no_clob_token_id="NO_TOKEN",
        )
        return match, selected

    def test_best_bid_ask_sorts_book_levels(self):
        book = {
            "bids": [{"price": "0.41", "size": "12"}, {"price": "0.39", "size": "8"}],
            "asks": [{"price": "0.45", "size": "6"}, {"price": "0.43", "size": "9"}],
        }

        bid, ask = clob.best_bid_ask(book)

        self.assertEqual(str(bid), "0.41")
        self.assertEqual(str(ask), "0.43")

    def test_build_proposal_verifies_market_uses_yes_token_and_caps_notional(self):
        book = {"bids": [{"price": "0.41"}], "asks": [{"price": "0.42"}]}
        with mock.patch.object(clob, "load_exact_market", return_value=self.market_pair()):
            proposal = clob.build_proposal(
                event_slug="fifwc-eng-mex-2026-06-18",
                outcome="away",
                side="yes",
                limit_price="0.42",
                quantity="10",
                max_notional="5",
                max_price="0.45",
                expected_team="Mexico",
                book=book,
            )

        self.assertTrue(proposal["ok"])
        self.assertEqual(proposal["token_id"], "YES_TOKEN")
        self.assertEqual(proposal["market_slug"], "fifwc-eng-mex-2026-06-18-mex")
        self.assertEqual(proposal["estimated_notional"], "4.20")
        self.assertEqual(len(proposal["approval_token"]), 16)
        self.assertTrue(proposal["manual_approval_only"])

    def test_build_proposal_rejects_best_ask_above_max_price(self):
        book = {"bids": [{"price": "0.41"}], "asks": [{"price": "0.48"}]}
        with mock.patch.object(clob, "load_exact_market", return_value=self.market_pair()):
            with self.assertRaises(SystemExit):
                clob.build_proposal(
                    event_slug="fifwc-eng-mex-2026-06-18",
                    outcome="away",
                    side="yes",
                    limit_price="0.42",
                    quantity="10",
                    max_notional="5",
                    max_price="0.45",
                    expected_team="Mexico",
                    book=book,
                )

    def test_live_execute_requires_approval_flags_before_order(self):
        args = SimpleNamespace(
            event_slug="fifwc-eng-mex-2026-06-18",
            outcome="away",
            side="yes",
            price="0.42",
            quantity="10",
            max_notional="5",
            max_price="0.45",
            expected_team="Mexico",
            notes=None,
            execute=True,
            approval_token="wrong",
            i_accept_live_trading=False,
        )
        proposal = {
            "ok": True,
            "market_slug": "fifwc-eng-mex-2026-06-18-mex",
            "approval_token": "right",
        }
        with mock.patch.object(clob, "build_proposal", return_value=proposal):
            with mock.patch.object(clob, "save_json", return_value="/tmp/receipt.json"):
                with self.assertRaises(SystemExit):
                    clob.cmd_propose(args)


if __name__ == "__main__":
    unittest.main()
