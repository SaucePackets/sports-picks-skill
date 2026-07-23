import unittest
from unittest import mock

from scripts import polymarket_wc_markets
from scripts.polymarket_wc_markets import (
    extract_event_shells,
    extract_markets,
    extract_outcome_markets,
    fetch_events_by_tag,
    parse_json_list,
)


def gamma_event():
    """Gamma-shaped event: outcomes/outcomePrices are JSON-encoded strings."""
    return {
        "slug": "fifwc-cze-mex-2026-06-24",
        "title": "Czechia vs. Mexico",
        "volume": 17601136.07,
        "markets": [
            {
                "slug": "fifwc-cze-mex-2026-06-24-cze",
                "question": "Will Czechia win on 2026-06-24?",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.255", "0.745"]',
                "volume": "3327606.32",
            },
            {
                "slug": "fifwc-cze-mex-2026-06-24-draw",
                "question": "Will Czechia vs. Mexico end in a draw?",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.245", "0.755"]',
            },
            {
                "slug": "fifwc-cze-mex-2026-06-24-mex",
                "question": "Will Mexico win on 2026-06-24?",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.505", "0.495"]',
            },
        ],
    }


class PolymarketWcMarketsTests(unittest.TestCase):
    def test_extract_event_shells_filters_by_date(self):
        events = [
            {"slug": "fifwc-cze-mex-2026-06-24", "title": "Czechia vs. Mexico"},
            {"slug": "fifwc-mar-hai-2026-06-25", "title": "Morocco vs. Haiti"},
            {"slug": "will-trump-attend-the-final", "title": "Trump vs. schedule"},
        ]

        shells = extract_event_shells(events, date="2026-06-24")

        self.assertEqual(len(shells), 1)
        self.assertEqual(shells[0][1], "fifwc-cze-mex-2026-06-24")
        self.assertEqual(shells[0][3], ["Czechia", "Mexico"])

    def test_extract_outcome_markets_coerces_json_string_fields(self):
        outcomes = extract_outcome_markets(gamma_event(), ["Czechia", "Mexico"])

        self.assertEqual(outcomes["home"].team, "Czechia")
        self.assertEqual(outcomes["home"].yes_price, 0.255)
        self.assertEqual(outcomes["home"].no_price, 0.745)
        self.assertEqual(outcomes["home"].volume, 3327606.32)
        self.assertIsNone(outcomes["draw"].team)
        self.assertEqual(outcomes["draw"].yes_price, 0.245)
        self.assertEqual(outcomes["away"].team, "Mexico")
        self.assertEqual(outcomes["away"].yes_price, 0.505)

    def test_extract_outcome_markets_can_filter_by_base_slug(self):
        event = {
            "markets": [
                {
                    "slug": "fifwc-cze-mex-2026-06-24-cze",
                    "question": "Will Czechia win on 2026-06-24?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.255", "0.745"]',
                },
                {
                    "slug": "fifwc-cze-bra-2026-06-30-cze",
                    "question": "Will Czechia win on 2026-06-30?",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.111", "0.889"]',
                },
            ]
        }

        outcomes = extract_outcome_markets(
            event, ["Czechia", "Mexico"], base_slug="fifwc-cze-mex-2026-06-24"
        )

        self.assertEqual(outcomes["home"].yes_price, 0.255)
        self.assertEqual(len(outcomes), 1)

    def test_extract_markets_builds_match_rows_from_gamma_events(self):
        markets = extract_markets([gamma_event()])

        self.assertEqual(len(markets), 1)
        match = markets[0]
        self.assertEqual(match.slug, "fifwc-cze-mex-2026-06-24")
        self.assertEqual(match.home_team, "Czechia")
        self.assertEqual(match.away_team, "Mexico")
        self.assertEqual(match.home_price, 0.255)
        self.assertEqual(match.draw_price, 0.245)
        self.assertEqual(match.away_price, 0.505)
        self.assertEqual(match.volume, 17601136.07)

    def test_parse_json_list_tolerates_native_lists_and_garbage(self):
        self.assertEqual(parse_json_list('["Yes", "No"]'), ["Yes", "No"])
        self.assertEqual(parse_json_list(["Yes", "No"]), ["Yes", "No"])
        self.assertEqual(parse_json_list("not json"), [])
        self.assertEqual(parse_json_list(None), [])
        self.assertEqual(parse_json_list('{"a": 1}'), [])

    def test_fetch_events_by_tag_paginates_and_respects_closed_flag(self):
        first_page = [{"slug": f"fifwc-x{i}", "title": "A vs. B"} for i in range(polymarket_wc_markets.PAGE_LIMIT)]
        second_page = [{"slug": "fifwc-last", "title": "A vs. B"}]
        requested = []

        def fake_fetch_json(url, **kwargs):
            requested.append(url)
            return first_page if len(requested) == 1 else second_page

        with mock.patch.object(polymarket_wc_markets, "fetch_json", side_effect=fake_fetch_json):
            events = fetch_events_by_tag(102232)

        self.assertEqual(len(events), polymarket_wc_markets.PAGE_LIMIT + 1)
        self.assertEqual(len(requested), 2)
        self.assertIn("tag_id=102232", requested[0])
        self.assertIn("closed=false", requested[0])
        self.assertIn("offset=0", requested[0])
        self.assertIn(f"offset={polymarket_wc_markets.PAGE_LIMIT}", requested[1])

    def test_fetch_events_by_tag_can_include_closed(self):
        with mock.patch.object(polymarket_wc_markets, "fetch_json", return_value=[]) as fetch:
            fetch_events_by_tag(102232, include_closed=True)

        self.assertNotIn("closed", fetch.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
