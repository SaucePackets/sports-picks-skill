import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "sports-picks"
    / "scripts"
    / "polymarket_us_guard.py"
)
spec = importlib.util.spec_from_file_location("polymarket_us_guard", SCRIPT_PATH)
assert spec is not None
polymarket_us_guard = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["polymarket_us_guard"] = polymarket_us_guard
spec.loader.exec_module(polymarket_us_guard)


def bbo(bid=None, ask=None, current=None):
    market_data = {}
    if bid is not None:
        market_data["bestBid"] = {"value": bid}
    if ask is not None:
        market_data["bestAsk"] = {"value": ask}
    if current is not None:
        market_data["currentPx"] = {"value": current}
    return {"marketData": market_data}


class BboSanityTests(unittest.TestCase):
    def test_reliable_book_returns_prices_for_yes_side(self):
        prices = polymarket_us_guard.market_prices_for_side(
            bbo(bid="0.55", ask="0.58", current="0.56"), "OUTCOME_SIDE_YES"
        )

        self.assertEqual(prices["book_state"], "reliable")
        self.assertEqual(prices["exit_bid"], "0.55")
        self.assertEqual(prices["entry_ask"], "0.58")
        self.assertEqual(prices["current"], "0.56")

    def test_reliable_book_complements_prices_for_no_side(self):
        prices = polymarket_us_guard.market_prices_for_side(
            bbo(bid="0.55", ask="0.58", current="0.56"), "OUTCOME_SIDE_NO"
        )

        self.assertEqual(prices["book_state"], "reliable")
        self.assertEqual(prices["exit_bid"], "0.42")
        self.assertEqual(prices["entry_ask"], "0.45")
        self.assertEqual(prices["current"], "0.44")

    def test_missing_bid_or_ask_is_unreliable_with_null_prices(self):
        for snapshot in (bbo(ask="0.58", current="0.56"), bbo(bid="0.55", current="0.56"), bbo()):
            for side in ("OUTCOME_SIDE_YES", "OUTCOME_SIDE_NO"):
                prices = polymarket_us_guard.market_prices_for_side(snapshot, side)
                self.assertEqual(prices["book_state"], "unreliable")
                self.assertIsNone(prices["exit_bid"])
                self.assertIsNone(prices["entry_ask"])
                self.assertIsNone(prices["current"])

    def test_crossed_book_is_unreliable(self):
        prices = polymarket_us_guard.market_prices_for_side(
            bbo(bid="0.60", ask="0.58"), "OUTCOME_SIDE_NO"
        )

        self.assertEqual(prices["book_state"], "unreliable")
        self.assertIsNone(prices["exit_bid"])

    def test_wider_than_ten_cents_is_unreliable_but_exactly_ten_is_ok(self):
        wide = polymarket_us_guard.market_prices_for_side(
            bbo(bid="0.40", ask="0.55"), "OUTCOME_SIDE_YES"
        )
        boundary = polymarket_us_guard.market_prices_for_side(
            bbo(bid="0.45", ask="0.55"), "OUTCOME_SIDE_YES"
        )

        self.assertEqual(wide["book_state"], "unreliable")
        self.assertEqual(boundary["book_state"], "reliable")

    def test_bbo_book_state_helper(self):
        state = polymarket_us_guard.bbo_book_state
        self.assertEqual(state(Decimal("0.5"), Decimal("0.52")), "reliable")
        self.assertEqual(state(None, Decimal("0.52")), "unreliable")
        self.assertEqual(state(Decimal("0.52"), Decimal("0.52")), "unreliable")
        self.assertEqual(state(Decimal("0.30"), Decimal("0.41")), "unreliable")


if __name__ == "__main__":
    unittest.main()
