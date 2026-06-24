import unittest

from scripts.polymarket_wc_markets import extract_event_shells, extract_outcome_markets


class PolymarketWcMarketsTests(unittest.TestCase):
    def test_extract_event_shells_filters_by_date(self):
        data = {
            "events": [
                {"slug": "fifwc-cze-mex-2026-06-24", "title": "Czechia vs. Mexico"},
                {"slug": "fifwc-mar-hai-2026-06-25", "title": "Morocco vs. Haiti"},
            ]
        }

        shells = extract_event_shells(data, date="2026-06-24")

        self.assertEqual(len(shells), 1)
        self.assertEqual(shells[0][0], "fifwc-cze-mex-2026-06-24")
        self.assertEqual(shells[0][2], ["Czechia", "Mexico"])

    def test_extract_outcome_markets_keeps_no_as_not_side(self):
        event_data = {
            "markets": [
                {
                    "slug": "fifwc-cze-mex-2026-06-24-cze",
                    "question": "Will Czechia win on 2026-06-24?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": ["0.255", "0.745"],
                },
                {
                    "slug": "fifwc-cze-mex-2026-06-24-draw",
                    "question": "Will Czechia vs. Mexico end in a draw?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": ["0.245", "0.755"],
                },
                {
                    "slug": "fifwc-cze-mex-2026-06-24-mex",
                    "question": "Will Mexico win on 2026-06-24?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": ["0.505", "0.495"],
                },
            ]
        }

        outcomes = extract_outcome_markets(event_data, ["Czechia", "Mexico"])

        self.assertEqual(outcomes["home"].team, "Czechia")
        self.assertEqual(outcomes["home"].yes_price, 0.255)
        self.assertEqual(outcomes["home"].no_price, 0.745)
        self.assertIsNone(outcomes["draw"].team)
        self.assertEqual(outcomes["draw"].yes_price, 0.245)
        self.assertEqual(outcomes["away"].team, "Mexico")
        self.assertEqual(outcomes["away"].yes_price, 0.505)


if __name__ == "__main__":
    unittest.main()
