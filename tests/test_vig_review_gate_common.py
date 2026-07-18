import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
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

    def test_successful_lineup_recheck_ignores_child_stdout_and_reports_validated_state(self):
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
                first_pitch = datetime.now(timezone.utc) + timedelta(minutes=75)
                before_entry = self._watch_entry(
                    side="MIN",
                    game="Minnesota Twins at Chicago Cubs",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat(),
                )
                schedule_path.write_text(
                    json.dumps({"candidates": [], "lineup_watchlist": [before_entry]})
                )

                promoted_candidate = {
                    "watchlist_id": "watch-1",
                    "side": "MIN",
                    "price": "+123",
                    "bettable_to_price": 105,
                    "unit_size": 18,
                    "vig_approved": True,
                    "vig_notes": "All gates hold.",
                    "execution_mode": "manual",
                    "manual_bet_status": "awaiting_jerry",
                    "executed": False,
                }
                promoted_entry = self._watch_entry(
                    side="MIN",
                    game="Minnesota Twins at Chicago Cubs",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat(),
                    status="promoted",
                    rechecked_at_utc="2026-07-18T16:20:23Z",
                    recheck_notes="Both lineups are confirmed; no late injury changes the edge.",
                    recheck={
                        "lineups_confirmed": True,
                        "key_injuries_refreshed": True,
                        "price_refreshed": True,
                        "all_original_gates_hold": True,
                    },
                    promoted_candidate=promoted_candidate,
                )

                def complete_review(*args, **kwargs):
                    schedule_path.write_text(
                        json.dumps(
                            {
                                "candidates": [promoted_candidate],
                                "lineup_watchlist": [promoted_entry],
                            }
                        )
                    )
                    noise = (
                        "*** " "Begin Patch\n*** Update File: /home/clawdbot/private/schedule.json\n"
                        "+{\"vig_approved\": true}\n*** " "End Patch\n"
                        "/home/clawdbot/private/schedule.json\nMLB lineup recheck — APPROVED\n"
                    )
                    return vig_review_gate_common.subprocess.CompletedProcess(
                        args[0], 0, stdout=noise, stderr=""
                    )

                output = StringIO()
                with patch.object(vig_review_gate_common.subprocess, "run", side_effect=complete_review):
                    with redirect_stdout(output):
                        status = vig_review_gate_common.run_gate("MLB")

                report = output.getvalue()
                self.assertEqual(status, 0)
                self.assertEqual(
                    report,
                    "MLB lineup recheck — APPROVED\n"
                    "Pick: MIN\n"
                    "Current price: +123\n"
                    "Bettable to: +105\n"
                    "Reason: Both lineups are confirmed; no late injury changes the edge.\n"
                    "Size/status: $18; awaiting Jerry\n"
                    "Manual placement required; no bet submitted.\n",
                )
                self.assertNotIn("Begin Patch", report)
                self.assertNotIn("/home/", report)
                self.assertNotIn("vig_approved", report)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_regular_review_ignores_child_stdout_and_reports_targeted_decisions(self):
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
                pending = [
                    {
                        "event_id": "1",
                        "side": "CWS",
                        "unit_size": 18,
                        "vig_approved": None,
                        "execution_mode": "manual",
                        "executed": False,
                    },
                    {
                        "event_id": "2",
                        "side": "TOR",
                        "unit_size": 12,
                        "vig_approved": None,
                        "execution_mode": "manual",
                        "executed": False,
                    },
                ]
                schedule_path.write_text(
                    json.dumps({"candidates": pending, "lineup_watchlist": []})
                )

                def complete_review(*args, **kwargs):
                    approved = {
                        **pending[0],
                        "vig_approved": True,
                        "vig_notes": "Rotation edge holds.",
                        "execution_mode": "manual",
                        "manual_bet_status": "awaiting_jerry",
                        "executed": False,
                    }
                    rejected = {
                        **pending[1],
                        "vig_approved": False,
                        "vig_notes": "Price moved beyond the limit.",
                    }
                    schedule_path.write_text(
                        json.dumps({"candidates": [approved, rejected], "lineup_watchlist": []})
                    )
                    noise = (
                        "tool result: /home/clawdbot/private/schedule.json\n"
                        "Card review\n{\"candidates\": [{\"vig_approved\": true}]}\n"
                    )
                    return vig_review_gate_common.subprocess.CompletedProcess(
                        args[0], 0, stdout=noise, stderr=""
                    )

                output = StringIO()
                with patch.object(vig_review_gate_common.subprocess, "run", side_effect=complete_review):
                    with redirect_stdout(output):
                        status = vig_review_gate_common.run_gate("MLB")

                report = output.getvalue()
                self.assertEqual(status, 0)
                self.assertEqual(
                    report,
                    "MLB card review — 1 approved, 1 rejected\n"
                    "- APPROVED CWS: Rotation edge holds. Size/status: $18; awaiting Jerry\n"
                    "- REJECTED TOR: Price moved beyond the limit.\n"
                    "Manual placement required; no bet submitted.\n",
                )
                self.assertNotIn("/home/", report)
                self.assertNotIn("candidates", report)
                self.assertNotIn("vig_approved", report)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_child_failure_is_concise_and_does_not_echo_tool_logs(self):
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
                schedule_path.write_text(
                    json.dumps(
                        {
                            "candidates": [{"event_id": "1", "side": "CWS"}],
                            "lineup_watchlist": [],
                        }
                    )
                )
                failed = vig_review_gate_common.subprocess.CompletedProcess(
                    ["hermes"],
                    7,
                    stdout='{"secret": "raw child JSON"}',
                    stderr="patch diff /home/clawdbot/private/schedule.json",
                )
                output = StringIO()

                with patch.object(vig_review_gate_common.subprocess, "run", return_value=failed):
                    with redirect_stdout(output):
                        status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 7)
                self.assertEqual(
                    output.getvalue(),
                    "MLB review gate ERROR: child reviewer exited 7; reviewed state was not accepted. "
                    "Retry the job and inspect Vig session logs.\n",
                )
                self.assertNotIn("raw child", output.getvalue())
                self.assertNotIn("/home/", output.getvalue())
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_validated_report_reasons_strip_artifacts_and_are_bounded(self):
        candidate = {
            "event_id": "1",
            "side": "CWS",
            "vig_approved": False,
            "vig_notes": (
                "Price moved beyond the limit. /home/clawdbot/private/schedule.json "
                "*** " "Begin Patch {\"vig_approved\": false} "
                + ("extra noise " * 80)
            ),
        }

        report = vig_review_gate_common.build_validated_review_report(
            {"candidates": [candidate], "lineup_watchlist": []},
            "MLB",
            [vig_review_gate_common.candidate_identity(candidate)],
            [],
        )

        self.assertIn("Price moved beyond the limit.", report)
        self.assertNotIn("/home/", report)
        self.assertNotIn("Begin Patch", report)
        self.assertNotIn("vig_approved", report)
        self.assertLessEqual(len(report.splitlines()[1]), 270)

    def test_report_price_rejects_unstructured_child_text(self):
        self.assertEqual(
            vig_review_gate_common._american_price(
                "+123 /home/clawdbot/private/schedule.json {\"tool\": true}"
            ),
            "not recorded",
        )

    def test_report_pick_label_strips_artifacts_and_is_bounded(self):
        candidate = {
            "event_id": "1",
            "side": "/home/clawdbot/private/schedule.json\n*** " "Begin Patch " + ("X" * 200),
            "vig_approved": False,
            "vig_notes": "Price moved.",
        }

        report = vig_review_gate_common.build_validated_review_report(
            {"candidates": [candidate], "lineup_watchlist": []},
            "MLB",
            [vig_review_gate_common.candidate_identity(candidate)],
            [],
        )

        self.assertNotIn("/home/", report)
        self.assertNotIn("Begin Patch", report)
        self.assertLessEqual(len(report.splitlines()[1]), 120)

    def test_child_timeout_is_a_concise_actionable_error(self):
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
                schedule_path.write_text(
                    json.dumps(
                        {
                            "candidates": [{"event_id": "1", "side": "CWS"}],
                            "lineup_watchlist": [],
                        }
                    )
                )
                output = StringIO()

                with patch.object(
                    vig_review_gate_common.subprocess,
                    "run",
                    side_effect=vig_review_gate_common.subprocess.TimeoutExpired("hermes", 1800),
                ):
                    with redirect_stdout(output):
                        status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 1)
                self.assertEqual(
                    output.getvalue(),
                    "MLB review gate ERROR: child reviewer timed out; reviewed state was not "
                    "accepted. Retry the job and inspect Vig session logs.\n",
                )
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

    def test_promotion_transition_requires_approved_candidate_with_notes(self):
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        promoted_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            "vig_approved": None,
            "vig_notes": "",
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

        errors = vig_review_gate_common.validate_review_transition(
            before, after, [], ["watch-1"]
        )

        self.assertIn("watchlist watch-1 promoted candidate must be vig_approved", errors)
        self.assertIn("watchlist watch-1 promoted candidate has empty vig_notes", errors)

    def test_transition_rejects_unexpected_after_only_candidate(self):
        before = {"candidates": [], "lineup_watchlist": []}
        injected = {
            "event_id": "injected",
            "side": "ABC",
            "vig_approved": True,
            "vig_notes": "Injected row.",
            "execution_mode": "automatic",
            "executed": True,
        }
        after = {"candidates": [injected], "lineup_watchlist": []}

        errors = vig_review_gate_common.validate_review_transition(before, after, [], [])

        self.assertIn(
            "unexpected candidate event_id:injected|side:ABC added during review",
            errors,
        )

    def test_transition_rejects_duplicate_candidate_identities(self):
        before_candidate = {"event_id": "1", "side": "CWS", "vig_approved": None}
        approved = {
            **before_candidate,
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            "execution_mode": "manual",
            "manual_bet_status": "awaiting_jerry",
            "executed": False,
        }
        injected = {**approved, "vig_notes": "Hidden duplicate."}
        before = {"candidates": [before_candidate], "lineup_watchlist": []}
        after = {"candidates": [injected, approved], "lineup_watchlist": []}
        identity = vig_review_gate_common.candidate_identity(before_candidate)

        errors = vig_review_gate_common.validate_review_transition(
            before, after, [identity], []
        )

        self.assertIn(f"candidate identity {identity} is duplicated after review", errors)

    def test_transition_rejects_duplicate_candidate_identities_before_review(self):
        first = {"event_id": "1", "side": "CWS", "unit_size": 10, "vig_approved": None}
        second = {"event_id": "1", "side": "CWS", "unit_size": 20, "vig_approved": None}
        identity = vig_review_gate_common.candidate_identity(first)
        approved = {
            **second,
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            "execution_mode": "manual",
            "manual_bet_status": "awaiting_jerry",
            "executed": False,
        }
        before = {"candidates": [first, second], "lineup_watchlist": []}
        after = {"candidates": [approved], "lineup_watchlist": []}

        errors = vig_review_gate_common.validate_review_transition(
            before, after, [identity, identity], []
        )

        self.assertIn(f"candidate identity {identity} is duplicated before review", errors)

    def test_rejected_candidate_cannot_gain_execution_state(self):
        before_candidate = {
            "event_id": "1",
            "side": "CWS",
            "vig_approved": None,
            "execution_mode": "manual",
            "executed": False,
        }
        rejected = {
            **before_candidate,
            "vig_approved": False,
            "vig_notes": "Price moved.",
            "execution_mode": "automatic",
            "executed": True,
            "execution_cron_id": "unsafe",
        }
        before = {"candidates": [before_candidate], "lineup_watchlist": []}
        after = {"candidates": [rejected], "lineup_watchlist": []}
        identity = vig_review_gate_common.candidate_identity(before_candidate)

        errors = vig_review_gate_common.validate_review_transition(
            before, after, [identity], []
        )

        self.assertIn(f"candidate {identity}: execution_mode must remain manual", errors)
        self.assertIn(f"candidate {identity}: executed must be false", errors)
        self.assertTrue(any("execution_cron_id" in error for error in errors))

    def test_transition_rejects_after_only_watchlist_entry(self):
        before = {"candidates": [], "lineup_watchlist": []}
        after = {"candidates": [], "lineup_watchlist": [self._watch_entry(id="injected")]}

        errors = vig_review_gate_common.validate_review_transition(before, after, [], [])

        self.assertIn("unexpected watchlist injected added during review", errors)

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
