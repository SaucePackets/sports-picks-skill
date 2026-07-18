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


if __name__ == "__main__":
    unittest.main()
