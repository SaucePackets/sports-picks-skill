import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig_review_gate_common.py"
spec = importlib.util.spec_from_file_location("vig_review_gate_common", SCRIPT_PATH)
assert spec is not None
vig_review_gate_common = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["vig_review_gate_common"] = vig_review_gate_common
spec.loader.exec_module(vig_review_gate_common)


class VigReviewGateCommonTests(unittest.TestCase):
    def test_pending_candidates_excludes_already_reviewed_rows(self):
        candidates = [
            {"side": "A", "vig_approved": None},
            {"side": "B", "vig_approved": True},
            {"side": "C", "vig_approved": False},
        ]

        self.assertEqual(vig_review_gate_common.pending_candidates(candidates), [candidates[0]])

    def test_mlb_review_work_includes_only_due_watchlist_entries(self):
        schedule = {
            "candidates": [],
            "lineup_watchlist": [
                {
                    "id": "due",
                    "first_pitch_utc": "2026-07-17T23:00:00Z",
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
                    "status": "pending_lineup_recheck",
                }
            ],
        }

        candidates, watchlist = vig_review_gate_common.review_work(
            schedule, "MLB", datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)
        )

        self.assertEqual(candidates, [])
        self.assertEqual([entry["id"] for entry in watchlist], ["due"])

    def test_regular_review_prompt_is_manual_only(self):
        prompt = vig_review_gate_common.build_regular_review_prompt(
            "MLB", "2026-07-17", Path("/tmp/schedule.json"), [{"side": "ABC"}]
        )

        self.assertIn("manual_bet_status=awaiting_jerry", prompt)
        self.assertIn("no execution cron", prompt)
        self.assertIn("must never place or schedule a bet", prompt)

    def test_enforce_manual_state_removes_execution_fields(self):
        schedule = {
            "candidates": [
                {
                    "side": "ABC",
                    "vig_approved": True,
                    "execution_mode": "automatic",
                    "manual_bet_status": None,
                    "executed": True,
                    "execution_cron_id": "unsafe",
                    "execution_cron_fire_utc": "2026-07-17T22:00:00Z",
                }
            ]
        }

        changed = vig_review_gate_common.enforce_manual_state(schedule)

        candidate = schedule["candidates"][0]
        self.assertTrue(changed)
        self.assertEqual(candidate["execution_mode"], "manual")
        self.assertEqual(candidate["manual_bet_status"], "awaiting_jerry")
        self.assertFalse(candidate["executed"])
        self.assertNotIn("execution_cron_id", candidate)
        self.assertNotIn("execution_cron_fire_utc", candidate)


if __name__ == "__main__":
    unittest.main()
