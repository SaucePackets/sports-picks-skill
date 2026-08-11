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

EXECUTION_GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_execution_gate.py"
execution_gate_spec = importlib.util.spec_from_file_location(
    "mlb_execution_gate_for_review_test", EXECUTION_GATE_PATH
)
assert execution_gate_spec is not None
mlb_execution_gate = importlib.util.module_from_spec(execution_gate_spec)
assert execution_gate_spec.loader is not None
execution_gate_spec.loader.exec_module(mlb_execution_gate)


POLICY = {
    "min_conservative_edge": 0.05,
    "max_mlb_official_bets_per_day": 2,
    "starter_pending_promotions_enabled": False,
    "max_small_bets_per_day_during_probation": 1,
    "policy_version": "vig-mlb-policy-v1",
    "policy_effective_at": "2026-08-11T00:00:00Z",
}


def _contract(conservative_probability=0.60, current_ask=0.51, **overrides):
    """Full PR-1 probability contract for a routing-eligible MLB candidate."""
    fields = {
        "dk_fair_prob": 0.55,
        "raw_probability": 0.63,
        "uncertainty_haircut": 0.03,
        "conservative_probability": conservative_probability,
        "current_ask": current_ask,
        "projected_edge_at_current_ask": round(
            conservative_probability - current_ask, 4
        ),
        "model_version": "market-prior-v1",
    }
    fields.update(overrides)
    return fields


