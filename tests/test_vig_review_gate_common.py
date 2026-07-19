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

EXECUTION_GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_execution_gate.py"
execution_gate_spec = importlib.util.spec_from_file_location(
    "mlb_execution_gate_for_review_test", EXECUTION_GATE_PATH
)
assert execution_gate_spec is not None
mlb_execution_gate = importlib.util.module_from_spec(execution_gate_spec)
assert execution_gate_spec.loader is not None
execution_gate_spec.loader.exec_module(mlb_execution_gate)


class VigReviewGateCommonTests(unittest.TestCase):
    def test_normalize_new_mlb_approval_repairs_manual_child_state_for_execution_gate(self):
        now = datetime(2026, 7, 19, 17, 0, tzinfo=timezone.utc)
        before = {
            "date": "2026-07-19",
            "candidates": [
                {
                    "event_id": "401816156",
                    "side": "CWS",
                    "first_pitch_utc": "2026-07-19T18:15:00Z",
                    "polymarket_slug": "aec-mlb-cws-tor-2026-07-19",
                    "polymarket_ask": 0.525,
                    "unit_size": 18,
                    "vig_approved": None,
                }
            ],
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            vig_approved=True,
            vig_notes="All gates hold.",
            execution_mode="manual",
            manual_bet_status="awaiting_jerry",
            execution_status="pending_manual_fill",
            executed=False,
        )

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        candidate = after["candidates"][0]
        self.assertEqual(after["sport"], "MLB")
        self.assertEqual(after["market_type"], "moneyline")
        self.assertEqual(candidate["sport"], "MLB")
        self.assertEqual(candidate["market_type"], "moneyline")
        self.assertEqual(candidate["execution_mode"], "standing_authorized")
        self.assertEqual(candidate["execution_status"], "pending")
        self.assertEqual(candidate["max_polymarket_price"], 0.525)
        self.assertIs(candidate["executed"], False)
        self.assertNotIn("manual_bet_status", candidate)
        self.assertEqual(
            mlb_execution_gate.eligible_candidates(after, now),
            [candidate],
        )

    def test_normalize_new_mlb_approval_fails_closed_without_numeric_ask(self):
        before = {"candidates": [{"event_id": "1", "side": "CWS", "vig_approved": None}]}
        after = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "vig_approved": True,
                    "vig_notes": "Approved.",
                    "polymarket_ask": "0.525",
                }
            ]
        }

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(
            errors,
            ["candidate event_id:1|side:CWS has no strict numeric approved Polymarket ask"],
        )
        self.assertNotEqual(after["candidates"][0].get("execution_mode"), "standing_authorized")

    def test_normalize_uses_original_captured_ask_when_child_mutates_generic_ask(self):
        before = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "vig_approved": None,
                    "polymarket_ask": 0.525,
                }
            ]
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            vig_approved=True,
            vig_notes="Approved.",
            polymarket_ask=0.99,
        )

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(after["candidates"][0]["max_polymarket_price"], 0.525)

    def test_normalize_rejects_injected_approved_candidate(self):
        before = {
            "candidates": [
                {"event_id": "1", "side": "CWS", "vig_approved": None, "polymarket_ask": 0.525}
            ],
            "lineup_watchlist": [],
        }
        after = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "vig_approved": False,
                    "vig_notes": "Rejected.",
                },
                {
                    "event_id": "2",
                    "side": "NYY",
                    "vig_approved": True,
                    "vig_notes": "Approved.",
                    "polymarket_ask": 0.99,
                },
            ],
            "lineup_watchlist": [],
        }

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(
            errors,
            ["candidate event_id:2|side:NYY was not a targeted candidate or watchlist promotion"],
        )
        self.assertNotEqual(after["candidates"][1].get("execution_mode"), "standing_authorized")

    def test_normalize_rejects_duplicate_target_identity(self):
        candidate = {
            "polymarket_slug": "aec-mlb-cws-tor-2026-07-19",
            "side": "CWS",
            "vig_approved": None,
            "polymarket_ask": 0.525,
        }
        before = {"candidates": [candidate], "lineup_watchlist": []}
        approved = dict(candidate, vig_approved=True, vig_notes="Approved.")
        after = {"candidates": [dict(candidate), approved], "lineup_watchlist": []}

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(
            errors,
            [
                "candidate polymarket_slug:aec-mlb-cws-tor-2026-07-19|side:CWS "
                "appears more than once after review"
            ],
        )
        self.assertNotEqual(approved.get("execution_mode"), "standing_authorized")

    def test_normalize_valid_watchlist_promotion_uses_captured_ask(self):
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        promoted_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            "polymarket_ask": 0.51,
        }
        promoted = self._watch_entry(
            status="promoted", promoted_candidate=dict(promoted_candidate)
        )
        after = {"candidates": [promoted_candidate], "lineup_watchlist": [promoted]}

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(promoted_candidate["max_polymarket_price"], 0.51)
        self.assertEqual(promoted["promoted_candidate"], promoted_candidate)

    def test_normalize_soccer_approval_preserves_manual_only_state(self):
        before = {"candidates": [{"event_id": "1", "side": "USA", "vig_approved": None}]}
        after = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "USA",
                    "vig_approved": True,
                    "vig_notes": "Approved.",
                    "execution_mode": "manual",
                    "manual_bet_status": "awaiting_jerry",
                    "executed": False,
                }
            ]
        }

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "intl-soccer", mlb_standing_authorized=False
        )

        self.assertEqual(errors, [])
        self.assertEqual(after["candidates"][0]["execution_mode"], "manual")
        self.assertEqual(after["candidates"][0]["manual_bet_status"], "awaiting_jerry")
        self.assertNotIn("sport", after)
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
                    "sport": "MLB",
                    "market_type": "moneyline",
                    "unit_size": 18,
                    "polymarket_ask": 0.51,
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
                        execution_mode="manual",
                        execution_status="pending_manual_fill",
                        manual_bet_status="awaiting_jerry",
                    )
                    schedule_path.write_text(
                        json.dumps(
                            {
                                "candidates": [updated],
                                "lineup_watchlist": [],
                                "approved_exposure": 18,
                                "daily_cap": 110,
                            }
                        )
                    )
                    return vig_review_gate_common.subprocess.CompletedProcess(
                        args[0], 0, stdout="Vig review complete", stderr=""
                    )

                with patch.object(vig_review_gate_common.subprocess, "run", side_effect=complete_review):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                reviewed = json.loads(schedule_path.read_text())
                self.assertEqual(reviewed["sport"], "MLB")
                self.assertEqual(reviewed["market_type"], "moneyline")
                self.assertEqual(
                    reviewed["candidates"][0]["execution_mode"], "standing_authorized"
                )
                self.assertEqual(reviewed["candidates"][0]["execution_status"], "pending")
                self.assertEqual(reviewed["candidates"][0]["max_polymarket_price"], 0.51)
                self.assertNotIn("manual_bet_status", reviewed["candidates"][0])
                latest = (root / ".picks" / "latest-action.md").read_text()
                self.assertIn(f"{day}: MLB review complete", latest)
                self.assertIn("1 approved standing-authorized candidate", latest)
                self.assertIn("1 approved", latest)
                self.assertIn("0 rejected", latest)
                self.assertIn("Approved exposure $18 / $110", latest)
                self.assertIn("Review gate placed no bet", latest)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_latest_action_failure_does_not_persist_execution_pending_routing(self):
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
                    "side": "CWS",
                    "unit_size": 18,
                    "polymarket_ask": 0.51,
                    "vig_approved": None,
                    "executed": False,
                }
                schedule_path.write_text(
                    json.dumps({"candidates": [candidate], "lineup_watchlist": []})
                )

                def complete_review(*args, **kwargs):
                    updated = dict(candidate)
                    updated.update(
                        vig_approved=True,
                        vig_notes="All gates hold.",
                        execution_mode="manual",
                        execution_status="pending_manual_fill",
                        manual_bet_status="awaiting_jerry",
                    )
                    schedule_path.write_text(
                        json.dumps({"candidates": [updated], "lineup_watchlist": []})
                    )
                    return vig_review_gate_common.subprocess.CompletedProcess(
                        args[0], 0, stdout="Vig review complete", stderr=""
                    )

                with (
                    patch.object(
                        vig_review_gate_common.subprocess, "run", side_effect=complete_review
                    ),
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    ),
                    patch.object(
                        vig_review_gate_common,
                        "write_latest_action",
                        side_effect=OSError("disk full"),
                    ),
                ):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 1)
                persisted = json.loads(schedule_path.read_text())
                self.assertEqual(persisted["candidates"][0]["execution_mode"], "manual")
                self.assertEqual(
                    persisted["candidates"][0]["execution_status"], "pending_manual_fill"
                )
                self.assertNotIn("max_polymarket_price", persisted["candidates"][0])
                self.assertNotIn("sport", persisted)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_mlb_review_prompt_routes_approved_candidate_to_execution_poller(self):
        prompt = vig_review_gate_common.build_regular_review_prompt(
            "MLB",
            "2026-07-17",
            Path("/tmp/schedule.json"),
            [{"side": "ABC"}],
            mlb_standing_authorized=True,
        )

        self.assertIn("execution_mode=standing_authorized", prompt)
        self.assertIn("execution_status=pending", prompt)
        self.assertIn("recurring MLB execution poller", prompt)
        self.assertNotIn("awaiting_jerry", prompt)

    def test_soccer_review_prompt_remains_manual_only(self):
        prompt = vig_review_gate_common.build_regular_review_prompt(
            "SOCCER", "2026-07-17", Path("/tmp/schedule.json"), [{"side": "ABC"}]
        )

        self.assertIn("manual_bet_status=awaiting_jerry", prompt)
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

    def test_mlb_candidate_validation_requires_standing_authorized_pending_state(self):
        candidate = {
            "side": "ABC",
            "vig_approved": True,
            "execution_mode": "manual",
            "manual_bet_status": "awaiting_jerry",
            "executed": False,
        }

        errors = vig_review_gate_common.approved_candidate_errors(candidate, "MLB", True)

        self.assertIn("execution_mode must be standing_authorized", errors)
        self.assertIn("execution_status must be pending", errors)
        self.assertIn("manual_bet_status must not be awaiting_jerry", errors)
        self.assertIn("max_polymarket_price must be between 0 and 1", errors)
        self.assertIn("sport must be MLB", errors)
        self.assertIn("market_type must be moneyline", errors)

    def test_mlb_candidate_validation_rejects_one_shot_execution_artifacts(self):
        candidate = {
            "side": "ABC",
            "sport": "MLB",
            "market_type": "moneyline",
            "vig_approved": True,
            "execution_mode": "standing_authorized",
            "execution_status": "pending",
            "max_polymarket_price": 0.51,
            "executed": False,
            "execution_cron_id": "unsafe",
            "execution_cron_fire_utc": "2026-07-19T17:00:00Z",
            "approval_token": "unsafe",
        }

        errors = vig_review_gate_common.approved_candidate_errors(candidate, "MLB", True)

        self.assertTrue(any("execution_cron_id" in error for error in errors))
        self.assertTrue(any("execution_cron_fire_utc" in error for error in errors))
        self.assertTrue(any("approval_token" in error for error in errors))

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

    def test_valid_standing_authorized_promotion_transition(self):
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        promoted_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            "sport": "MLB",
            "market_type": "moneyline",
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            "execution_mode": "standing_authorized",
            "execution_status": "pending",
            "max_polymarket_price": 0.51,
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
            vig_review_gate_common.validate_review_transition(
                before, after, [], ["watch-1"], mlb_standing_authorized=True
            ),
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
