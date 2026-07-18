import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig_review_gate_common.py"
spec = importlib.util.spec_from_file_location("vig_review_gate_common", SCRIPT_PATH)
assert spec is not None
vig_review_gate_common = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["vig_review_gate_common"] = vig_review_gate_common
spec.loader.exec_module(vig_review_gate_common)


class VigReviewGateCommonTests(unittest.TestCase):
    def test_resolve_root_falls_back_from_profile_scripts_to_default_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "projects" / "sports-picks-skill"
            scripts = home / ".hermes" / "profiles" / "vig" / "scripts"
            (project / ".picks").mkdir(parents=True)
            scripts.mkdir(parents=True)

            root = vig_review_gate_common.resolve_root(cwd=scripts, home=home)

            self.assertEqual(root, project.resolve())

    def test_raw_candidate_array_rejects_non_objects(self):
        with self.assertRaises(vig_review_gate_common.ScheduleFormatError):
            vig_review_gate_common.parse_candidates([{"side": "A"}, "bad"])

    def test_nonempty_legacy_array_fails_before_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                setattr(vig_review_gate_common, "ROOT", Path(tmp))
                day = vig_review_gate_common.datetime.now(
                    vig_review_gate_common.ZoneInfo("America/Chicago")
                ).date().isoformat()
                schedule = Path(tmp) / ".picks" / "execute" / f"{day}-schedule.json"
                schedule.parent.mkdir(parents=True)
                schedule.write_text(json.dumps([{"side": "ABC", "vig_approved": None}]))

                self.assertEqual(vig_review_gate_common.run_gate("MLB"), 1)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

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
            ],
        }

        candidates, watchlist = vig_review_gate_common.review_work(
            schedule, "MLB", datetime(2026, 7, 17, 21, 45, tzinfo=timezone.utc)
        )

        self.assertEqual(candidates, [])
        self.assertEqual([entry["id"] for entry in watchlist], ["due"])

    def test_invalid_slate_prices_surface_as_gate_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                day = vig_review_gate_common.datetime.now(
                    vig_review_gate_common.ZoneInfo("America/Chicago")
                ).date().isoformat()
                schedule = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule.parent.mkdir(parents=True)
                bad_entry = self._watch_entry(
                    original_price="MIN +119 at DraftKings",
                    bettable_to_price="+105",
                )
                schedule.write_text(json.dumps({"candidates": [], "lineup_watchlist": [bad_entry]}))
                output = StringIO()

                with redirect_stdout(output):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 1)
                self.assertIn("original_price must be numeric", output.getvalue())
                self.assertIn("bettable_to_price must be numeric", output.getvalue())
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_successful_review_writes_latest_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                day = vig_review_gate_common.datetime.now(
                    vig_review_gate_common.ZoneInfo("America/Chicago")
                ).date().isoformat()
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                candidate = {
                    "event_id": "401816156",
                    "game": "Chicago White Sox at Toronto Blue Jays",
                    "side": "CWS",
                    "unit_size": 18,
                    "vig_approved": None,
                    "execution_mode": "manual",
                    "manual_bet_status": None,
                    "executed": False,
                }
                schedule_path.write_text(json.dumps({"candidates": [candidate], "lineup_watchlist": []}))

                def complete_review(*args, **kwargs):
                    updated = dict(candidate)
                    updated.update(
                        vig_approved=True,
                        vig_notes="All gates hold.",
                        manual_bet_status="awaiting_jerry",
                    )
                    schedule_path.write_text(
                        json.dumps({"candidates": [updated], "lineup_watchlist": []})
                    )
                    return vig_review_gate_common.subprocess.CompletedProcess(
                        args[0], 0, stdout="Vig review complete", stderr=""
                    )

                with patch.object(vig_review_gate_common.subprocess, "run", side_effect=complete_review):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                latest = (root / ".picks" / "latest-action.md").read_text()
                self.assertIn(f"{day}: MLB review complete", latest)
                self.assertIn("1 approved manual-only candidate", latest)
                self.assertIn("0 rejected", latest)
                self.assertIn("No bet placed or scheduled", latest)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_regular_review_prompt_is_manual_only(self):
        prompt = vig_review_gate_common.build_regular_review_prompt(
            "MLB", "2026-07-17", Path("/tmp/schedule.json"), [{"side": "ABC"}]
        )

        self.assertIn("manual_bet_status=awaiting_jerry", prompt)
        self.assertIn("no execution cron", prompt)
        self.assertIn("must never place or schedule a bet", prompt)

    def test_manual_candidate_validation_rejects_execution_state(self):
        candidate = {
            "side": "ABC",
            "vig_approved": True,
            "execution_mode": "automatic",
            "manual_bet_status": None,
            "executed": True,
            "execution_cron_id": "unsafe",
        }

        errors = vig_review_gate_common.manual_candidate_errors(candidate)

        self.assertIn("execution_mode must be manual", errors)
        self.assertIn("executed must be false", errors)
        self.assertTrue(any("execution_cron_id" in error for error in errors))

    def test_post_review_requires_targeted_watch_entry_to_finish(self):
        before = {
            "candidates": [],
            "lineup_watchlist": [self._watch_entry()],
        }
        after = {
            "candidates": [],
            "lineup_watchlist": [self._watch_entry()],
        }

        errors = vig_review_gate_common.validate_review_transition(before, after, [], ["watch-1"])

        self.assertIn("watchlist watch-1 did not reach promoted or passed", errors)

    def test_valid_manual_promotion_transition(self):
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        promoted_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            "execution_mode": "manual",
            "manual_bet_status": "awaiting_jerry",
            "executed": False,
        }
        promoted = self._watch_entry(
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
        after = {"candidates": [promoted_candidate], "lineup_watchlist": [promoted]}

        self.assertEqual(
            vig_review_gate_common.validate_review_transition(before, after, [], ["watch-1"]),
            [],
        )

    @staticmethod
    def _watch_entry(**overrides):
        item = {
            "id": "watch-1",
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


if __name__ == "__main__":
    unittest.main()