def _patched_policy():
    return patch.object(vig_review_gate_common, "load_mlb_policy", return_value=dict(POLICY))


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
            **_contract(current_ask=0.525),
        )

        with _patched_policy():
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

        self.assertEqual(len(errors), 1)
        self.assertIn(
            "candidate event_id:1|side:CWS has no strict numeric approved Polymarket ask",
            errors[0],
        )
        self.assertNotEqual(after["candidates"][0].get("execution_mode"), "standing_authorized")

    def test_normalize_new_mlb_approval_rejected_without_probability_contract(self):
        before = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "first_pitch_utc": "2026-07-19T18:15:00Z",
                    "polymarket_slug": "aec-mlb-cws-tor-2026-07-19",
                    "polymarket_ask": 0.51,
                    "vig_approved": None,
                }
            ]
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(vig_approved=True, vig_notes="Approved.")

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        candidate = after["candidates"][0]
        self.assertIs(candidate["vig_approved"], False)
        self.assertIn("missing/non-numeric probability contract fields", candidate["vig_notes"])
        self.assertNotEqual(candidate.get("execution_mode"), "standing_authorized")

    @staticmethod
    def _routing_candidate(event_id, side, ask, edge_prob, **overrides):
        candidate = {
            "event_id": event_id,
            "side": side,
            "first_pitch_utc": "2026-07-19T18:15:00Z",
            "polymarket_slug": f"aec-mlb-{side.lower()}-opp-2026-07-19",
            "polymarket_ask": ask,
            "vig_approved": None,
            "original_gate_results": {
                "starter_floor": True,
                "opposing_starter_shutdown_path": True,
                "bullpen_close_game_survival": True,
                "cold_fade_reset": True,
                "price_discipline": True,
                "real_winner_conviction": True,
            },
        }
        candidate.update(overrides)
        return candidate

    def _approve_all(self, before, edges):
        after = json.loads(json.dumps(before))
        for candidate, edge_prob in zip(after["candidates"], edges):
            candidate.update(
                vig_approved=True,
                vig_notes="All gates hold.",
                **_contract(
                    conservative_probability=edge_prob,
                    current_ask=candidate["polymarket_ask"],
                ),
            )
        return after

    def test_third_qualified_daily_candidate_rejected_by_rank_limit(self):
        before = {
            "candidates": [
                self._routing_candidate("1", "AAA", 0.50, 0.60),
                self._routing_candidate("2", "BBB", 0.50, 0.58),
                self._routing_candidate("3", "CCC", 0.50, 0.56),
            ]
        }
        after = self._approve_all(before, [0.60, 0.58, 0.56])

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        routed = [
            c["side"]
            for c in after["candidates"]
            if c.get("execution_mode") == "standing_authorized"
        ]
        self.assertEqual(routed, ["AAA", "BBB"])
        weakest = after["candidates"][2]
        self.assertIs(weakest["vig_approved"], False)
        self.assertIn("daily official-bet limit", weakest["vig_notes"])

    def test_daily_limit_counts_existing_approvals_as_consumed_slots(self):
        # Cap is 2. One candidate was already approved earlier today (edge
        # 0.06). Two stronger new approvals (0.10, 0.08) arrive in this
        # review: the existing approval consumes a slot, so only the strongest
        # new candidate may keep its approval — all three must NOT stay
        # approved.
        existing = self._routing_candidate("9", "OLD", 0.50, 0.56)
        existing["vig_approved"] = True
        existing["vig_notes"] = "Approved in an earlier review."
        existing.update(
            _contract(conservative_probability=0.56, current_ask=0.50)
        )
        before = {
            "candidates": [
                existing,
                self._routing_candidate("1", "AAA", 0.50, 0.60),
                self._routing_candidate("2", "BBB", 0.50, 0.58),
            ]
        }
        after = json.loads(json.dumps(before))
        for candidate, edge_prob in zip(after["candidates"][1:], [0.60, 0.58]):
            candidate.update(
                vig_approved=True,
                vig_notes="All gates hold.",
                **_contract(
                    conservative_probability=edge_prob,
                    current_ask=candidate["polymarket_ask"],
                ),
            )

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        approved_sides = [
            c["side"] for c in after["candidates"] if c.get("vig_approved") is True
        ]
        self.assertEqual(approved_sides, ["OLD", "AAA"])
        weakest = after["candidates"][2]
        self.assertIs(weakest["vig_approved"], False)
        self.assertIn("daily official-bet limit", weakest["vig_notes"])

    def test_small_bet_limit_counts_existing_small_approvals(self):
        # Probation small-tier cap is 1/day. An existing small approval
        # consumes the slot; a new small candidate, even with a stronger edge,
        # must be demoted.
        existing = self._routing_candidate(
            "9", "OLD", 0.50, 0.56, confidence="small", unit_size=9
        )
        existing["vig_approved"] = True
        existing.update(_contract(conservative_probability=0.56, current_ask=0.50))
        before = {
            "candidates": [
                existing,
                self._routing_candidate(
                    "1", "AAA", 0.50, 0.60, confidence="small", unit_size=9
                ),
            ]
        }
        after = json.loads(json.dumps(before))
        after["candidates"][1].update(
            vig_approved=True,
            vig_notes="All gates hold.",
            **_contract(conservative_probability=0.60, current_ask=0.50),
        )

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        self.assertIs(after["candidates"][0]["vig_approved"], True)
        newcomer = after["candidates"][1]
        self.assertIs(newcomer["vig_approved"], False)
        self.assertIn("probation small-bet daily limit", newcomer["vig_notes"])

    def test_daily_limit_keeps_highest_edge_not_first_listed(self):
        before = {
            "candidates": [
                self._routing_candidate("1", "AAA", 0.50, 0.56),
                self._routing_candidate("2", "BBB", 0.50, 0.60),
                self._routing_candidate("3", "CCC", 0.50, 0.58),
            ]
        }
        after = self._approve_all(before, [0.56, 0.60, 0.58])

        with _patched_policy():
            vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        routed = {
            c["side"]
            for c in after["candidates"]
            if c.get("execution_mode") == "standing_authorized"
        }
        self.assertEqual(routed, {"BBB", "CCC"})

    def test_edge_boundary_at_routing(self):
        # 0.049 live edge fails the 0.05 floor; 0.05+ passes.
        before = {
            "candidates": [
                self._routing_candidate("1", "AAA", 0.55, 0.60),
                self._routing_candidate("2", "BBB", 0.551, 0.60),
            ]
        }
        after = self._approve_all(before, [0.60, 0.60])

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        passing, failing = after["candidates"]
        self.assertEqual(passing["execution_mode"], "standing_authorized")
        self.assertIs(failing["vig_approved"], False)
        self.assertIn("below min_conservative_edge", failing["vig_notes"])

    def test_price_deterioration_since_morning_rejects_at_routing(self):
        # Stored projected_edge_at_current_ask claims 0.09, but the live
        # recomputed edge from conservative_probability - current_ask is 0.04.
        before = {"candidates": [self._routing_candidate("1", "AAA", 0.50, 0.60)]}
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            vig_approved=True,
            vig_notes="All gates hold.",
            **_contract(
                conservative_probability=0.60,
                current_ask=0.56,
                projected_edge_at_current_ask=0.09,
            ),
        )

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        candidate = after["candidates"][0]
        self.assertIs(candidate["vig_approved"], False)
        self.assertIn("below min_conservative_edge", candidate["vig_notes"])

    def test_second_small_bet_same_day_rejected_during_probation(self):
        before = {
            "candidates": [
                self._routing_candidate("1", "AAA", 0.50, 0.60, confidence="small", unit_size=9),
                self._routing_candidate("2", "BBB", 0.50, 0.58, confidence="small", unit_size=9),
            ]
        }
        after = self._approve_all(before, [0.60, 0.58])

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        smalls_routed = [
            c["side"]
            for c in after["candidates"]
            if c.get("execution_mode") == "standing_authorized"
        ]
        self.assertEqual(smalls_routed, ["AAA"])
        self.assertIn("probation small-bet daily limit", after["candidates"][1]["vig_notes"])

    def test_different_days_do_not_share_daily_limit(self):
        before = {
            "candidates": [
                self._routing_candidate("1", "AAA", 0.50, 0.60),
                self._routing_candidate("2", "BBB", 0.50, 0.58),
                self._routing_candidate(
                    "3", "CCC", 0.50, 0.56,
                    first_pitch_utc="2026-07-20T18:15:00Z",
                    polymarket_slug="aec-mlb-ccc-opp-2026-07-20",
                ),
            ]
        }
        after = self._approve_all(before, [0.60, 0.58, 0.56])

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        routed = [
            c["side"]
            for c in after["candidates"]
            if c.get("execution_mode") == "standing_authorized"
        ]
        self.assertEqual(sorted(routed), ["AAA", "BBB", "CCC"])

    def test_stale_stored_edge_cannot_override_live_arithmetic(self):
        # projected_edge_at_current_ask field is stale-high; live fields govern.
        before = {"candidates": [self._routing_candidate("1", "AAA", 0.50, 0.60)]}
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            vig_approved=True,
            vig_notes="All gates hold.",
            **_contract(conservative_probability=0.60, current_ask=0.53),
        )
        after["candidates"][0]["projected_edge_at_current_ask"] = 0.005  # stale low

        with _patched_policy():
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            after["candidates"][0]["execution_mode"], "standing_authorized"
        )

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
            **_contract(current_ask=0.525),
        )

        with _patched_policy():
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
        self.assertNotEqual(after["candidates"][1].get("vig_approved"), True)

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
            **_contract(current_ask=0.51),
        }
        promoted = self._watch_entry(
            status="promoted", promoted_candidate=dict(promoted_candidate)
        )
        after = {"candidates": [promoted_candidate], "lineup_watchlist": [promoted]}

        with _patched_policy():
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

    def test_lineup_recheck_prompt_fetches_schedule_mapped_snapshot(self):
        entry = self._watch_entry(
            id="sea-watch",
            event_id="401816229",
            game="Cincinnati Reds at Seattle Mariners",
        )
        snapshot = {
            "game_pk": 823110,
            "away_team": "Cincinnati Reds",
            "home_team": "Seattle Mariners",
            "player_count": 52,
            "away_batting_order": ["Away"] * 9,
            "home_batting_order": ["Home"] * 9,
        }

        with patch.object(
            vig_review_gate_common, "fetch_lineup_snapshot", return_value=snapshot
        ) as fetch:
            prompt = vig_review_gate_common.build_lineup_recheck_prompt(
                Path("/tmp/schedule.json"), [entry]
            )

        fetch.assert_called_once_with(entry)
        self.assertIn("MLB gamePk 823110", prompt)
        self.assertNotIn("401816229/feed/live", prompt)

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
                        **_contract(current_ask=0.51),
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

                with patch.object(vig_review_gate_common.subprocess, "run", side_effect=complete_review), _patched_policy():
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

    def test_successful_watchlist_review_ignores_exact_review_diff_payload(self):
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
                    "price": 123,
                    "bettable_to_price": 105,
                    "unit_size": 18,
                    "vig_approved": True,
                    "vig_notes": "All gates hold.",
                    "captured_polymarket_ask": 0.51,
                    "execution_mode": "manual",
                    "manual_bet_status": "awaiting_jerry",
                    "executed": False,
                    **_contract(current_ask=0.51),
                }
                promoted_entry = self._watch_entry(
                    side="MIN",
                    game="Minnesota Twins at Chicago Cubs",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat(),
                    status="promoted",
                    rechecked_at_utc="2026-07-19T17:00:00Z",
                    recheck_notes="Both lineups confirmed; matchup edge still holds.",
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
                    leaked = (
                        "┊ review diff\n*** " "Begin Patch\n"
                        "*** " "Update File: /home/clawdbot/private/schedule.json\n"
                        "+{\"vig_approved\": true}\n*** " "End Patch\n"
                    )
                    return vig_review_gate_common.subprocess.CompletedProcess(
                        args[0], 0, stdout=leaked, stderr=""
                    )

                output = StringIO()
                with (
                    patch.object(
                        vig_review_gate_common.subprocess, "run", side_effect=complete_review
                    ),
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    ),
                    _patched_policy(),
                    redirect_stdout(output),
                ):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                self.assertEqual(
                    output.getvalue(),
                    "MLB lineup recheck — APPROVED\n"
                    "Side: MIN\n"
                    "Supported price: +123\n"
                    "Bettable to: +105\n"
                    "Reason: Both lineups confirmed; matchup edge still holds.\n"
                    "Size: $18\n"
                    "Status: pending execution\n",
                )
                self.assertNotIn("review diff", output.getvalue())
                self.assertNotIn("Begin Patch", output.getvalue())
                self.assertNotIn("vig_approved", output.getvalue())
                self.assertNotIn("/home/", output.getvalue())
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_child_failure_is_concise_and_does_not_echo_child_output(self):
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
                    stdout="┊ review diff\n{\"secret\": true}",
                    stderr="/home/clawdbot/private/schedule.json",
                )
                output = StringIO()

                with (
                    patch.object(vig_review_gate_common.subprocess, "run", return_value=failed),
                    redirect_stdout(output),
                ):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 7)
                self.assertEqual(
                    output.getvalue(),
                    "MLB review gate ERROR: child reviewer exited 7; reviewed state was not "
                    "accepted. Retry the job and inspect Vig session logs.\n",
                )
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_validated_report_strips_review_diff_from_persisted_reason(self):
        candidate = {
            "event_id": "1",
            "side": "CWS",
            "vig_approved": False,
            "vig_notes": (
                "Price moved beyond the limit. ┊ review diff *** "
                "Update File: /home/clawdbot/private/schedule.json "
                "{\"vig_approved\": false}"
            ),
        }

        report = vig_review_gate_common.build_validated_review_report(
            {"candidates": [candidate], "lineup_watchlist": []},
            "MLB",
            [vig_review_gate_common.candidate_identity(candidate)],
            [],
            True,
        )

        self.assertIn("Price moved beyond the limit.", report)
        self.assertNotIn("review diff", report)
        self.assertNotIn("Update File", report)
        self.assertNotIn("vig_approved", report)
        self.assertNotIn("/home/", report)

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

    def test_review_prompt_states_zero_fee_and_no_phantom_fee(self):
        for sport, kwargs in (
            ("MLB", {"mlb_standing_authorized": True}),
            ("SOCCER", {}),
        ):
            with _patched_policy():
                prompt = vig_review_gate_common.build_regular_review_prompt(
                    sport,
                    "2026-08-09",
                    Path("/tmp/schedule.json"),
                    [{"side": "ABC"}],
                    **kwargs,
                )
            # The ceiling is the single guardrail: judge the real cost to buy
            # against max_polymarket_price = probability - min_conservative_edge,
            # no fee math.
            self.assertIn("max_polymarket_price = ", prompt)
            self.assertIn("- 0.050", prompt)
            self.assertIn("cost to buy", prompt)
            self.assertIn("ZERO", prompt)
            # No phantom-fee SUBTRACTION (the bug that rejected the 2026-08-09
            # Brewers pick); naming 0.024 as forbidden is fine, subtracting is not.
            self.assertNotIn("- 0.024", prompt)
            self.assertNotIn("net_edge = win_probability - polymarket_ask - 0.024", prompt)

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

    def test_promoted_watch_entry_requires_approval_and_decisive_reason(self):
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        promoted_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            "sport": "MLB",
            "market_type": "moneyline",
            "price": -120,
            "vig_approved": False,
            "vig_notes": "",
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

        errors = vig_review_gate_common.validate_review_transition(
            before, after, [], ["watch-1"], mlb_standing_authorized=True
        )

        self.assertIn("watchlist watch-1 promoted candidate must be vig_approved", errors)
        self.assertIn("watchlist watch-1 promoted candidate has no decisive reason", errors)

    def test_passed_watch_entry_does_not_report_original_price_as_supported(self):
        passed = self._watch_entry(
            status="passed",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck_notes="Supported-market price refresh was unavailable.",
        )

        report = vig_review_gate_common.build_validated_review_report(
            {"candidates": [], "lineup_watchlist": [passed]},
            "MLB",
            [],
            ["watch-1"],
            True,
        )

        self.assertIn("Supported price: not recorded", report)
        self.assertNotIn("Supported price: -125", report)

    def test_valid_standing_authorized_promotion_transition(self):
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        promoted_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            "sport": "MLB",
            "market_type": "moneyline",
            "price": -120,
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


