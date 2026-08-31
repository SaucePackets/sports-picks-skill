import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mlb_stage2_scan
from scripts.mlb_stage2_scan import MlbSlateCollector


class Stage2ScanTests(unittest.TestCase):
    def test_devig_normalizes_two_sided_prices(self):
        away, home = mlb_stage2_scan.devig("+120", "-140")
        self.assertAlmostEqual(away + home, 1.0)
        self.assertLess(away, home)

    def test_known_park_reports_its_factor_as_available(self):
        park = mlb_stage2_scan.park_context("Coors Field")
        self.assertEqual(park["run_factor"], 112)
        self.assertEqual(park["data_status"], "available")
        self.assertIn("extreme hitter park", park["flag"])

    def test_unknown_venue_is_reported_as_an_outage_not_a_silent_null(self):
        # Journey Bank Ballpark (the neutral-site game that blocked MIL on
        # 2026-08-23) is off the fixed table. A bare null run_factor reads as
        # "no park effect" to one handicapper and as a missing required input to
        # another; naming the outage makes it route like the price and lineup
        # outages instead of terminating the game.
        park = mlb_stage2_scan.park_context("Journey Bank Ballpark")
        self.assertIsNone(park["run_factor"])
        self.assertEqual(park["data_status"], "unavailable")
        self.assertIn("unknown_park_environment", park["flag"])
        self.assertIn("Journey Bank Ballpark", park["flag"])

    def test_unknown_venue_never_gets_a_fabricated_neutral_factor(self):
        # A substituted 100 would let a park read be claimed on no data at all,
        # which is worse than the discard this change removes.
        for venue in (None, "", "   ", "Some New Ballpark"):
            park = mlb_stage2_scan.park_context(venue)
            self.assertIsNone(park["run_factor"], venue)
            self.assertEqual(park["data_status"], "unavailable", venue)

    def test_outs_from_ip_handles_partial_innings(self):
        self.assertEqual(mlb_stage2_scan.outs_from_ip("5.2"), 17)
        self.assertEqual(mlb_stage2_scan.outs_from_ip(None), 0)

    def test_one_broken_game_emits_partial_row_instead_of_killing_slate(self):
        collector = MlbSlateCollector("2026-07-22", 2026)
        espn = {
            "events": [
                {"id": "1", "name": "Bad Game", "date": "2026-07-22T20:00Z"},
                {"id": "2", "name": "Good Game", "date": "2026-07-22T23:00Z"},
            ]
        }
        good_row = {"event_id": "2", "event": "Good Game"}

        def fake_get(url):
            if "espn.com" in url:
                return espn
            return {"dates": []}

        with mock.patch.object(mlb_stage2_scan, "get", side_effect=fake_get), \
                mock.patch.object(
                    MlbSlateCollector, "match_stats_game", return_value={"gamePk": 1}
                ), \
                mock.patch.object(
                    MlbSlateCollector,
                    "build_row",
                    side_effect=[KeyError("competitions"), good_row],
                ):
            rows = collector.collect()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event_id"], "1")
        self.assertIn("KeyError", rows[0]["error"])
        self.assertEqual(rows[1], good_row)

    def test_final_boxscore_is_cached_and_reused(self):
        box = {"teams": {"home": {"team": {"id": 1}}}}
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mlb-boxscores"
            with mock.patch.object(mlb_stage2_scan, "BOXSCORE_CACHE_DIR", cache_dir), \
                    mock.patch.object(mlb_stage2_scan, "get", return_value=box) as get:
                first = mlb_stage2_scan.cached_final_boxscore(12345)
                second = mlb_stage2_scan.cached_final_boxscore(12345)

            self.assertEqual(first, box)
            self.assertEqual(second, box)
            self.assertEqual(get.call_count, 1)
            cached = json.loads((cache_dir / "12345.json").read_text())
            self.assertEqual(cached, box)

    def test_corrupt_cache_entry_is_refetched(self):
        box = {"ok": True}
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "mlb-boxscores"
            cache_dir.mkdir(parents=True)
            (cache_dir / "99.json").write_text("{not json")
            with mock.patch.object(mlb_stage2_scan, "BOXSCORE_CACHE_DIR", cache_dir), \
                    mock.patch.object(mlb_stage2_scan, "get", return_value=box) as get:
                self.assertEqual(mlb_stage2_scan.cached_final_boxscore(99), box)

            self.assertEqual(get.call_count, 1)
            self.assertEqual(json.loads((cache_dir / "99.json").read_text()), box)


def espn_event(event_id, away_abbr, home_abbr, when):
    def side(home_away, abbr):
        return {
            "homeAway": home_away,
            "team": {"abbreviation": abbr, "displayName": f"{abbr} Club", "id": abbr},
        }

    return {
        "id": event_id,
        "name": f"{away_abbr} at {home_abbr}",
        "date": when,
        "competitions": [
            {
                "competitors": [side("away", away_abbr), side("home", home_abbr)],
                "odds": [{"moneyline": {"away": {"close": {"odds": "+120"}},
                                        "home": {"close": {"odds": "-140"}}}}],
            }
        ],
    }


