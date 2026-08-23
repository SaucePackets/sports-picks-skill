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

from mlb_baseball_evidence import valid_baseball_evidence, valid_execution_checks
from mlb_probability_model import valid_probability_components

EXECUTION_GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_execution_gate.py"
execution_gate_spec = importlib.util.spec_from_file_location(
    "mlb_execution_gate_for_review_test", EXECUTION_GATE_PATH
)
assert execution_gate_spec is not None
mlb_execution_gate = importlib.util.module_from_spec(execution_gate_spec)
assert execution_gate_spec.loader is not None
execution_gate_spec.loader.exec_module(mlb_execution_gate)


PROBABILITY_TRAIL = {
    "dk_fair_prob": 0.55,
    "raw_probability": 0.57,
    "uncertainty_haircut": 0.03,
    "conservative_probability": 0.54,
    "current_ask": 0.48,
    "projected_edge_at_current_ask": 0.06,
    "model_version": "market-only-fallback-v1",
    "baseball_evidence": valid_baseball_evidence(),
    "execution_checks": valid_execution_checks(supported_price=0.48),
    "probability_components": valid_probability_components(),
}
# The executable ceiling normalization stamps on each routed candidate:
# conservative_probability - min_conservative_edge (0.54 - 0.05).
POLICY_CEILING = round(0.54 - 0.05, 6)

# Phase 4 refresh-contract blocks for a standing-authorized watchlist
# promotion: the morning components on the entry, the refreshed components in
# the recheck (unchanged here), and the material-change accounting.
PROBABILITY_COMPONENTS = {
    field: PROBABILITY_TRAIL[field]
    for field in (
        "dk_fair_prob",
        "raw_probability",
        "uncertainty_haircut",
        "conservative_probability",
        "current_ask",
        "projected_edge_at_current_ask",
        "model_version",
    )
}
def consistent_probability_overrides(conservative, ask):
    """Trail overrides that keep raw - haircut == conservative (Phase 3
    identity) with a matching probability_components block."""
    raw = round(conservative + 0.03, 6)
    return {
        "raw_probability": raw,
        "uncertainty_haircut": 0.03,
        "conservative_probability": conservative,
        "current_ask": ask,
        "projected_edge_at_current_ask": round(conservative - ask, 6),
        "probability_components": valid_probability_components(
            adjustments=[
                {
                    "component": "starter_run_prevention",
                    "delta": round(raw - 0.55, 6),
                    "evidence": "Starter FIP advantage over the season sample",
                }
            ]
        ),
    }


REFRESHED_RECHECK = {
    "lineups_confirmed": True,
    "key_injuries_refreshed": True,
    "price_refreshed": True,
    "all_original_gates_hold": True,
    "probability": dict(PROBABILITY_COMPONENTS),
    "material_changes": [],
}

_POLICY_STATE = None