def test_entry_slug_prefers_stamped_then_thesis():
    import vig_review_gate_common as g
    assert g._entry_slug({"polymarket_slug": "aec-mlb-nyy-cws-2026-07-27"}) == "aec-mlb-nyy-cws-2026-07-27"
    assert g._entry_slug({"thesis": "priced aec-mlb-bos-ath-2026-07-27 at 0.62"}) == "aec-mlb-bos-ath-2026-07-27"
    assert g._entry_slug({"thesis": "no slug here"}) is None


def test_price_context_uses_deterministic_fetch(monkeypatch):
    import vig_review_gate_common as g
    monkeypatch.setattr(g, "fetch_market_price", lambda slug: {
        "slug": slug, "open": True, "reason": "open",
        "long_ask": "0.5750", "no_ask": "0.4300", "book_state": "reliable",
    })
    entries = [{"id": "e1", "polymarket_slug": "aec-mlb-nyy-cws-2026-07-27", "thesis": ""}]
    ctx = g._price_context(entries)
    assert "Deterministic Polymarket US prices" in ctx
    assert "long/YES ask=0.5750" in ctx and "NO-side ask=0.4300" in ctx
    assert "DO NOT web-search" in ctx


def test_price_context_degrades_without_slug_or_fetch(monkeypatch):
    import vig_review_gate_common as g
    monkeypatch.setattr(g, "fetch_market_price", lambda slug: None)
    ctx = g._price_context([{"id": "e2", "polymarket_slug": "aec-mlb-x-y-2026-07-27", "thesis": ""}])
    assert "current price unavailable" in ctx  # never raises; recheck carries ceiling to poller