def stats_game(game_pk, away_abbr, home_abbr, when, away_sp, home_sp):
    def side(abbr, starter):
        return {
            "team": {"id": abbr, "abbreviation": abbr, "name": f"{abbr} Club"},
            "probablePitcher": {"fullName": starter, "id": starter},
        }

    return {
        "gamePk": game_pk,
        "gameDate": when,
        "venue": {"name": "Wrigley Field"},
        "teams": {"away": side(away_abbr, away_sp), "home": side(home_abbr, home_sp)},
    }


class Stage2DenominatorTests(unittest.TestCase):
    """The scan's row set is the slate's denominator, so it has to be exact."""

    def collect(self, events, games):
        collector = MlbSlateCollector("2026-07-11", 2026)

        def fake_get(url):
            if "espn.com" in url:
                return {"events": events}
            return {"dates": [{"games": games}]}

        with mock.patch.object(mlb_stage2_scan, "get", side_effect=fake_get), \
                mock.patch.object(mlb_stage2_scan, "team_offense_quality", return_value={}), \
                mock.patch.object(MlbSlateCollector, "team_form", return_value={}), \
                mock.patch.object(MlbSlateCollector, "bullpen", return_value={}), \
                mock.patch.object(MlbSlateCollector, "pitcher_stats", return_value={}), \
                mock.patch.object(MlbSlateCollector, "injuries", return_value=[]):
            return collector.collect()

    def test_row_carries_both_id_spaces(self):
        rows = self.collect(
            [espn_event("401816115", "MIL", "PIT", "2026-07-11T20:05Z")],
            [stats_game(823356, "MIL", "PIT", "2026-07-11T20:05:00Z", "Drohan", "Chandler")],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["game_pk"], 823356)
        self.assertEqual(rows[0]["event_id"], "401816115")
        self.assertNotEqual(str(rows[0]["game_pk"]), rows[0]["event_id"])

    def test_doubleheader_games_do_not_collapse_onto_one_statsapi_record(self):
        # Keying the join on the team pair alone kept only the last game, so
        # both events resolved to it and one game of the doubleheader was
        # described with the other's starters. Measured on the 2026 corpus: 6
        # dates, 12 rows, identical starters in every pair.
        rows = self.collect(
            [
                espn_event("401889917", "MIL", "PIT", "2026-07-11T16:05Z"),
                espn_event("401816115", "MIL", "PIT", "2026-07-11T20:05Z"),
            ],
            [
                stats_game(823357, "MIL", "PIT", "2026-07-11T16:05:00Z", "Early", "Opener"),
                stats_game(823356, "MIL", "PIT", "2026-07-11T20:05:00Z", "Late", "Closer"),
            ],
        )
        self.assertEqual([row["game_pk"] for row in rows], [823357, 823356])
        self.assertEqual([row["away_starter"] for row in rows], ["Early", "Late"])
        self.assertEqual([row["home_starter"] for row in rows], ["Opener", "Closer"])

    def test_espn_event_with_no_statsapi_game_is_recorded_not_dropped(self):
        rows = self.collect([espn_event("9", "MIL", "PIT", "2026-07-11T20:05Z")], [])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["game_pk"])
        self.assertEqual(rows[0]["event_id"], "9")
        self.assertIn("no MLB StatsAPI game", rows[0]["error"])

    def test_statsapi_game_with_no_espn_event_is_recorded_not_dropped(self):
        rows = self.collect(
            [],
            [stats_game(823356, "MIL", "PIT", "2026-07-11T20:05:00Z", "Drohan", "Chandler")],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["game_pk"], 823356)
        self.assertIsNone(rows[0]["event_id"])
        self.assertIn("no ESPN scoreboard event", rows[0]["error"])

    def test_matched_game_is_never_handed_to_a_second_event(self):
        rows = self.collect(
            [
                espn_event("1", "MIL", "PIT", "2026-07-11T16:05Z"),
                espn_event("2", "MIL", "PIT", "2026-07-11T20:05Z"),
            ],
            [stats_game(823357, "MIL", "PIT", "2026-07-11T16:05:00Z", "Early", "Opener")],
        )
        self.assertEqual(rows[0]["game_pk"], 823357)
        self.assertIsNone(rows[1]["game_pk"])
        self.assertIn("no MLB StatsAPI game", rows[1]["error"])

    def test_unreadable_start_time_never_wins_the_tiebreak(self):
        self.assertEqual(mlb_stage2_scan.start_time_gap("nope", "2026-07-11T20:05Z"), float("inf"))
        self.assertEqual(mlb_stage2_scan.start_time_gap(None, "2026-07-11T20:05Z"), float("inf"))
        self.assertEqual(
            mlb_stage2_scan.start_time_gap("2026-07-11T20:05:00Z", "2026-07-11T20:05Z"), 0.0
        )


if __name__ == "__main__":
    unittest.main()
