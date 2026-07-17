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
        promoted = self.entry(status="promoted")

        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [self.entry()]}, early), [])
        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [self.entry()]}, late), [])
        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [promoted]}, datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)), [])

    def test_entry_must_be_blocked_only_by_unconfirmed_lineups(self):
        extra_blocker = self.entry(blocked_only_by=["lineups_unconfirmed", "price_discipline"])
        broken_gate = self.entry()
        broken_gate["original_gate_results"]["starter_floor"] = False

        now = datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)

        self.assertEqual(mlb_lineup_watchlist.due_entries({"lineup_watchlist": [extra_blocker, broken_gate]}, now), [])

    def test_validation_rejects_missing_manual_safety_fields(self):
        promoted = self.entry(
            status="promoted",
            recheck={"lineups_confirmed": True, "key_injuries_refreshed": True, "price_refreshed": True, "all_original_gates_hold": True},
            promoted_candidate={"execution_mode": "automatic", "manual_bet_status": "awaiting_jerry", "executed": False},
        )

        errors = mlb_lineup_watchlist.validate_entry(promoted)

        self.assertIn("promoted_candidate.execution_mode must be manual", errors)

    def test_recheck_prompt_enforces_refresh_gates_and_manual_only_promotion(self):
        prompt = mlb_lineup_watchlist.build_recheck_prompt(Path("/tmp/schedule.json"), [self.entry()])

        self.assertIn("confirmed batting lineups", prompt)
        self.assertIn("key injury status", prompt)
        self.assertIn("current supported-market price", prompt)
        self.assertIn("every original gate", prompt)
        self.assertIn("manual_bet_status=awaiting_jerry", prompt)
        self.assertIn("must never place or schedule a bet", prompt)
        self.assertIn("lineup-abc-def", prompt)


if __name__ == "__main__":
    unittest.main()