class MlbPolicyBoundaryTests(unittest.TestCase):
    """PR-1 acceptance boundaries: edge floor, daily rank limit, stale edge."""

    def _approval_pair(self, contract_kwargs, slug="aec-mlb-cws-tor-2026-07-19"):
        before = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "first_pitch_utc": "2026-07-19T18:15:00Z",
                    "polymarket_slug": slug,
                    "polymarket_ask": contract_kwargs.get("current_ask", 0.51),
                    "unit_size": 18,
                    "vig_approved": None,
                }
            ]
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            vig_approved=True,
            vig_notes="All gates hold.",
            **_contract(**contract_kwargs),
        )
        return before, after

    def _normalize(self, before, after):
        with _patched_policy():
            return vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )

    def test_edge_just_below_floor_rejected(self):
        # 0.049 conservative edge must fail when the floor is 0.05.
        before, after = self._approval_pair(
            {"conservative_probability": 0.599, "current_ask": 0.55}
        )
        errors = self._normalize(before, after)
        self.assertEqual(errors, [])
        candidate = after["candidates"][0]
        self.assertIs(candidate["vig_approved"], False)
        self.assertIn("below min_conservative_edge", candidate["vig_notes"])
        self.assertNotEqual(candidate.get("execution_mode"), "standing_authorized")

    def test_edge_exactly_at_floor_routes(self):
        # 0.050 conservative edge passes the floor.
        before, after = self._approval_pair(
            {"conservative_probability": 0.60, "current_ask": 0.55}
        )
        errors = self._normalize(before, after)
        self.assertEqual(errors, [])
        candidate = after["candidates"][0]
        self.assertIs(candidate["vig_approved"], True)
        self.assertEqual(candidate.get("execution_mode"), "standing_authorized")

    def test_third_qualified_candidate_cut_by_daily_limit(self):
        def cand(event_id, slug, edge):
            return {
                "event_id": event_id,
                "side": f"T{event_id}",
                "first_pitch_utc": "2026-07-19T18:15:00Z",
                "polymarket_slug": slug,
                "polymarket_ask": 0.50,
                "unit_size": 18,
                "vig_approved": None,
            }

        before = {
            "candidates": [
                cand("1", "aec-mlb-a-b-2026-07-19", 0.08),
                cand("2", "aec-mlb-c-d-2026-07-19", 0.07),
                cand("3", "aec-mlb-e-f-2026-07-19", 0.06),
            ]
        }
        after = json.loads(json.dumps(before))
        for item, edge in zip(after["candidates"], (0.08, 0.07, 0.06)):
            item.update(
                vig_approved=True,
                vig_notes="All gates hold.",
                **_contract(
                    conservative_probability=0.50 + edge,
                    current_ask=0.50,
                ),
            )
        errors = self._normalize(before, after)
        self.assertEqual(errors, [])
        routed = [
            c for c in after["candidates"]
            if c.get("execution_mode") == "standing_authorized"
        ]
        self.assertEqual(len(routed), 2)
        weakest = after["candidates"][2]
        self.assertIs(weakest["vig_approved"], False)
        self.assertIn("daily official-bet limit", weakest["vig_notes"])

    def test_stale_stored_edge_cannot_override_live_arithmetic(self):
        # stored projected edge says 0.08, but live recomputation from the
        # probability fields yields 0.02 < floor: the candidate must be cut.
        before, after = self._approval_pair(
            {
                "conservative_probability": 0.57,
                "current_ask": 0.55,
                "projected_edge_at_current_ask": 0.08,
            }
        )
        errors = self._normalize(before, after)
        self.assertEqual(errors, [])
        candidate = after["candidates"][0]
        self.assertIs(candidate["vig_approved"], False)
        self.assertIn("below min_conservative_edge", candidate["vig_notes"])
