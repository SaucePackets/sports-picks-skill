import base64
import json
import unittest
import zlib

from scripts.polymarket_wc_markets import (
    extract_event_shells,
    extract_markets,
    extract_outcome_markets,
    extract_serialized_payload,
    parse_clob_token_ids,
)


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
                    "clobTokenIds": '["YES_CZE", "NO_CZE"]',
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
        self.assertEqual(outcomes["home"].question, "Will Czechia win on 2026-06-24?")
        self.assertEqual(outcomes["home"].yes_price, 0.255)
        self.assertEqual(outcomes["home"].no_price, 0.745)
        self.assertEqual(outcomes["home"].yes_clob_token_id, "YES_CZE")
        self.assertEqual(outcomes["home"].no_clob_token_id, "NO_CZE")
        self.assertIsNone(outcomes["draw"].team)
        self.assertEqual(outcomes["draw"].yes_price, 0.245)
        self.assertEqual(outcomes["away"].team, "Mexico")
        self.assertEqual(outcomes["away"].yes_price, 0.505)

    def test_extract_outcome_markets_can_filter_by_base_slug(self):
        event_data = {
            "markets": [
                {
                    "slug": "fifwc-cze-mex-2026-06-24-cze",
                    "question": "Will Czechia win on 2026-06-24?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": ["0.255", "0.745"],
                    "clobTokenIds": '["YES_CZE", "NO_CZE"]',
                },
                {
                    "slug": "fifwc-cze-bra-2026-06-30-cze",
                    "question": "Will Czechia win on 2026-06-30?",
                    "outcomes": ["Yes", "No"],
                    "outcomePrices": ["0.111", "0.889"],
                },
            ]
        }

        outcomes = extract_outcome_markets(
            event_data,
            ["Czechia", "Mexico"],
            base_slug="fifwc-cze-mex-2026-06-24",
        )

        self.assertEqual(outcomes["home"].yes_price, 0.255)

    def test_parse_clob_token_ids_accepts_json_string_and_list(self):
        self.assertEqual(parse_clob_token_ids('["YES", "NO"]'), ("YES", "NO"))
        self.assertEqual(parse_clob_token_ids(["YES2", "NO2"]), ("YES2", "NO2"))
        self.assertEqual(parse_clob_token_ids("not-json"), (None, None))

    def test_extract_serialized_payload_decodes_react_flight_payload(self):
        data = {
            "events": [
                {
                    "slug": "fifwc-cze-mex-2026-06-24",
                    "title": "Czechia vs. Mexico",
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
                    ],
                }
            ]
        }
        payload = (
            base64.urlsafe_b64encode(zlib.compress(json.dumps(data).encode()))
            .decode()
            .rstrip("=")
        )
        page_html = (
            '<script>self.__next_f.push([1,"0:[{\\"serializedPayload\\":\\"$6b\\"}]"])</script>'
            f'<script>self.__next_f.push([1,"{payload}"])</script>'
        )

        decoded = extract_serialized_payload(page_html)
        markets = extract_markets(decoded, build_id=None)

        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0].home_price, 0.255)
        self.assertEqual(markets[0].draw_price, 0.245)
        self.assertEqual(markets[0].away_price, 0.505)


if __name__ == "__main__":
    unittest.main()