class VigReviewGateCommonTests(unittest.TestCase):
    def setUp(self):
        # Point the shared policy loader at a temp state dir with the PR 1
        # policy block so edge-floor and daily-cap rails are deterministic.
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        (state / "risk_limits.json").write_text(json.dumps({
            "mlb_selection_policy": {
                "schema": "vig-mlb-selection-policy-v1",
                "policy_version": "test",
                "effective_at": "2026-08-11T00:00:00Z",
                "min_conservative_edge": 0.05,
                "max_mlb_official_bets_per_day": 2,
                "starter_pending_promotions_enabled": False,
                "max_small_bets_per_day_probation": 1,
            }
        }))
        (state / "standing_authorization.json").write_text(json.dumps({
            "schema": "vig-standing-authorization-v1",
            "enabled": True,
        }))
        self.env_patcher = patch.dict("os.environ", {"VIG_STATE_DIR": str(state)})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        self.tmp.cleanup()

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
            PROBABILITY_TRAIL,
            vig_approved=True,
            vig_notes="All gates hold.",
            approved_polymarket_ask=0.48,
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
        self.assertEqual(candidate["max_polymarket_price"], POLICY_CEILING)
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
                    **PROBABILITY_TRAIL,
                }
            ]
        }

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(
            errors,
            ["candidate event_id:1|side:CWS has no strict numeric approved_polymarket_ask"],
        )
        self.assertNotEqual(after["candidates"][0].get("execution_mode"), "standing_authorized")

    def test_normalize_new_mlb_approval_rejected_with_non_finite_probability(self):
        # Routing regression for the NaN/Inf fail-closed defect: a candidate
        # carrying a full contract but a non-finite probability/ask field must
        # NOT route to standing-authorized execution. NaN comparisons are all
        # false, so a poisoned field can never be treated as meeting the floor.
        for field in (
            "dk_fair_prob",
            "raw_probability",
            "uncertainty_haircut",
            "conservative_probability",
            "current_ask",
            "projected_edge_at_current_ask",
        ):
            for bad in (float("nan"), float("inf"), float("-inf")):
                before = {
                    "candidates": [
                        {
                            "event_id": "1",
                            "side": "CWS",
                            "polymarket_ask": 0.48,
                            "vig_approved": None,
                        }
                    ]
                }
                after = json.loads(json.dumps(before))
                contract = dict(PROBABILITY_TRAIL)
                contract[field] = bad
                after["candidates"][0].update(
                    contract,
                    vig_approved=True,
                    vig_notes="All gates hold.",
                    approved_polymarket_ask=0.48,
                )

                errors = vig_review_gate_common.normalize_review_routing(
                    before, after, "MLB", mlb_standing_authorized=True
                )

                self.assertTrue(
                    errors,
                    msg=f"{field}={bad} must fail closed at routing",
                )
                self.assertIn(
                    "probability contract violation",
                    errors[0],
                    msg=f"{field}={bad} should be reported by the contract check",
                )
                self.assertNotEqual(
                    after["candidates"][0].get("execution_mode"),
                    "standing_authorized",
                    msg=f"{field}={bad} must not route to standing_authorized",
                )

    def test_normalize_rejects_regular_approval_with_only_legacy_ask_fields(self):
        # P1 regression (PR #48 review): a regular card approval must carry the
        # explicit approved_polymarket_ask exactly like a lineup promotion.
        # Legacy captured/original ask fields — in before or after — are never
        # a fallback.
        for legacy in (
            {"polymarket_ask": 0.525},
            {"captured_polymarket_ask": 0.525},
            {"polymarket_ask": 0.525, "captured_polymarket_ask": 0.51},
        ):
            with self.subTest(legacy=legacy):
                before = {
                    "candidates": [
                        {"event_id": "1", "side": "CWS", "vig_approved": None, **legacy}
                    ]
                }
                after = json.loads(json.dumps(before))
                after["candidates"][0].update(
                    PROBABILITY_TRAIL, vig_approved=True, vig_notes="Approved.", **legacy
                )

                errors = vig_review_gate_common.normalize_review_routing(
                    before, after, "MLB", mlb_standing_authorized=True
                )

                self.assertEqual(
                    errors,
                    ["candidate event_id:1|side:CWS has no strict numeric approved_polymarket_ask"],
                )
                self.assertNotEqual(
                    after["candidates"][0].get("execution_mode"), "standing_authorized"
                )

    def test_normalize_ignores_mutated_generic_ask_when_approved_ask_is_set(self):
        # The child may rewrite polymarket_ask; only approved_polymarket_ask
        # feeds routing, and the ceiling still comes from the shared policy.
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
            PROBABILITY_TRAIL,
            vig_approved=True,
            vig_notes="Approved.",
            approved_polymarket_ask=0.48,
            polymarket_ask=0.99,
        )

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(after["candidates"][0]["max_polymarket_price"], POLICY_CEILING)

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

    def test_normalize_valid_watchlist_promotion_routes_on_approved_ask(self):
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        promoted_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            **PROBABILITY_TRAIL,
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            # Must agree with the refreshed current_ask / supported_price the
            # same recheck wrote (PR #48 review, finding 2).
            "approved_polymarket_ask": 0.48,
        }
        promoted = self._watch_entry(
            status="promoted", promoted_candidate=dict(promoted_candidate)
        )
        # Exact upstream promotion shape: the approved price is a JSON number,
        # not American odds and not a quoted string.
        self.assertIsInstance(promoted_candidate["approved_polymarket_ask"], float)
        after = {"candidates": [promoted_candidate], "lineup_watchlist": [promoted]}

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(promoted_candidate["max_polymarket_price"], POLICY_CEILING)
        self.assertEqual(promoted["promoted_candidate"], promoted_candidate)

    def test_lineup_promotion_rejects_legacy_or_invalid_approved_ask(self):
        for field, value in (
            ("polymarket_ask", 0.51),
            ("captured_polymarket_ask", 0.51),
            ("approved_polymarket_ask", "0.51"),
            ("approved_polymarket_ask", 110),
            ("approved_polymarket_ask", 0),
            ("approved_polymarket_ask", 1),
        ):
            with self.subTest(field=field, value=value):
                before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
                candidate = {
                    "watchlist_id": "watch-1",
                    "side": "ABC",
                    **PROBABILITY_TRAIL,
                    "vig_approved": True,
                    "vig_notes": "All gates hold.",
                    field: value,
                }
                promoted = self._watch_entry(status="promoted", promoted_candidate=dict(candidate))
                errors = vig_review_gate_common.normalize_review_routing(
                    before,
                    {"candidates": [candidate], "lineup_watchlist": [promoted]},
                    "MLB",
                    mlb_standing_authorized=True,
                )
                self.assertTrue(any("approved_polymarket_ask" in error for error in errors))

    def test_promoted_candidate_transition_validates_full_approval_contract(self):
        before_entry = self._watch_entry()
        before_candidate = {
            "watchlist_id": "watch-1",
            "side": "ABC",
            "vig_approved": True,
            "vig_notes": "Morning approval persisted.",
        }
        promoted = self._watch_entry(
            status="promoted", promoted_candidate=dict(before_candidate)
        )
        promoted["recheck_notes"] = "Lineups confirmed; price still holds."
        after = {
            "candidates": [before_candidate],
            "lineup_watchlist": [promoted],
        }
        errors = vig_review_gate_common.validate_review_transition(
            {"candidates": [before_candidate], "lineup_watchlist": [before_entry]},
            after,
            [],
            ["watch-1"],
            "MLB",
            True,
        )
        self.assertTrue(
            any("approved_polymarket_ask" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("probability" in error for error in errors),
            errors,
        )

    def test_routing_fails_closed_when_policy_missing(self):
        # With no shared policy block loadable, standing-authorized routing must
        # refuse the entire review — never silently fall back to partial rails.
        state = Path(self.tmp.name) / "empty-state"
        state.mkdir()
        (state / "risk_limits.json").write_text(json.dumps({}))
        (state / "standing_authorization.json").write_text(json.dumps({
            "schema": "vig-standing-authorization-v1",
            "enabled": True,
        }))
        before = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "vig_approved": None,
                    "polymarket_ask": 0.48,
                }
            ]
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            PROBABILITY_TRAIL,
            vig_approved=True,
            vig_notes="All gates hold.",
            approved_polymarket_ask=0.48,
        )
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(state)}):
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )
        self.assertTrue(errors)
        self.assertIn("policy missing or invalid", errors[0])
        self.assertNotEqual(
            after["candidates"][0].get("execution_mode"), "standing_authorized"
        )

    def test_routing_with_deployed_policy_shape_stamps_policy_ceiling(self):
        # Integration with the canonical DEPLOYED key names (mlb_policy,
        # policy_effective_at, max_small_bets_per_day_during_probation):
        # normalization must stamp conservative_probability - floor, not the ask.
        state = Path(self.tmp.name) / "deployed-state"
        state.mkdir()
        (state / "risk_limits.json").write_text(json.dumps({
            "mlb_policy": {
                "schema": "vig-mlb-selection-policy-v1",
                "policy_version": "2026-08-11-hardening-pr1",
                "policy_effective_at": "2026-08-11T00:00:00Z",
                "min_conservative_edge": 0.05,
                "max_mlb_official_bets_per_day": 2,
                "starter_pending_promotions_enabled": False,
                "max_small_bets_per_day_during_probation": 1,
            }
        }))
        before = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "vig_approved": None,
                    "polymarket_ask": 0.50,
                }
            ]
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            PROBABILITY_TRAIL,
            vig_approved=True,
            vig_notes="All gates hold.",
            approved_polymarket_ask=0.50,
            execution_checks=valid_execution_checks(supported_price=0.50),
            **consistent_probability_overrides(0.58, 0.50),
        )
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(state)}):
            errors = vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            )
        self.assertEqual(errors, [])
        # 0.58 - 0.05 = 0.53, NOT the 0.50 ask.
        self.assertAlmostEqual(
            after["candidates"][0]["max_polymarket_price"], 0.53, places=6
        )

    def test_normalize_rejects_third_approved_candidate_beyond_daily_limit(self):
        # Three approvals each with a passing price: the shared policy caps the
        # day at two official MLB bets, ranked by live conservative edge.
        def _candidate(event_id, edge):
            return {
                "event_id": event_id,
                "side": "CWS",
                "polymarket_ask": 0.48,
                "approved_polymarket_ask": 0.48,
                "vig_approved": None,
                "dk_fair_prob": 0.55,
                "model_version": "market-only-fallback-v1",
                "baseball_evidence": valid_baseball_evidence(),
                "execution_checks": valid_execution_checks(supported_price=0.48),
                **consistent_probability_overrides(round(0.48 + edge, 6), 0.48),
            }

        before = {
            "candidates": [
                _candidate("1", 0.07),
                _candidate("2", 0.12),
                _candidate("3", 0.09),
            ],
            "lineup_watchlist": [],
        }
        after = json.loads(json.dumps(before))
        for candidate in after["candidates"]:
            candidate.update(vig_approved=True, vig_notes="All gates hold.")

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("daily candidate limit 2 exceeded", errors[0])
        self.assertIn("event_id:1|side:CWS", errors[0])  # lowest edge rejected
        self.assertNotIn("event_id:2|side:CWS", errors[0])
        self.assertNotIn("event_id:3|side:CWS", errors[0])

    def test_normalize_caps_manual_state_new_approvals_before_rewrite(self):
        # Regression: three newly approved MLB children arrive in manual state
        # (the routing flow repairs them to standing_authorized below). The
        # daily cap must count them BEFORE the rewrite; filtering the cap pool
        # on execution_mode would let all three bypass the cap and then all be
        # rewritten to standing_authorized.
        def _candidate(event_id, edge):
            return {
                "event_id": event_id,
                "side": "CWS",
                "polymarket_ask": 0.48,
                "approved_polymarket_ask": 0.48,
                "vig_approved": None,
                "dk_fair_prob": 0.55,
                "model_version": "market-only-fallback-v1",
                "baseball_evidence": valid_baseball_evidence(),
                "execution_checks": valid_execution_checks(supported_price=0.48),
                **consistent_probability_overrides(round(0.48 + edge, 6), 0.48),
            }

        before = {
            "candidates": [
                _candidate("1", 0.07),
                _candidate("2", 0.12),
                _candidate("3", 0.09),
            ],
            "lineup_watchlist": [],
        }
        after = json.loads(json.dumps(before))
        for candidate in after["candidates"]:
            candidate.update(
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

        self.assertEqual(len(errors), 1)
        self.assertIn("daily candidate limit 2 exceeded", errors[0])
        # Lowest edge (event_id 1) is the rejected tail; nothing may be
        # rewritten to standing_authorized because the cap rejects first.
        for candidate in after["candidates"]:
            self.assertEqual(candidate["execution_mode"], "manual")

    def test_normalize_cap_preserves_genuinely_manual_only_candidate(self):
        # A pre-existing manual-only candidate (never rewritten, not newly
        # approved) must not consume a standing-authorized cap slot.
        def _candidate(event_id, edge, vig_approved=None):
            return {
                "event_id": event_id,
                "side": "CWS",
                "polymarket_ask": 0.48,
                "approved_polymarket_ask": 0.48,
                "vig_approved": vig_approved,
                "dk_fair_prob": 0.55,
                "model_version": "market-only-fallback-v1",
                "baseball_evidence": valid_baseball_evidence(),
                "execution_checks": valid_execution_checks(supported_price=0.48),
                **consistent_probability_overrides(round(0.48 + edge, 6), 0.48),
            }

        before = {
            "candidates": [
                _candidate("manual", 0.20, vig_approved=True),
                _candidate("1", 0.07),
                _candidate("2", 0.12),
            ],
            "lineup_watchlist": [],
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0]["execution_mode"] = "manual"
        after["candidates"][0]["manual_bet_status"] = "awaiting_jerry"
        for candidate in after["candidates"][1:]:
            candidate.update(vig_approved=True, vig_notes="All gates hold.")

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        # The manual-only candidate is excluded from the cap pool, so the two
        # new approvals fit within the cap of two.
        self.assertEqual(errors, [])
        self.assertEqual(after["candidates"][0]["execution_mode"], "manual")

    def test_normalize_rejects_approval_below_conservative_edge_floor(self):
        before = {
            "candidates": [
                {
                    "event_id": "1",
                    "side": "CWS",
                    "polymarket_ask": 0.48,
                    "vig_approved": None,
                }
            ],
            "lineup_watchlist": [],
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            dk_fair_prob=0.55,
            raw_probability=0.56,
            uncertainty_haircut=0.02,
            conservative_probability=0.529,
            current_ask=0.48,
            projected_edge_at_current_ask=0.049,
            model_version="market-only-fallback-v1",
            vig_approved=True,
            vig_notes="Approved.",
        )

        errors = vig_review_gate_common.validate_review_transition(
            before,
            after,
            [vig_review_gate_common.candidate_identity(after["candidates"][0])],
            [],
            "MLB",
            mlb_standing_authorized=True,
        )

        self.assertTrue(errors)
        self.assertTrue(
            any("below the shared policy floor" in message for message in errors),
            msg=repr(errors),
        )

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

        with (
            patch.object(
                vig_review_gate_common, "fetch_lineup_snapshot", return_value=snapshot
            ) as fetch,
            patch.object(
                vig_review_gate_common,
                "fetch_market_price",
                return_value={
                    "slug": "aec-mlb-cin-sea-2026-07-17",
                    "open": True,
                    "reason": "open",
                    "long_ask": "0.5750",
                    "no_ask": "0.4300",
                    "book_state": "reliable",
                },
            ),
        ):
            prompt, deferral_eligible = vig_review_gate_common.build_lineup_recheck_prompt(
                Path("/tmp/schedule.json"), [entry]
            )

        fetch.assert_called_once_with(entry)
        self.assertIn("MLB gamePk 823110", prompt)
        self.assertNotIn("401816229/feed/live", prompt)
        # Both live inputs resolved: nothing is eligible to defer.
        self.assertEqual(deferral_eligible, set())

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
                # Future first pitch: a live invalid entry must stay a hard
                # gate error (only provably-dead past-pitch entries quarantine).
                now = datetime.now(timezone.utc)
                bad_entry = self._watch_entry(
                    original_price="MIN +119 at DraftKings",
                    bettable_to_price="+105",
                    first_pitch_utc=(now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                    recheck_due_utc=(now + timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
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
                        PROBABILITY_TRAIL,
                        vig_approved=True,
                        vig_notes="All gates hold.",
                        approved_polymarket_ask=0.48,
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
                self.assertEqual(reviewed["candidates"][0]["max_polymarket_price"], POLICY_CEILING)
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
                    **PROBABILITY_TRAIL,
                    "vig_approved": True,
                    "vig_notes": "All gates hold.",
                    "approved_polymarket_ask": 0.48,
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
                    rechecked_at_utc="2026-07-19T17:00:00Z",
                    recheck_notes="Both lineups confirmed; matchup edge still holds.",
                    slate_probability=dict(PROBABILITY_COMPONENTS),
                    recheck=json.loads(json.dumps(REFRESHED_RECHECK)),
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

    def test_abandoned_pending_entry_is_warned_but_live_recheck_is_not(self):
        # 08-23 approval follow-up (Reviewer non-blocking #2): the overdue
        # warning had no production caller. It now runs in the lane that owns
        # rechecks — but scoped: due_entries selects on the first-pitch window
        # and never on recheck_due_utc, so the entry being rechecked right now
        # carries a long-past due stamp and must NOT be warned. Only the entry
        # the gate will never pick up again is a zombie.
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
                now = datetime.now(timezone.utc)
                first_pitch = now + timedelta(minutes=75)
                due_entry = self._watch_entry(
                    side="MIN",
                    game="Minnesota Twins at Chicago Cubs",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat(),
                )
                # Valid, still pending, first pitch long past: due_entries can
                # never return it again and stale_invalid_watchlist only covers
                # INVALID entries, so nothing else surfaces it.
                zombie = self._watch_entry(
                    id="watch-zombie",
                    side="SEA",
                    game="Seattle Mariners at Texas Rangers",
                    bettable_to_price=105,
                    first_pitch_utc=(now - timedelta(hours=6)).isoformat(),
                    recheck_due_utc=(now - timedelta(hours=7)).isoformat(),
                )
                schedule_path.write_text(
                    json.dumps({"candidates": [], "lineup_watchlist": [due_entry, zombie]})
                )
                passed_entry = dict(due_entry)
                passed_entry.update(
                    status="passed",
                    rechecked_at_utc="2026-07-19T17:00:00Z",
                    recheck_notes="Lineups posted without the bat the edge rested on.",
                )

                def complete_review(*args, **kwargs):
                    schedule_path.write_text(
                        json.dumps(
                            {
                                "candidates": [],
                                "lineup_watchlist": [passed_entry, zombie],
                            }
                        )
                    )
                    return vig_review_gate_common.subprocess.CompletedProcess(
                        args[0], 0, stdout="", stderr=""
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
                    redirect_stdout(output),
                ):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                printed = output.getvalue()
                self.assertIn("lineup recheck overdue on watch-zombie", printed)
                self.assertIn("MLB review gate NOTICE:", printed)
                self.assertNotIn("watch-1", printed)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_zombie_alone_on_the_schedule_is_still_reported(self):
        # PR #53 review, non-blocking: the notice sat below the no-work early
        # return, so it reported a zombie only on cycles that happened to have
        # OTHER review work — the opposite of when it is needed. The case it
        # exists for is an abandoned entry with nothing else on the schedule:
        # the last stuck entry of the day, or a one-game slate. No reviewer
        # child is spawned, so this stdout is the job's own delivery.
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
                now = datetime.now(timezone.utc)
                zombie = self._watch_entry(
                    id="watch-zombie",
                    side="SEA",
                    game="Seattle Mariners at Texas Rangers",
                    bettable_to_price=105,
                    first_pitch_utc=(now - timedelta(hours=6)).isoformat(),
                    recheck_due_utc=(now - timedelta(hours=7)).isoformat(),
                )
                schedule_path.write_text(
                    json.dumps({"candidates": [], "lineup_watchlist": [zombie]})
                )

                output = StringIO()
                with (
                    patch.object(
                        vig_review_gate_common.subprocess,
                        "run",
                        side_effect=AssertionError("no reviewer child on a no-work cycle"),
                    ),
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    ),
                    redirect_stdout(output),
                ):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                self.assertIn("lineup recheck overdue on watch-zombie", output.getvalue())
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_unreachable_first_pitch_is_reported_by_run_gate(self):
        # PR #55 review. Preferring first pitch over the derived stamp moved the
        # one-wrong-number hole instead of closing it. This entry is valid,
        # never selected by due_entries (its recheck window opens three days
        # from now and the day's gate will never see that window), never
        # quarantined, and silent to the overdue notice — whose deadline comes
        # from the same wrong number. Without this notice nothing surfaces it.
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
                now = datetime.now(timezone.utc)
                first_pitch = now + timedelta(days=3)
                mistyped = self._watch_entry(
                    id="watch-mistyped",
                    side="SEA",
                    game="Seattle Mariners at Texas Rangers",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat(),
                    # derived from first pitch exactly as references/mlb.md
                    # instructs the slate agent, so both fields carry the error
                    recheck_due_utc=(first_pitch - timedelta(minutes=75)).isoformat(),
                )
                schedule_path.write_text(
                    json.dumps({"candidates": [], "lineup_watchlist": [mistyped]})
                )

                output = StringIO()
                with (
                    patch.object(
                        vig_review_gate_common.subprocess,
                        "run",
                        side_effect=AssertionError("no reviewer child on a no-work cycle"),
                    ),
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    ),
                    redirect_stdout(output),
                ):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                printed = output.getvalue()
                self.assertIn("first pitch on watch-mistyped cannot belong", printed)
                # The overdue notice cannot see it — that is the whole point.
                self.assertNotIn("lineup recheck overdue", printed)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_empty_schedule_still_emits_nothing(self):
        # Hoisting the notice must not make quiet cycles chatty.
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
                    json.dumps({"candidates": [], "lineup_watchlist": []})
                )

                output = StringIO()
                with redirect_stdout(output):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                self.assertEqual(output.getvalue(), "")
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    _RESOLVED_LINEUP_SNAPSHOT = {
        "game_pk": 823110,
        "away_team": "Minnesota Twins",
        "home_team": "Chicago Cubs",
        "player_count": 52,
        "away_batting_order": ["Away"] * 9,
        "home_batting_order": ["Home"] * 9,
    }

    _LIVE_QUOTE = {
        "slug": "aec-mlb-min-chc-2026-07-17",
        "open": True,
        "reason": "open",
        "long_ask": "0.5750",
        "no_ask": "0.4300",
        "book_state": "reliable",
    }

    def _run_deferred_noop_gate(self, root, price, lineup_snapshot):
        """Drive run_gate with a due watchlist entry the child leaves untouched.

        price / lineup_snapshot configure the machine-verified availability of
        each live input: None price means the fetch failed; an Exception
        lineup_snapshot means the feed fetch raised. Returns (status, output,
        entry, schedule_path).
        """
        day = vig_review_gate_common.datetime.now(
            vig_review_gate_common.ZoneInfo("America/Chicago")
        ).date().isoformat()
        schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
        schedule_path.parent.mkdir(parents=True)
        first_pitch = datetime.now(timezone.utc) + timedelta(minutes=75)
        entry = self._watch_entry(
            side="MIN",
            game="Minnesota Twins at Chicago Cubs",
            first_pitch_utc=first_pitch.isoformat(),
            polymarket_slug="aec-mlb-min-chc-2026-07-17",
        )
        schedule_path.write_text(
            json.dumps({"candidates": [], "lineup_watchlist": [entry]})
        )

        def noop_review(*args, **kwargs):
            # The child reviewer exits 0 and leaves the schedule untouched.
            return vig_review_gate_common.subprocess.CompletedProcess(
                args[0], 0, stdout="Recheck deferred", stderr=""
            )

        lineup_kwargs = (
            {"side_effect": lineup_snapshot}
            if isinstance(lineup_snapshot, Exception)
            else {"return_value": lineup_snapshot}
        )
        output = StringIO()
        with (
            patch.object(
                vig_review_gate_common.subprocess, "run", side_effect=noop_review
            ),
            patch.object(
                vig_review_gate_common, "fetch_market_price", return_value=price
            ),
            patch.object(
                vig_review_gate_common, "fetch_lineup_snapshot", **lineup_kwargs
            ),
            redirect_stdout(output),
        ):
            status = vig_review_gate_common.run_gate("MLB")
        return status, output.getvalue(), entry, schedule_path

    def _assert_deferred_noop_accepted(self, status, output, entry, schedule_path, root):
        self.assertEqual(status, 0)
        self.assertNotIn("pre-review schedule restored", output)
        self.assertNotIn("invalid review transition", output)
        self.assertIn("MLB lineup recheck — DEFERRED", output)
        self.assertIn("Status: still pending recheck; no bet", output)
        # The candidate/watchlist state on disk is byte-identical: no
        # fabricated approval, price, probability, or execution fields.
        # (The routing normalizer's top-level sport/market_type/date
        # stamp predates this fix and is not part of the entry state.)
        persisted = json.loads(schedule_path.read_text())
        self.assertEqual(persisted["lineup_watchlist"], [entry])
        self.assertEqual(persisted["candidates"], [])
        latest = (root / ".picks" / "latest-action.md").read_text()
        self.assertIn("1 lineup watchlist recheck pending", latest)
        self.assertIn("Review gate placed no bet", latest)

    def test_no_price_deferred_recheck_is_accepted_without_rollback(self):
        # Regression, isolated to the intended case: ONLY the live price fetch
        # fails (the lineup feed resolves both orders), the recheck leaves the
        # due entry byte-identical at pending_lineup_recheck, and the gate must
        # accept the deferred no-op instead of restoring the pre-review schedule.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                status, output, entry, schedule_path = self._run_deferred_noop_gate(
                    root, price=None, lineup_snapshot=dict(self._RESOLVED_LINEUP_SNAPSHOT)
                )
                self._assert_deferred_noop_accepted(
                    status, output, entry, schedule_path, root
                )
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_lineup_feed_failure_deferred_recheck_is_accepted_without_rollback(self):
        # The other machine-verified unavailable input, isolated: the price
        # resolves but the lineup feed fetch raises. A deferred no-op is still
        # legitimate — the entry stays due for the next cycle.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                status, output, entry, schedule_path = self._run_deferred_noop_gate(
                    root,
                    price=dict(self._LIVE_QUOTE),
                    lineup_snapshot=Exception("lineup feed unavailable"),
                )
                self._assert_deferred_noop_accepted(
                    status, output, entry, schedule_path, root
                )
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_closed_market_deferred_recheck_is_accepted_without_rollback(self):
        # 08-23 review P1: a closed/inactive market returns a full dict (no
        # raise), so the child is shown "market open=False" with no usable ask
        # — the prompt tells it to defer, and the validator must accept that
        # defer instead of rolling back the whole day's review.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                closed = dict(self._LIVE_QUOTE)
                closed["open"] = False
                closed["reason"] = "market not open for trading"
                status, output, entry, schedule_path = self._run_deferred_noop_gate(
                    root,
                    price=closed,
                    lineup_snapshot=dict(self._RESOLVED_LINEUP_SNAPSHOT),
                )
                self._assert_deferred_noop_accepted(
                    status, output, entry, schedule_path, root
                )
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_unreliable_book_deferred_recheck_is_accepted_without_rollback(self):
        # The other non-raising unavailable state: an unreliable book (missing
        # side / crossed / wide spread) hands the child ask=None on its side.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                unreliable = dict(self._LIVE_QUOTE)
                unreliable["long_ask"] = None
                unreliable["no_ask"] = None
                unreliable["book_state"] = "unreliable"
                status, output, entry, schedule_path = self._run_deferred_noop_gate(
                    root,
                    price=unreliable,
                    lineup_snapshot=dict(self._RESOLVED_LINEUP_SNAPSHOT),
                )
                self._assert_deferred_noop_accepted(
                    status, output, entry, schedule_path, root
                )
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_unchanged_pending_with_available_inputs_fails_and_restores(self):
        # Real-boundary negative: a valid live quote AND both confirmed
        # lineups were supplied to the child, so an unchanged pending entry is
        # an unreviewed entry, not a defer. The gate must fail the transition,
        # restore the pre-review schedule, and exit nonzero — never report
        # DEFERRED for work it proved was doable.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                status, output, entry, schedule_path = self._run_deferred_noop_gate(
                    root,
                    price=dict(self._LIVE_QUOTE),
                    lineup_snapshot=dict(self._RESOLVED_LINEUP_SNAPSHOT),
                )

                self.assertEqual(status, 1)
                self.assertIn("invalid review transition", output)
                self.assertIn("machine-verified", output)
                self.assertIn("pre-review schedule restored", output)
                self.assertNotIn("DEFERRED", output)
                self.assertFalse((root / ".picks" / "latest-action.md").exists())
                persisted = json.loads(schedule_path.read_text())
                self.assertEqual(persisted["lineup_watchlist"], [entry])
                self.assertEqual(persisted["candidates"], [])
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
                        PROBABILITY_TRAIL,
                        vig_approved=True,
                        vig_notes="All gates hold.",
                        approved_polymarket_ask=0.48,
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

    def test_mlb_review_prompt_includes_evidence_schema(self):
        # Phase 2 hard validators reject any approval without structured
        # baseball_evidence/execution_checks, so the review prompt must hand
        # Vig the schema — otherwise every approval fails closed at routing.
        prompt = vig_review_gate_common.build_regular_review_prompt(
            "MLB",
            "2026-07-17",
            Path("/tmp/schedule.json"),
            [{"side": "ABC"}],
            mlb_standing_authorized=True,
        )

        self.assertIn("BASEBALL EVIDENCE (required object", prompt)
        self.assertIn("EXECUTION CHECKS (required object", prompt)
        self.assertIn("bullpen_availability.leverage_arms_available must be true", prompt)
        self.assertIn('named_risks: list of {name, status: "resolved" | "unresolved", evidence}', prompt)
        self.assertIn("probability_delta_explanation", prompt)

    def test_soccer_and_manual_review_prompts_omit_evidence_schema(self):
        # Soccer and non-standing-authorized MLB reviews carry no evidence
        # contract; their prompts stay unchanged.
        for sport in ("SOCCER", "MLB"):
            prompt = vig_review_gate_common.build_regular_review_prompt(
                sport, "2026-07-17", Path("/tmp/schedule.json"), [{"side": "ABC"}]
            )
            self.assertNotIn("BASEBALL EVIDENCE", prompt)
            self.assertNotIn("EXECUTION CHECKS (required object", prompt)

    def test_review_prompt_states_zero_fee_and_no_phantom_fee(self):
        for sport, kwargs in (
            ("MLB", {"mlb_standing_authorized": True}),
            ("SOCCER", {}),
        ):
            prompt = vig_review_gate_common.build_regular_review_prompt(
                sport,
                "2026-08-09",
                Path("/tmp/schedule.json"),
                [{"side": "ABC"}],
                **kwargs,
            )
            # The ceiling is the single guardrail: judge the real cost to buy
            # against max_polymarket_price = conservative_probability - floor,
            # no fee math. The shared 5-point conservative edge floor replaced
            # the hard-coded 2-point floor in PR 1 of the hardening plan.
            self.assertIn("max_polymarket_price = conservative_probability - 0.05", prompt)
            self.assertNotIn("max_polymarket_price = win_probability - 0.02", prompt)
            self.assertIn("cost to buy", prompt)
            self.assertIn("ZERO", prompt)
            # The uncertainty haircut is a model-uncertainty buffer, never a fee.
            self.assertIn("NEVER a venue fee", prompt)
            # No phantom-fee SUBTRACTION (the bug that rejected the 2026-08-09
            # Brewers pick); naming 0.024 as forbidden is fine, subtracting is not.
            self.assertNotIn("- 0.024", prompt)
            self.assertNotIn("net_edge = win_probability - polymarket_ask - 0.024", prompt)
            # The probability contract is stated explicitly.
            self.assertIn("dk_fair_prob", prompt)
            self.assertIn("conservative_probability", prompt)
            self.assertIn("projected_edge_at_current_ask", prompt)
            self.assertIn("model_version", prompt)

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

    def test_unchanged_preexisting_invalid_watch_entry_does_not_wedge_review(self):
        # Historical garbage written outside this gate (2026-08-11 incident:
        # invented status, emptied blockers) must not reject an unrelated
        # review that left it byte-identical.
        garbage = self._watch_entry(
            id="LW-history-garbage",
            first_pitch_utc="2026-07-16T01:40:00Z",
            status="recheck_complete",
            blocked_only_by=[],
        )
        target = self._watch_entry(
            status="passed",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck_notes="ask over ceiling at recheck",
        )
        before = {"candidates": [], "lineup_watchlist": [garbage, self._watch_entry()]}
        after = {"candidates": [], "lineup_watchlist": [garbage, target]}

        errors = vig_review_gate_common.validate_review_transition(before, after, [], ["watch-1"])

        self.assertFalse([e for e in errors if "LW-history-garbage" in e], errors)

    def test_review_that_introduces_invalid_watch_entry_still_fails(self):
        # The tolerance is strictly for unchanged pre-existing entries: a
        # review that writes a new invalid entry (or edits one) is rejected.
        injected = self._watch_entry(
            id="LW-injected",
            status="recheck_complete",
            blocked_only_by=[],
        )
        target = self._watch_entry(
            status="passed",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            recheck_notes="ask over ceiling at recheck",
        )
        before = {"candidates": [], "lineup_watchlist": [self._watch_entry()]}
        after = {"candidates": [], "lineup_watchlist": [injected, target]}

        errors = vig_review_gate_common.validate_review_transition(before, after, [], ["watch-1"])

        self.assertTrue([e for e in errors if "LW-injected" in e], errors)

    def test_unchanged_pending_targeted_watch_entry_is_a_deferred_noop(self):
        # A recheck whose live inputs were machine-verified unavailable this
        # cycle intentionally leaves the entry byte-identical at
        # pending_lineup_recheck; that is a valid deferred no-op, not a failed
        # transition, so the cron must not roll the schedule back. Eligibility
        # is the caller-supplied machine evidence, never inferred from the diff.
        before = {
            "candidates": [],
            "lineup_watchlist": [self._watch_entry()],
        }
        after = {
            "candidates": [],
            "lineup_watchlist": [self._watch_entry()],
        }

        errors = vig_review_gate_common.validate_review_transition(
            before, after, [], ["watch-1"], deferral_eligible_ids={"watch-1"}
        )

        self.assertEqual(errors, [])

    def test_unchanged_pending_without_unavailable_input_evidence_fails(self):
        # Fail-closed: with no machine-verified unavailable input for the
        # entry (or no evidence supplied at all), an unchanged pending entry
        # is an unreviewed entry, not a defer.
        before = {
            "candidates": [],
            "lineup_watchlist": [self._watch_entry()],
        }
        after = {
            "candidates": [],
            "lineup_watchlist": [self._watch_entry()],
        }

        default_errors = vig_review_gate_common.validate_review_transition(
            before, after, [], ["watch-1"]
        )
        other_entry_errors = vig_review_gate_common.validate_review_transition(
            before, after, [], ["watch-1"], deferral_eligible_ids={"watch-other"}
        )

        for errors in (default_errors, other_entry_errors):
            self.assertTrue(
                [e for e in errors if "watch-1" in e and "machine-verified" in e],
                errors,
            )

    def test_edited_pending_targeted_watch_entry_still_fails(self):
        # The deferred no-op tolerance is strictly for an UNCHANGED entry: a
        # review that edits an entry while leaving it pending is still an
        # unfinished transition, even when the entry WAS deferral-eligible.
        before = {
            "candidates": [],
            "lineup_watchlist": [self._watch_entry()],
        }
        after = {
            "candidates": [],
            "lineup_watchlist": [
                self._watch_entry(rechecked_at_utc="2026-07-17T21:50:00Z")
            ],
        }

        errors = vig_review_gate_common.validate_review_transition(
            before, after, [], ["watch-1"], deferral_eligible_ids={"watch-1"}
        )

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
            **PROBABILITY_TRAIL,
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            "execution_mode": "standing_authorized",
            "execution_status": "pending",
            "max_polymarket_price": 0.51,
            "approved_polymarket_ask": 0.48,
            "executed": False,
        }
        promoted = self._watch_entry(
            status="promoted",
            rechecked_at_utc="2026-07-17T21:45:00Z",
            slate_probability=dict(PROBABILITY_COMPONENTS),
            recheck=json.loads(json.dumps(REFRESHED_RECHECK)),
            promoted_candidate=promoted_candidate,
        )
        after = {"candidates": [promoted_candidate], "lineup_watchlist": [promoted]}

        self.assertEqual(
            vig_review_gate_common.validate_review_transition(
                before, after, [], ["watch-1"], mlb_standing_authorized=True
            ),
            [],
        )

    def test_regular_approval_transition_requires_approved_polymarket_ask(self):
        # P1 regression (PR #48 review): the transition validator holds a
        # regular standing-authorized approval to the same explicit
        # approved_polymarket_ask contract as a lineup promotion.
        def routed_candidate(**overrides):
            candidate = {
                "event_id": "9",
                "side": "NYY",
                "sport": "MLB",
                "market_type": "moneyline",
                **PROBABILITY_TRAIL,
                "vig_approved": True,
                "vig_notes": "All gates hold.",
                "execution_mode": "standing_authorized",
                "execution_status": "pending",
                "max_polymarket_price": 0.51,
                "executed": False,
            }
            candidate.update(overrides)
            return candidate

        before = {
            "candidates": [{"event_id": "9", "side": "NYY", "vig_approved": None}],
            "lineup_watchlist": [],
        }
        identity = vig_review_gate_common.candidate_identity(before["candidates"][0])

        missing_ask = {
            "candidates": [routed_candidate()],
            "lineup_watchlist": [],
        }
        errors = vig_review_gate_common.validate_review_transition(
            before, missing_ask, [identity], [], "MLB", mlb_standing_authorized=True
        )
        self.assertTrue(
            any("approved_polymarket_ask" in error for error in errors), errors
        )

        with_ask = {
            "candidates": [routed_candidate(approved_polymarket_ask=0.48)],
            "lineup_watchlist": [],
        }
        self.assertEqual(
            vig_review_gate_common.validate_review_transition(
                before, with_ask, [identity], [], "MLB", mlb_standing_authorized=True
            ),
            [],
        )

    def test_normalize_rejects_approved_ask_disagreeing_with_refreshed_contract(self):
        # P1 regression (PR #48 review, finding 2): a strict-numeric
        # approved_polymarket_ask that disagrees with the refreshed price
        # contract (current_ask=0.48, supported_price=0.48) previously routed
        # with empty error lists. It must fail closed at normalization.
        before = {
            "candidates": [
                {"event_id": "1", "side": "CWS", "vig_approved": None, "polymarket_ask": 0.48}
            ]
        }
        after = json.loads(json.dumps(before))
        after["candidates"][0].update(
            PROBABILITY_TRAIL,
            vig_approved=True,
            vig_notes="All gates hold.",
            approved_polymarket_ask=0.01,
        )

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("approved ask violation", errors[0])
        self.assertIn("does not match the refreshed current_ask", errors[0])
        self.assertIn("does not match execution_checks.supported_price", errors[0])
        self.assertNotEqual(
            after["candidates"][0].get("execution_mode"), "standing_authorized"
        )

    def test_transition_rejects_approved_ask_disagreeing_with_refreshed_contract(self):
        # Same P1 at the transition layer, including the case where only
        # execution_checks.supported_price disagrees while current_ask agrees.
        def routed_candidate(**overrides):
            candidate = {
                "event_id": "9",
                "side": "NYY",
                "sport": "MLB",
                "market_type": "moneyline",
                **PROBABILITY_TRAIL,
                "vig_approved": True,
                "vig_notes": "All gates hold.",
                "execution_mode": "standing_authorized",
                "execution_status": "pending",
                "max_polymarket_price": 0.51,
                "executed": False,
            }
            candidate.update(overrides)
            return candidate

        before = {
            "candidates": [{"event_id": "9", "side": "NYY", "vig_approved": None}],
            "lineup_watchlist": [],
        }
        identity = vig_review_gate_common.candidate_identity(before["candidates"][0])

        for label, overrides, expected in (
            (
                "ask far below refreshed contract",
                {"approved_polymarket_ask": 0.01},
                "does not match the refreshed current_ask",
            ),
            (
                "supported_price disagrees while current_ask agrees",
                {
                    "approved_polymarket_ask": 0.48,
                    "execution_checks": valid_execution_checks(supported_price=0.52),
                },
                "does not match execution_checks.supported_price",
            ),
        ):
            with self.subTest(label):
                after = {
                    "candidates": [routed_candidate(**overrides)],
                    "lineup_watchlist": [],
                }
                errors = vig_review_gate_common.validate_review_transition(
                    before, after, [identity], [], "MLB", mlb_standing_authorized=True
                )
                self.assertTrue(
                    any(expected in error for error in errors), errors
                )

    def test_valid_baseball_evidence_passes_review_routing(self):
        # Positive-path fixture: a candidate with valid Phase-2 evidence and
        # execution checks must route without errors. The regression fixtures
        # prove the gate can say no; this proves it can say yes when it should.
        before = {"candidates": [], "lineup_watchlist": []}
        approved_candidate = {
            "id": "pos-1",
            "sport": "MLB",
            "market_type": "moneyline",
            "side": "NYY",
            "price": -130,
            "vig_approved": True,
            "vig_notes": "All gates hold.",
            "execution_mode": "manual",
            "manual_bet_status": "awaiting_jerry",
            "executed": False,
            **PROBABILITY_TRAIL,
        }
        after = {"candidates": [approved_candidate], "lineup_watchlist": []}
        identity = vig_review_gate_common.candidate_identity(approved_candidate)
        self.assertEqual(
            vig_review_gate_common.validate_review_transition(before, after, [identity], []),
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
    ctx, no_price_ids = g._price_context(entries)
    assert "Deterministic Polymarket US prices" in ctx
    assert "long/YES ask=0.5750" in ctx and "NO-side ask=0.4300" in ctx
    assert "DO NOT web-search" in ctx
    assert no_price_ids == set()  # a fetched price is never deferral evidence


def test_price_context_degrades_without_slug_or_fetch(monkeypatch):
    import vig_review_gate_common as g
    monkeypatch.setattr(g, "fetch_market_price", lambda slug: None)
    ctx, no_price_ids = g._price_context(
        [{"id": "e2", "polymarket_slug": "aec-mlb-x-y-2026-07-27", "thesis": ""}]
    )
    assert "current price unavailable" in ctx  # never raises; recheck carries ceiling to poller
    assert no_price_ids == {"e2"}  # slug resolved + fetch failed = machine-verified unavailable


def test_price_context_missing_slug_is_not_deferral_eligible(monkeypatch):
    # A missing slug is a data defect, not a transient outage: the entry gets
    # no price line and NO deferral eligibility, so an unchanged pending no-op
    # on it fails the transition instead of no-opping forever.
    import vig_review_gate_common as g
    monkeypatch.setattr(g, "fetch_market_price", lambda slug: (_ for _ in ()).throw(AssertionError("must not fetch")))
    ctx, no_price_ids = g._price_context([{"id": "e3", "thesis": "no slug here"}])
    assert "no Polymarket slug resolvable" in ctx
    assert no_price_ids == set()


def test_price_context_defer_markers_correspond_to_eligibility(monkeypatch):
    # The correspondence contract itself (08-23 review): every entry whose
    # price line tells the child to defer is deferral-eligible, and every
    # eligible entry's line carries the defer marker — across fetch-failure,
    # closed-market, unreliable-book, usable-quote, and no-slug states. The
    # markers and the eligible set are derived from the same predicate, and
    # this test pins that they can never diverge again.
    import vig_review_gate_common as g

    snapshots = {
        "aec-mlb-outage-2026-08-23": None,
        "aec-mlb-closed-2026-08-23": {
            "slug": "aec-mlb-closed-2026-08-23", "open": False,
            "reason": "market not open for trading", "long_ask": "0.5750",
            "no_ask": "0.4300", "book_state": "reliable",
        },
        "aec-mlb-unreliable-2026-08-23": {
            "slug": "aec-mlb-unreliable-2026-08-23", "open": True,
            "reason": "open", "long_ask": None, "no_ask": None,
            "book_state": "unreliable",
        },
        "aec-mlb-good-2026-08-23": {
            "slug": "aec-mlb-good-2026-08-23", "open": True, "reason": "open",
            "long_ask": "0.5750", "no_ask": "0.4300", "book_state": "reliable",
        },
    }
    monkeypatch.setattr(g, "fetch_market_price", lambda slug: snapshots[slug])
    entries = [
        {"id": "watch-outage", "polymarket_slug": "aec-mlb-outage-2026-08-23", "thesis": ""},
        {"id": "watch-closed", "polymarket_slug": "aec-mlb-closed-2026-08-23", "thesis": ""},
        {"id": "watch-unreliable", "polymarket_slug": "aec-mlb-unreliable-2026-08-23", "thesis": ""},
        {"id": "watch-good", "polymarket_slug": "aec-mlb-good-2026-08-23", "thesis": ""},
        {"id": "watch-noslug", "thesis": "no slug here"},
    ]
    ctx, eligible = g._price_context(entries)

    assert eligible == {"watch-outage", "watch-closed", "watch-unreliable"}
    for line in ctx.splitlines():
        entry_ids = [e["id"] for e in entries if line.startswith(e["id"])]
        if not entry_ids:
            continue
        (entry_id,) = entry_ids
        # Defer marker on a line <=> that id is deferral-eligible.
        assert (g.PRICE_UNAVAILABLE_MARKER in line) == (entry_id in eligible), line
    # The data-defect line instructs a decisive pass, never a defer.
    noslug_line = next(l for l in ctx.splitlines() if l.startswith("watch-noslug"))
    assert g.PRICE_DEFECT_MARKER in noslug_line
    assert g.PRICE_UNAVAILABLE_MARKER not in noslug_line
    # An unusable quote must never print tradable-looking asks next to the
    # defer marker (08-23 approval note 3): a closed market can still carry
    # numbers in the book, and showing them invites pricing off an untradable
    # market.
    for entry_id in ("watch-closed", "watch-unreliable"):
        line = next(l for l in ctx.splitlines() if l.startswith(entry_id))
        assert "no executable ask" in line, line
        assert "long/YES ask=" not in line and "NO-side ask=" not in line, line
    good_line = next(l for l in ctx.splitlines() if l.startswith("watch-good"))
    assert "long/YES ask=0.5750" in good_line and "NO-side ask=0.4300" in good_line
    # The recheck prompt teaches both markers, so the child sees the contract.
    import mlb_lineup_watchlist as mlw
    prompt = mlw.build_recheck_prompt(Path("/tmp/x.json"), entries, {})
    assert "PRICE UNAVAILABLE this cycle" in prompt
    assert "DATA DEFECT" in prompt


def test_lineup_outage_prompt_defers_instead_of_failing_gate(monkeypatch):
    # 08-23 approval note 2: after PR #48 the price side deferred on an outage
    # while the lineup side still said "Fail the lineup-confirmation gate" —
    # a terminal passed, the discard shape that swallowed the 08-16/08-18
    # winners. A feed outage is machine-verified unavailability on both sides:
    # the ids are deferral-eligible and the instruction must be defer.
    import vig_review_gate_common as g

    monkeypatch.setattr(g, "fetch_market_price", lambda slug: {
        "slug": slug, "open": True, "reason": "open",
        "long_ask": "0.5750", "no_ask": "0.4300", "book_state": "reliable",
    })
    monkeypatch.setattr(
        g,
        "fetch_lineup_snapshot",
        lambda entry: (_ for _ in ()).throw(RuntimeError("lineup feed down")),
    )
    entries = [{"id": "watch-a", "polymarket_slug": "aec-mlb-good-2026-08-23", "thesis": ""}]
    prompt, eligible = g.build_lineup_recheck_prompt(Path("/tmp/x.json"), entries)

    assert g.LINEUP_UNAVAILABLE_MARKER in prompt
    assert "keep status pending_lineup_recheck" in g.LINEUP_UNAVAILABLE_MARKER
    assert "Fail the lineup-confirmation gate" not in prompt
    assert eligible == {"watch-a"}
