import unittest
from unittest import mock

from scripts import nfl_stage2_scan
from scripts.nfl_stage2_scan import NflSlateCollector


def schedule_event(date, team_id, opp_id, team_score, opp_score, home=True, completed=True):
    mine = {"homeAway": "home" if home else "away", "score": team_score, "team": {"id": str(team_id)}}
    opp = {"homeAway": "away" if home else "home", "score": opp_score, "team": {"id": str(opp_id)}}
    return {
        "date": date,
        "competitions": [
            {
                "status": {"type": {"completed": completed}},
                "competitors": [mine, opp],
            }
        ],
    }


class Stage2ScanTests(unittest.TestCase):
    def test_devig_normalizes_two_sided_prices(self):
        away, home = nfl_stage2_scan.devig("+120", "-140")
        self.assertAlmostEqual(away + home, 1.0)
        self.assertLess(away, home)

    def test_score_value_handles_all_espn_shapes(self):
        self.assertEqual(nfl_stage2_scan.score_value(24), 24)
        self.assertEqual(nfl_stage2_scan.score_value("17"), 17)
        self.assertEqual(nfl_stage2_scan.score_value({"value": 31.0, "displayValue": "31"}), 31)
        self.assertIsNone(nfl_stage2_scan.score_value(None))
        self.assertIsNone(nfl_stage2_scan.score_value({"displayValue": "24"}))

    def test_extract_moneylines_handles_both_odds_shapes(self):
        close_shape = {
            "odds": [{"moneyline": {"away": {"close": {"odds": "+150"}}, "home": {"close": {"odds": "-170"}}}}]
        }
        legacy_shape = {"odds": [{"awayTeamOdds": {"moneyLine": 150}, "homeTeamOdds": {"moneyLine": -170}}]}

        self.assertEqual(nfl_stage2_scan.extract_moneylines(close_shape), ("+150", "-170"))
        self.assertEqual(nfl_stage2_scan.extract_moneylines(legacy_shape), (150, -170))
        self.assertEqual(nfl_stage2_scan.extract_moneylines({}), (None, None))

    def test_team_form_counts_only_completed_games_before_slate(self):
        collector = NflSlateCollector(2026, 6)
        events = [
            schedule_event("2026-09-13T17:00Z", 2, 20, 24, 10),
            schedule_event("2026-09-20T17:00Z", 2, 17, 17, 20, home=False),
            schedule_event("2026-10-04T17:00Z", 2, 15, 30, 30),
            schedule_event("2026-10-11T17:00Z", 2, 20, 0, 0, completed=False),
            schedule_event("2026-10-18T17:00Z", 2, 33, 21, 14),  # on/after slate date
        ]
        with mock.patch.object(NflSlateCollector, "team_schedule_events", return_value=events):
            import datetime as dt

            form = collector.team_form(2, dt.date(2026, 10, 18))

        self.assertEqual(form["n"], 3)
        self.assertEqual((form["w"], form["l"], form["t"]), (1, 1, 1))
        self.assertEqual(form["pf"], 71)
        self.assertEqual(form["pa"], 60)
        self.assertEqual(form["pd"], 11)
        self.assertEqual(form["prior_season_games"], 0)
        self.assertEqual(form["last_game_date"], "2026-10-04")

    def test_team_form_backfills_from_prior_season_early_in_year(self):
        import datetime as dt

        collector = NflSlateCollector(2026, 1)
        current = []
        prior = [
            schedule_event(f"2025-12-{day:02d}T17:00Z", 2, 20, 28, 7) for day in (7, 14, 21, 28)
        ]

        def fake_schedule(self, team_id, season):
            return current if season == 2026 else prior

        with mock.patch.object(NflSlateCollector, "team_schedule_events", fake_schedule):
            form = collector.team_form(2, dt.date(2026, 9, 10))

        self.assertEqual(form["n"], 4)
        self.assertEqual(form["prior_season_games"], 4)
        self.assertEqual(form["w"], 4)

    def test_rest_days_and_short_week_flag(self):
        import datetime as dt

        collector = NflSlateCollector(2026, 7)
        events = [schedule_event("2026-10-11T17:00Z", 2, 20, 24, 10)]
        with mock.patch.object(NflSlateCollector, "team_schedule_events", return_value=events):
            rest = collector.rest_days(2, dt.date(2026, 10, 15))  # Thursday after Sunday

        self.assertEqual(rest, 4)
        self.assertLess(rest, nfl_stage2_scan.SHORT_WEEK_REST_DAYS)

    def test_one_broken_game_emits_partial_row_instead_of_killing_slate(self):
        collector = NflSlateCollector(2026, 1)
        scoreboard = {
            "events": [
                {"id": "1", "name": "Bad Game", "date": "2026-09-13T17:00Z"},
                {"id": "2", "name": "Good Game", "date": "2026-09-13T20:00Z"},
            ]
        }
        good_row = {"event_id": "2", "event": "Good Game"}

        with mock.patch.object(nfl_stage2_scan, "get", return_value=scoreboard), \
                mock.patch.object(
                    NflSlateCollector,
                    "build_row",
                    side_effect=[KeyError("competitions"), good_row],
                ):
            rows = collector.collect()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_id"], "1")
        self.assertIn("KeyError", rows[0]["error"])
        self.assertEqual(rows[1], good_row)


if __name__ == "__main__":
    unittest.main()
