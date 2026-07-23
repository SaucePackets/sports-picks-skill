import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_lineup_watchlist.py"
spec = importlib.util.spec_from_file_location("mlb_lineup_watchlist", SCRIPT_PATH)
assert spec is not None
mlb_lineup_watchlist = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["mlb_lineup_watchlist"] = mlb_lineup_watchlist
spec.loader.exec_module(mlb_lineup_watchlist)


class MlbLineupWatchlistTests(unittest.TestCase):
    def entry(self, **overrides):
        item = {
            "id": "lineup-abc-def",
            "game": "ABC @ DEF",
            "side": "ABC",
            "first_pitch_utc": "2026-07-17T23:00:00Z",
            "recheck_due_utc": "2026-07-17T21:45:00Z",
            "blocked_only_by": ["lineups_unconfirmed"],
            "original_gate_results": {
                "starter_floor": True,
                "opposing_starter_shutdown_path": True,
                "bullpen_close_game_survival": True,
                "cold_fade_reset": True,
                "price_discipline": True,
                "real_winner_conviction": True,
                "lineups_confirmed": False,
            },
            "original_price": -125,
            "bettable_to_price": -135,
            "status": "pending_lineup_recheck",
        }
        item.update(overrides)
        return item

    def test_due_entry_is_selected_inside_sixty_to_ninety_minute_window(self):
        schedule = {"lineup_watchlist": [self.entry()]}
        now = datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)

        due = mlb_lineup_watchlist.due_entries(schedule, now)

        self.assertEqual([item["id"] for item in due], ["lineup-abc-def"])

    def test_entry_is_not_due_outside_window_or_after_terminal_status(self):
        early = datetime(2026, 7, 17, 21, 20, tzinfo=timezone.utc)
        late = datetime(2026, 7, 17, 22, 5, tzinfo=timezone.utc)
        promoted_candidate = {
            "watchlist_id": "lineup-abc-def",
            "sport": "MLB",
            "market_type": "moneyline",
            "execution_mode": "standing_authorized",
            "execution_status": "pending",
            "max_polymarket_price": 0.51,
            "executed": False,
        }
        promoted = self.entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={
                "lineups_confirmed": True,
                "key_injuries_refreshed": True,
                "price_refreshed": True,
                "all_original_gates_hold": True,
            },
            promoted_candidate=promoted_candidate,
        )

        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [self.entry()]}, early), [])
        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [self.entry()]}, late), [])
        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [promoted]}, datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)), [])

    def test_entry_must_be_blocked_only_by_unconfirmed_lineups(self):
        extra_blocker = self.entry(blocked_only_by=["lineups_unconfirmed", "price_discipline"])
        broken_gate = self.entry()
        broken_gate["original_gate_results"]["starter_floor"] = False

        now = datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)

        with self.assertRaises(mlb_lineup_watchlist.WatchlistFormatError):
            mlb_lineup_watchlist.due_entries({"lineup_watchlist": [extra_blocker, broken_gate]}, now)

    def test_pending_entry_requires_identity_timing_and_prices(self):
        broken = self.entry(id="", recheck_due_utc="bad", original_price=None, bettable_to_price=None)

        errors = mlb_lineup_watchlist.validate_entry(broken)

        self.assertIn("id must be a non-empty string", errors)
        self.assertIn("recheck_due_utc must be a valid timestamp", errors)
        self.assertIn("original_price must be numeric", errors)
        self.assertIn("bettable_to_price must be numeric", errors)

    def test_slate_schedule_rejects_descriptive_and_quoted_watchlist_prices(self):
        slate_schedule = {
            "date": "2026-07-18",
            "candidates": [],
            "lineup_watchlist": [
                self.entry(
                    id="LW20260718-MIN-CHC",
                    original_price="MIN +119 at DraftKings",
                    bettable_to_price="+105",
                )
            ],
        }

        errors = mlb_lineup_watchlist.validate_watchlist(slate_schedule)

        self.assertEqual(
            errors["LW20260718-MIN-CHC"],
            ["original_price must be numeric", "bettable_to_price must be numeric"],
        )

    def test_duplicate_watchlist_ids_fail_closed(self):
        with self.assertRaises(mlb_lineup_watchlist.WatchlistFormatError):
            mlb_lineup_watchlist.due_entries(
                {"lineup_watchlist": [self.entry(), self.entry()]},
                datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc),
            )

    def test_passed_entry_requires_timestamp_and_exact_blocker(self):
        errors = mlb_lineup_watchlist.validate_entry(self.entry(status="passed"))

        self.assertIn("passed entry requires rechecked_at_utc", errors)
        self.assertIn("passed entry requires non-empty recheck_notes", errors)

    def test_validation_rejects_manual_state_for_standing_authorized_mlb(self):
        promoted = self.entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck={"lineups_confirmed": True, "key_injuries_refreshed": True, "price_refreshed": True, "all_original_gates_hold": True},
            promoted_candidate={"watchlist_id": "lineup-abc-def", "execution_mode": "manual", "manual_bet_status": "awaiting_jerry", "executed": False},
        )

        errors = mlb_lineup_watchlist.validate_entry(promoted)

        self.assertIn("promoted_candidate.execution_mode must be standing_authorized", errors)
        self.assertIn("promoted_candidate.execution_status must be pending", errors)
        self.assertIn("promoted_candidate.max_polymarket_price must be between 0 and 1", errors)
        self.assertIn("promoted_candidate.sport must be MLB", errors)
        self.assertIn("promoted_candidate.market_type must be moneyline", errors)

    def test_recheck_prompt_routes_promotion_to_recurring_execution_poller(self):
        prompt = mlb_lineup_watchlist.build_recheck_prompt(Path("/tmp/schedule.json"), [self.entry()])

        self.assertIn("confirmed batting lineups", prompt)
        self.assertIn("key injury status", prompt)
        self.assertIn("current supported-market price", prompt)
        self.assertIn("every original gate", prompt)
        self.assertIn("execution_mode=standing_authorized", prompt)
        self.assertIn("execution_status=pending", prompt)
        self.assertIn("recurring MLB execution poller", prompt)
        self.assertNotIn("awaiting_jerry", prompt)
        self.assertIn("lineup-abc-def", prompt)

    def test_lineup_snapshot_maps_espn_event_to_mlb_game_pk_before_fetching_feed(self):
        entry = self.entry(
            event_id="401816229",
            game="Cincinnati Reds at Seattle Mariners",
            first_pitch_utc="2026-07-22T19:40:00Z",
        )
        schedule = {
            "dates": [{
                "games": [{
                    "gamePk": 823110,
                    "teams": {
                        "away": {"team": {"name": "Cincinnati Reds"}},
                        "home": {"team": {"name": "Seattle Mariners"}},
                    },
                }]
            }]
        }
        away_order = list(range(1, 10))
        home_order = list(range(10, 19))
        players = {
            f"ID{player_id}": {"fullName": f"Player {player_id}"}
            for player_id in range(1, 53)
        }
        feed = {
            "gameData": {"players": players},
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": away_order, "players": {}},
                "home": {"battingOrder": home_order, "players": {}},
            }}},
        }
        requested_urls = []

        def fetch_json(url):
            requested_urls.append(url)
            if "/api/v1/schedule?" in url:
                return schedule
            if url.endswith("/api/v1.1/game/823110/feed/live"):
                return feed
            self.fail(f"unexpected URL: {url}")

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)

        self.assertEqual(snapshot["game_pk"], 823110)
        self.assertEqual(snapshot["player_count"], 52)
        self.assertEqual(len(snapshot["away_batting_order"]), 9)
        self.assertEqual(len(snapshot["home_batting_order"]), 9)
        self.assertIn("date=2026-07-22", requested_urls[0])
        self.assertNotIn("401816229/feed/live", "\n".join(requested_urls))

    def test_lineup_snapshot_uses_espn_event_teams_when_game_name_is_missing(self):
        entry = self.entry(
            event_id="401816229",
            game="",
            first_pitch_utc="2026-07-22T19:40:00Z",
        )
        schedule = {
            "dates": [{"games": [{
                "gamePk": 823110,
                "teams": {
                    "away": {"team": {"name": "Cincinnati Reds"}},
                    "home": {"team": {"name": "Seattle Mariners"}},
                },
            }]}]
        }
        espn = {
            "header": {"competitions": [{"competitors": [
                {"homeAway": "home", "team": {"displayName": "Seattle Mariners"}},
                {"homeAway": "away", "team": {"displayName": "Cincinnati Reds"}},
            ]}]}
        }
        feed = {
            "gameData": {"players": {}},
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": []},
                "home": {"battingOrder": []},
            }}},
        }
        requested_urls = []

        def fetch_json(url):
            requested_urls.append(url)
            if "/api/v1/schedule?" in url:
                return schedule
            if "site.api.espn.com" in url:
                return espn
            if url.endswith("/api/v1.1/game/823110/feed/live"):
                return feed
            self.fail(f"unexpected URL: {url}")

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)

        self.assertEqual(snapshot["game_pk"], 823110)
        self.assertIn("event=401816229", requested_urls[1])
        self.assertTrue(requested_urls[2].endswith("/823110/feed/live"))

    def test_resolve_game_pk_picks_doubleheader_game_nearest_first_pitch(self):
        schedule = {
            "dates": [{
                "games": [
                    {
                        "gamePk": 111,
                        "gameDate": "2026-07-22T17:10:00Z",
                        "teams": {
                            "away": {"team": {"name": "Cincinnati Reds"}},
                            "home": {"team": {"name": "Seattle Mariners"}},
                        },
                    },
                    {
                        "gamePk": 222,
                        "gameDate": "2026-07-22T23:40:00Z",
                        "teams": {
                            "away": {"team": {"name": "Cincinnati Reds"}},
                            "home": {"team": {"name": "Seattle Mariners"}},
                        },
                    },
                ]
            }]
        }
        nightcap_first_pitch = datetime(2026, 7, 22, 23, 40, tzinfo=timezone.utc)

        game_pk = mlb_lineup_watchlist.resolve_game_pk(
            schedule, "Cincinnati Reds", "Seattle Mariners", first_pitch=nightcap_first_pitch
        )

        self.assertEqual(game_pk, 222)

        opener_first_pitch = datetime(2026, 7, 22, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(
            mlb_lineup_watchlist.resolve_game_pk(
                schedule, "Cincinnati Reds", "Seattle Mariners", first_pitch=opener_first_pitch
            ),
            111,
        )

    def test_lineup_snapshot_uses_stamped_game_pk_without_schedule_lookup(self):
        entry = self.entry(
            game_pk=823110,
            game="Cincinnati Reds at Seattle Mariners",
            first_pitch_utc="2026-07-22T19:40:00Z",
        )
        feed = {
            "gameData": {
                "players": {"ID1": {"fullName": "Player 1"}},
                "teams": {
                    "away": {"name": "Cincinnati Reds"},
                    "home": {"name": "Seattle Mariners"},
                },
            },
            "liveData": {"boxscore": {"teams": {
                "away": {"battingOrder": [1]},
                "home": {"battingOrder": []},
            }}},
        }
        requested_urls = []

        def fetch_json(url):
            requested_urls.append(url)
            if url.endswith("/api/v1.1/game/823110/feed/live"):
                return feed
            self.fail(f"unexpected URL: {url}")

        snapshot = mlb_lineup_watchlist.fetch_lineup_snapshot(entry, fetch_json=fetch_json)

        self.assertEqual(snapshot["game_pk"], 823110)
        self.assertEqual(snapshot["away_team"], "Cincinnati Reds")
        self.assertEqual(snapshot["home_team"], "Seattle Mariners")
        self.assertEqual(requested_urls, ["https://statsapi.mlb.com/api/v1.1/game/823110/feed/live"])

    def test_validate_entry_rejects_non_positive_or_non_integer_game_pk(self):
        for bad in (0, -5, True, "823110", 1.5):
            errors = mlb_lineup_watchlist.validate_entry(self.entry(game_pk=bad))
            self.assertIn("game_pk must be a positive integer when present", errors, msg=repr(bad))

        self.assertEqual(mlb_lineup_watchlist.validate_entry(self.entry(game_pk=823110)), [])
        self.assertEqual(mlb_lineup_watchlist.validate_entry(self.entry()), [])

    def test_recheck_prompt_includes_concise_resolved_mlb_lineups(self):
        snapshot = {
            "game_pk": 823110,
            "away_team": "Cincinnati Reds",
            "home_team": "Seattle Mariners",
            "player_count": 52,
            "away_batting_order": [f"Red {number}" for number in range(1, 10)],
            "home_batting_order": [f"Mariner {number}" for number in range(1, 10)],
        }

        prompt = mlb_lineup_watchlist.build_recheck_prompt(
            Path("/tmp/schedule.json"),
            [self.entry(id="2026-07-22-SEA-ML")],
            {"2026-07-22-SEA-ML": snapshot},
        )

        self.assertIn("MLB gamePk 823110", prompt)
        self.assertIn("52 roster players", prompt)
        self.assertIn("Cincinnati Reds batting order (9)", prompt)
        self.assertIn("Seattle Mariners batting order (9)", prompt)
        self.assertNotIn("{\"game_pk\"", prompt)


if __name__ == "__main__":
    unittest.main()
