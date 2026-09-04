import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig_review_gate_common.py"
spec = importlib.util.spec_from_file_location("vig_review_gate_common", SCRIPT_PATH)
assert spec is not None
vig_review_gate_common = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["vig_review_gate_common"] = vig_review_gate_common
spec.loader.exec_module(vig_review_gate_common)

import vig_run_journal

# The bare module, deliberately: `vig_review_gate_common` imports it as
# `mlb_game_reads` off its own sys.path entry, and `from scripts import ...`
# would give this test a DIFFERENT module object from the one the gate mutates
# (the dual-import trap, PR #74).
import mlb_game_reads
# Bare, for the same reason: the gate imports `mlb_slate_receipt` off its own
# sys.path entry, and `from scripts import ...` would give these tests a second
# module object whose VERDICT_* constants are equal but whose patches land
# somewhere the gate never looks.
import mlb_slate_receipt

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
    "model_version": "vig-mlb-market-v1",
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

# Sentinel for "the child omitted this field", distinct from a None value the
# child could legitimately write.
_OMITTED = object()



# A schedule whose per-game record is VALID, so the recorder-gap notice stays
# silent. An MLB schedule with no `game_reads` is now a reported defect, which
# is the whole point of the 2026-09-01 fix; a fixture that omits it is testing
# the gap path while claiming to test the quiet path.
def with_recorder_record(payload):
    """Add a VALID per-game record matching whatever the fixture carries.

    These fixtures are about watchlist expiry, child failures and approval
    repair — not about the recorder. Without a record they would each also be
    exercising the recorder-gap path, and their "no extra notices" assertions
    would be asserting the absence of a notice that is now correct to emit.

    The record is generated from the fixture's own candidates and watchlist so
    the coverage counts line up; a hand-written constant would drift the first
    time a fixture grew an entry.
    """
    payload = dict(payload)
    if "game_reads" in payload and "slate_denominator" in payload:
        return payload
    entries = [
        ("candidate", entry) for entry in payload.get("candidates", []) or []
    ] + [
        ("lineup_watchlist", entry) for entry in payload.get("lineup_watchlist", []) or []
    ]
    reads, games = [], []
    for index, (disposition, _entry) in enumerate(entries):
        game_pk = 900000 + index
        event_id = f"4019{game_pk}"
        games.append(
            {"game_pk": game_pk, "event_id": event_id, "away": "Away Club",
             "home": "Home Club"}
        )
        reads.append(
            {
                "game_pk": game_pk,
                "event_id": event_id,
                "away": "Away Club",
                "home": "Home Club",
                "disposition": disposition,
                "dk_fair_prob": {"away": 0.398, "home": 0.602},
                "polymarket_ask": {"away": 0.460, "home": 0.545},
                "raw_probability": {"away": 0.400, "home": 0.610},
                "uncertainty_haircut": 0.02,
                "conservative_probability": {"away": 0.380, "home": 0.590},
                "model_version": "vig-mlb-market-v1",
                "net_edge": {"away": -0.080, "home": 0.045},
                "refusing_rails": [],
            }
        )
    payload.setdefault(
        "slate_denominator",
        {
            "source": "mlb_stage2_scan",
            "fetched_at_utc": "2026-09-01T15:30:00+00:00",
            "games": games,
        },
    )
    payload.setdefault("game_reads", reads)
    return payload


def write_denominator_scan(root, day, payload):
    """Write the independent scan artifact beside a fixture's schedule.

    A valid ``game_reads`` record is no longer enough to make a fixture day
    recorder-clean: the gate cross-checks the record against the scan, which
    the run did not write, because a run that trimmed the reads and the
    denominator together passes every check keyed on the schedule alone.
    A fixture with no scan is a day nobody scanned, and that is a defect.
    """
    games = (payload.get("slate_denominator") or {}).get("games") or []
    path = Path(root) / ".picks" / "tmp" / f"stage2-{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(games), encoding="utf-8")
    return path


def recorded_empty_card(**overrides):
    payload = {
        "candidates": [],
        "lineup_watchlist": [],
        "slate_denominator": {
            "source": "mlb_stage2_scan",
            "fetched_at_utc": "2026-09-01T15:30:00+00:00",
            "games": [],
        },
        "game_reads": [],
    }
    payload.update(overrides)
    return payload


class DeterministicPolicyState:
    """Temp ``VIG_STATE_DIR`` carrying the shared policy and standing auth.

    Every routing test needs it, because ``normalize_review_routing`` fails
    closed without a loadable policy — one setUp, so a second suite of routing
    tests cannot quietly disagree with this one about the rails it runs under.
    """

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


class VigReviewGateCommonTests(DeterministicPolicyState, unittest.TestCase):
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
            [
                "candidate event_id:2|side:NYY was not a targeted candidate and this "
                "review promoted no watchlist entry, so nothing corroborates it"
            ],
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
                "model_version": "vig-mlb-market-v1",
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
                "model_version": "vig-mlb-market-v1",
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
                "model_version": "vig-mlb-market-v1",
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                first_pitch = datetime.now(timezone.utc) + timedelta(minutes=75)
                before_entry = self._watch_entry(
                    side="MIN",
                    game="Minnesota Twins at Chicago Cubs",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat(),
                )
                payload = with_recorder_record(
                    {"candidates": [], "lineup_watchlist": [before_entry]}
                )
                schedule_path.write_text(json.dumps(payload))
                write_denominator_scan(root, day, payload)
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
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
                    # Edit the file the gate handed over instead of rewriting
                    # it from captured dicts: the gate now expires the zombie
                    # on disk BEFORE spawning the child, and a child that
                    # resurrects the pre-expiry copy is an untargeted-entry
                    # edit the transition validator rightly rejects.
                    on_disk = json.loads(schedule_path.read_text())
                    on_disk["lineup_watchlist"] = [
                        passed_entry if entry.get("id") == due_entry["id"] else entry
                        for entry in on_disk["lineup_watchlist"]
                    ]
                    schedule_path.write_text(json.dumps(on_disk))
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
                # The zombie is not merely warned any more: it is expired on
                # disk, terminal and auditable, while the live recheck's
                # result is untouched beside it.
                persisted = json.loads(schedule_path.read_text())
                by_id = {
                    entry["id"]: entry for entry in persisted["lineup_watchlist"]
                }
                self.assertEqual(by_id["watch-zombie"]["status"], "expired")
                self.assertIn("expired_at_utc", by_id["watch-zombie"])
                self.assertIn(
                    "still pending_lineup_recheck",
                    by_id["watch-zombie"]["expired_reason"],
                )
                self.assertEqual(by_id[due_entry["id"]]["status"], "passed")
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
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

    def test_next_morning_typo_is_reported_by_run_gate(self):
        # PR #56 review. The +3-day case above was already caught; this is the
        # one that was not. A +1 day typo onto a morning game sat inside the old
        # 36h lead and was silent to BOTH detectors — empty stdout, exit 0 — so
        # the invisible entry survived the function written to end it. One day is
        # the likeliest date typo, and 11:05 CT is an ordinary start (12:05pm ET
        # getaway day), so this window was never empty.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                chicago = ZoneInfo("America/Chicago")
                # The gate's own function, so the day this test writes and the
                # day the gate reads cannot straddle Chicago midnight.
                day = vig_review_gate_common.schedule_day_now()
                today = date.fromisoformat(day)
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                first_pitch = vig_review_gate_common.datetime.combine(
                    today + timedelta(days=1),
                    time(11, 5),
                    tzinfo=chicago,
                ).astimezone(timezone.utc)
                mistyped = self._watch_entry(
                    id="watch-next-morning",
                    side="SEA",
                    game="Seattle Mariners at Texas Rangers",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat(),
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
                self.assertIn("first pitch on watch-next-morning cannot belong", printed)
                # Still invisible to the overdue sibling — this notice is the
                # only thing between the entry and nobody ever seeing it.
                self.assertNotIn("lineup recheck overdue", printed)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_previous_evening_typo_yields_exactly_one_notice(self):
        # PR #57 review. Removing the 6h lag was right, but it was not free: a
        # D-1 entry satisfies BOTH detectors, so the gate printed the overdue
        # warning AND the unreachable notice for the same entry — and the
        # notice's tail then claimed nothing else would surface it, one line
        # under the thing that just had. Duplication is the alarm-fatigue axis
        # the #53 hoist scoping exists to protect, arriving as repetition
        # instead of volume.
        #
        # The unreachable notice is the survivor because it is strictly more
        # informative: overdue says a deadline passed, unreachable says the
        # window can never open and names the day the entry disagrees with.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                chicago = ZoneInfo("America/Chicago")
                day = vig_review_gate_common.schedule_day_now()
                today = date.fromisoformat(day)
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                # Yesterday 19:05 CT — long past, so the overdue detector fires
                # too. Both are true; only one should print.
                first_pitch = vig_review_gate_common.datetime.combine(
                    today - timedelta(days=1), time(19, 5), tzinfo=chicago
                ).astimezone(timezone.utc)
                mistyped = self._watch_entry(
                    id="watch-yesterday",
                    side="SEA",
                    game="Seattle Mariners at Texas Rangers",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat().replace("+00:00", "Z"),
                    recheck_due_utc=(first_pitch - timedelta(minutes=75))
                    .isoformat()
                    .replace("+00:00", "Z"),
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
                notices = [
                    line for line in printed.splitlines()
                    if "watch-yesterday" in line
                ]
                self.assertEqual(len(notices), 1, printed)
                self.assertIn("cannot belong", notices[0])
                # The overdue warning is suppressed for this entry specifically,
                # not disabled: the unreachable notice already covers it.
                self.assertNotIn("lineup recheck overdue", printed)
                # The retired claim must not come back with it.
                self.assertNotIn("nothing else will surface it", printed)

                # And the instant is rendered in the zone the verdict is about.
                # UTC alone printed the SAME DATE on both sides of the sentence
                # for every entry within ~5-6h of a boundary, which reads as a
                # broken detector.
                local = first_pitch.astimezone(chicago)
                self.assertIn(local.isoformat(), notices[0])
                self.assertIn(local.date().isoformat(), notices[0])
                self.assertNotEqual(local.date().isoformat(), day)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_an_id_less_entry_still_yields_exactly_one_notice(self):
        # PR #59 review. The de-dup was defeated by a MISSING id, in exactly the
        # case the de-dup is about. The unreachable detector keyed on
        # "<missing-id>" while the overdue detector skipped on str(None) ==
        # "None", so the exclusion set could not intersect and both notices
        # printed. Reachable because a previous-day first pitch is already past:
        # _split_watchlist_errors quarantines rather than fails, and run_gate
        # proceeds to the notices. Reviewer's repro was this test with
        # `del entry["id"]`; that is what this is.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                chicago = ZoneInfo("America/Chicago")
                day = vig_review_gate_common.schedule_day_now()
                today = date.fromisoformat(day)
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                first_pitch = vig_review_gate_common.datetime.combine(
                    today - timedelta(days=1), time(19, 5), tzinfo=chicago
                ).astimezone(timezone.utc)
                mistyped = self._watch_entry(
                    id="watch-nameless",
                    side="SEA",
                    game="Seattle Mariners at Texas Rangers",
                    bettable_to_price=105,
                    first_pitch_utc=first_pitch.isoformat().replace("+00:00", "Z"),
                    recheck_due_utc=(first_pitch - timedelta(minutes=75))
                    .isoformat()
                    .replace("+00:00", "Z"),
                )
                del mistyped["id"]
                schedule_path.write_text(
                    json.dumps({"candidates": [], "lineup_watchlist": [mistyped]})
                )

                output = StringIO()
                with (
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
                notices = [
                    line for line in printed.splitlines()
                    if "<missing-id>" in line and "review gate NOTICE" in line
                ]
                self.assertEqual(len(notices), 1, printed)
                self.assertIn("cannot belong", notices[0])
                self.assertNotIn("lineup recheck overdue", printed)
                # And no entry is ever reported under the string "None".
                self.assertNotIn("on None,", printed)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_empty_schedule_still_emits_nothing(self):
        # Hoisting the notice must not make quiet cycles chatty.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                payload = recorded_empty_card()
                schedule_path.write_text(json.dumps(payload))
                write_denominator_scan(root, day, payload)

                output = StringIO()
                with redirect_stdout(output):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                self.assertEqual(output.getvalue(), "")
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    # --- run journal: every gate outcome leaves a dated artifact -----------
    #
    # The lane's observability defect was not that any single path was wrong,
    # it was that only ONE path (a completed review) wrote anything, and it
    # wrote to a single file the next run overwrote. These tests pin the
    # outcomes that previously left nothing behind.

    def _journal_records(self, root, day=None, sport=None):
        day = day or vig_review_gate_common.schedule_day_now()
        records, problems = vig_run_journal.read_records(
            vig_run_journal.journal_path(root, day)
        )
        self.assertEqual(problems, [], "journal must be parseable")
        if sport:
            records = [r for r in records if r["sport"] == sport]
        return records

    def test_journal_records_a_day_with_no_schedule_at_all(self):
        # The 08-12/13/14/19/20 shape: the gate ran and there was no schedule
        # file. Before this, the run left nothing, so a PASS day and a day the
        # cron never fired were the same observation afterwards.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                day = vig_review_gate_common.schedule_day_now()

                output = StringIO()
                with redirect_stdout(output):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                self.assertEqual(output.getvalue(), "")
                records = self._journal_records(root, day)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["outcome"], vig_run_journal.OUTCOME_NO_SCHEDULE)
                self.assertEqual(records[0]["stage"], "schedule_missing")
                self.assertEqual(records[0]["day"], day)
                self.assertEqual(records[0]["sport"], "MLB")
                # And the coverage audit now names the gap rather than
                # inferring it from an absent file.
                self.assertEqual(
                    vig_run_journal.unjournalled_days(root, [day]), []
                )
                self.assertEqual(
                    vig_run_journal.unjournalled_days(root, ["1999-01-01"]), ["1999-01-01"]
                )
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_journal_records_an_explicit_pass_on_an_empty_card(self):
        # Explicit PASS: the slate was collected and produced no card. Silence
        # on stdout is still correct — the artifact is on disk, not in the
        # cron delivery.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                day = vig_review_gate_common.schedule_day_now()
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                payload = recorded_empty_card()
                schedule_path.write_text(json.dumps(payload))
                write_denominator_scan(root, day, payload)

                output = StringIO()
                with redirect_stdout(output):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                self.assertEqual(output.getvalue(), "")
                records = self._journal_records(root, day)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["outcome"], vig_run_journal.OUTCOME_NO_WORK)
                self.assertEqual(records[0]["stage"], "no_reviewable_work")
                self.assertEqual(records[0]["schedule_path"], str(schedule_path))
                # A pass is distinguishable from a missing slate, which is the
                # whole point of having two outcomes rather than one silence.
                self.assertNotEqual(
                    records[0]["outcome"], vig_run_journal.OUTCOME_NO_SCHEDULE
                )
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_journal_appends_rather_than_overwriting_within_a_day(self):
        # latest-action.md's actual defect: the second cycle of the day
        # destroyed the first cycle's evidence. Two runs, two records.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                day = vig_review_gate_common.schedule_day_now()

                with redirect_stdout(StringIO()):
                    vig_review_gate_common.run_gate("MLB")
                    vig_review_gate_common.run_gate("MLB")

                self.assertEqual(len(self._journal_records(root, day)), 2)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_journal_records_a_positive_candidate_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                day = vig_review_gate_common.schedule_day_now()
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
                    redirect_stdout(StringIO()),
                ):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                records = self._journal_records(root, day)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["outcome"], vig_run_journal.OUTCOME_REVIEWED)
                self.assertEqual(records[0]["stage"], "complete")
                self.assertEqual(records[0]["counts"]["candidates"], 1)
                self.assertEqual(records[0]["counts"]["approved"], 1)
                self.assertEqual(records[0]["counts"]["rejected"], 0)
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

    def _run_deferred_noop_gate(self, root, price, lineup_snapshot, extra_entries=()):
        """Drive run_gate with a due watchlist entry the child leaves untouched.

        price / lineup_snapshot configure the machine-verified availability of
        each live input: None price means the fetch failed; an Exception
        lineup_snapshot means the feed fetch raised. Returns (status, output,
        entry, schedule_path).
        """
        # The gate's own function — see schedule_day_now.
        day = vig_review_gate_common.schedule_day_now()
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
            json.dumps(
                {"candidates": [], "lineup_watchlist": [entry, *extra_entries]}
            )
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

    def test_journal_records_a_price_deferral_with_its_source_and_instant(self):
        # "Entry X was not reviewed" is unactionable. The 08-16 winners went
        # unreviewed for a knowable reason nobody could read afterwards, so the
        # record has to name the feed and the instant, not just the id.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                status, _, entry, _ = self._run_deferred_noop_gate(
                    root, price=None, lineup_snapshot=dict(self._RESOLVED_LINEUP_SNAPSHOT)
                )
                self.assertEqual(status, 0)

                records = self._journal_records(root)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["outcome"], vig_run_journal.OUTCOME_REVIEWED)
                deferrals = records[0]["deferrals"]
                self.assertEqual(len(deferrals), 1)
                self.assertEqual(deferrals[0]["id"], entry["id"])
                self.assertEqual(deferrals[0]["source"], vig_run_journal.SOURCE_PRICE_FEED)
                self.assertIn("price unavailable", deferrals[0]["reason"])
                # A timestamp, parseable — a bare "yes it was deferred" cannot
                # tell a one-cycle outage from a lane that has been down a week.
                self.assertTrue(deferrals[0]["observed_at"].endswith("Z"))
                datetime.fromisoformat(deferrals[0]["observed_at"].replace("Z", "+00:00"))
                self.assertEqual(records[0]["counts"]["deferral_eligible"], 1)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_journal_records_a_lineup_feed_deferral_against_the_right_source(self):
        # The two deferral sources must be distinguishable in the record: the
        # remedy for a dead price feed is not the remedy for a dead lineup feed.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                status, _, entry, _ = self._run_deferred_noop_gate(
                    root,
                    price=dict(self._LIVE_QUOTE),
                    lineup_snapshot=Exception("lineup feed unavailable"),
                )
                self.assertEqual(status, 0)

                deferrals = self._journal_records(root)[0]["deferrals"]
                self.assertEqual(
                    [item["source"] for item in deferrals],
                    [vig_run_journal.SOURCE_LINEUP_FEED],
                )
                self.assertEqual(deferrals[0]["id"], entry["id"])
                self.assertIn("lineup feed unavailable", deferrals[0]["reason"])
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_the_two_watchlist_counts_name_two_different_populations(self):
        # watchlist_due is THIS RUN's due set; watchlist_pending_after is every
        # pending entry left on the watchlist. Called "deferred", the second
        # invited subtraction against the first, and two counts over one
        # source whose populations differ silently are indistinguishable from
        # a stale counter (Reviewer, PR #60). This pins them APART: a run with
        # one due entry and one far-future entry must report 1 and 2.
        far_future = datetime.now(timezone.utc) + timedelta(days=2)
        not_due = self._watch_entry(
            id="not-due-today",
            side="SEA",
            game="Seattle Mariners at Texas Rangers",
            first_pitch_utc=far_future.isoformat(),
            recheck_due_utc=(far_future - timedelta(minutes=75)).isoformat(),
            polymarket_slug="aec-mlb-sea-tex-2026-07-19",
        )
        with self._temp_root() as root:
            status, _, _, _ = self._run_deferred_noop_gate(
                root,
                price=None,
                lineup_snapshot=dict(self._RESOLVED_LINEUP_SNAPSHOT),
                extra_entries=[not_due],
            )
            self.assertEqual(status, 0)
            counts = self._journal_records(root)[0]["counts"]

        self.assertEqual(counts["watchlist_due"], 1)
        self.assertEqual(counts["watchlist_pending_after"], 2)

    def test_journal_records_a_failed_review_transition_it_rolled_back(self):
        # A rejected review is exactly the outcome that most needs a durable
        # trace: the schedule on disk is restored to its pre-review state, so
        # afterwards the file itself carries no sign the review ever ran.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                status, _, _, _ = self._run_deferred_noop_gate(
                    root,
                    price=dict(self._LIVE_QUOTE),
                    lineup_snapshot=dict(self._RESOLVED_LINEUP_SNAPSHOT),
                )
                self.assertEqual(status, 1)

                records = self._journal_records(root)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["outcome"], vig_run_journal.OUTCOME_ERROR)
                self.assertEqual(records[0]["stage"], "review_transition")
                self.assertIn("invalid review transition", records[0]["detail"])
                self.assertEqual(records[0]["counts"]["watchlist_due"], 1)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def test_journal_write_failure_is_loud_and_never_changes_the_verdict(self):
        # Deliberate asymmetry: observability must not become a new way for a
        # review to fail. The gate keeps its own verdict and the failure is
        # reported on its own line rather than escalated into an exit code.
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                day = vig_review_gate_common.schedule_day_now()
                # A regular FILE where the journal directory must be, so the
                # append's mkdir raises for a real filesystem reason.
                journal_dir = root / ".picks" / "journal"
                journal_dir.parent.mkdir(parents=True)
                journal_dir.write_text("not a directory")

                output = StringIO()
                with redirect_stdout(output):
                    status = vig_review_gate_common.run_gate("MLB")

                self.assertEqual(status, 0)
                text = output.getvalue()
                self.assertIn("MLB review gate JOURNAL CRITICAL", text)
                self.assertIn(str(vig_run_journal.journal_path(root, day)), text)
                # Nothing else leaked: the failure names itself and stops.
                self.assertNotIn("ERROR", text)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    # --- journal: the ERROR paths, not only the four that already had a test
    #
    # Reviewer, PR #60: twelve of the sixteen journal call sites could each be
    # deleted with the full suite still green, and every one of the twelve was
    # an error path — including child_timeout, the scenario vig_run_journal's
    # own docstring leads with. A feature whose stated purpose is that failed
    # runs stop being invisible cannot leave its failure paths unobserved.
    #
    # One case per stage below, each asserting the STAGE rather than the exit
    # code: nine of these stages return 1, so the exit code cannot say which
    # path ran, and a test that cannot discriminate its causes does not
    # observe the thing it is named for (PR #59, three times over).

    # Enough to make the gate reach the child. The stages below the child do
    # not care what the candidate says, only that there was one.
    _CHILD_REACHING_SCHEDULE = json.dumps(
        {"candidates": [{"event_id": "1", "side": "CWS"}], "lineup_watchlist": []}
    )

    @contextmanager
    def _temp_root(self):
        """A temp ROOT restored on the way out, including on failure."""
        original_root = getattr(vig_review_gate_common, "ROOT")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                setattr(vig_review_gate_common, "ROOT", Path(tmp))
                yield Path(tmp)
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def _journalled_gate(
        self, root, schedule_text, *, run=None, extra_patches=(), scan=None
    ):
        """Drive run_gate("MLB") over schedule_text; return status, stdout, records, path.

        schedule_text is written verbatim rather than dumped, because several
        of these stages are reachable only by a schedule that is not valid
        JSON or not an object at all. `run` replaces subprocess.run, so a case
        can make the child time out, fail to start, exit nonzero, or write a
        reviewed state back over the schedule. `scan` is the schedule payload
        whose denominator should also exist as a scan artifact on disk —
        passed explicitly, because "this day was never scanned" is itself a
        state several of these cases are about.
        """
        day = vig_review_gate_common.schedule_day_now()
        schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
        schedule_path.parent.mkdir(parents=True, exist_ok=True)
        schedule_path.write_text(schedule_text)
        if scan is not None:
            write_denominator_scan(root, day, scan)
        output = StringIO()
        with ExitStack() as stack:
            if run is not None:
                stack.enter_context(
                    patch.object(vig_review_gate_common.subprocess, "run", side_effect=run)
                )
            for patcher in extra_patches:
                stack.enter_context(patcher)
            stack.enter_context(redirect_stdout(output))
            status = vig_review_gate_common.run_gate("MLB")
        records = self._journal_records(root, sport="MLB")
        return status, output.getvalue(), records, schedule_path

    def _assert_single_record(self, records, outcome, stage, *, detail=None):
        self.assertEqual(
            [(record["outcome"], record["stage"]) for record in records],
            [(outcome, stage)],
        )
        if detail is not None:
            self.assertIn(detail, records[0]["detail"])

    def test_journal_records_an_unparseable_schedule(self):
        with self._temp_root() as root:
            status, output, records, path = self._journalled_gate(root, "{not json")
        self.assertEqual(status, 1)
        self.assertIn("invalid schedule JSON", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "schedule_parse",
            detail="invalid schedule JSON",
        )
        self.assertEqual(records[0]["schedule_path"], str(path))

    def test_journal_records_an_empty_legacy_array_as_an_explicit_pass(self):
        # An empty legacy array is a no-work day, not an error: the outcome
        # has to distinguish it from the migration failure below, which shares
        # its shape on disk and nothing else.
        with self._temp_root() as root:
            status, output, records, _ = self._journalled_gate(root, "[]")
        self.assertEqual(status, 0)
        self.assertEqual(output, "")
        self._assert_single_record(
            records, vig_run_journal.OUTCOME_NO_WORK, "schedule_empty"
        )

    def test_journal_records_a_legacy_array_that_needs_migration(self):
        with self._temp_root() as root:
            status, output, records, _ = self._journalled_gate(
                root, json.dumps([{"side": "ABC", "vig_approved": None}])
            )
        self.assertEqual(status, 1)
        self.assertIn("requires migration", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "schedule_legacy_array",
            detail="requires migration",
        )

    def test_journal_records_a_schedule_that_is_neither_object_nor_array(self):
        with self._temp_root() as root:
            status, output, records, _ = self._journalled_gate(root, json.dumps("a string"))
        self.assertEqual(status, 1)
        self.assertIn("expected object or list", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "schedule_type",
            detail="got str",
        )

    def test_journal_records_a_slate_whose_watchlist_cannot_be_read(self):
        # A live invalid entry is a hard gate error (only provably-dead
        # past-pitch entries quarantine), and the run dies before any child
        # is spawned — so the journal is the only trace it ran at all.
        now = datetime.now(timezone.utc)
        bad_entry = self._watch_entry(
            original_price="MIN +119 at DraftKings",
            bettable_to_price="+105",
            first_pitch_utc=(now + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            recheck_due_utc=(now + timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
        )
        with self._temp_root() as root:
            status, output, records, _ = self._journalled_gate(
                root, json.dumps({"candidates": [], "lineup_watchlist": [bad_entry]})
            )
        self.assertEqual(status, 1)
        self.assertIn("original_price must be numeric", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "review_work",
            detail="original_price must be numeric",
        )

    def test_journal_records_a_child_reviewer_that_timed_out(self):
        # The scenario vig_run_journal's docstring leads with: before this
        # slice a timed-out child printed one line to a cron delivery nobody
        # reads and left the day looking like a day the job never fired.
        def timeout(*args, **kwargs):
            raise vig_review_gate_common.subprocess.TimeoutExpired(args[0], 1800)

        with self._temp_root() as root:
            status, output, records, _ = self._journalled_gate(
                root, self._CHILD_REACHING_SCHEDULE, run=timeout
            )
        self.assertEqual(status, 1)
        self.assertIn("child reviewer timed out", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "child_timeout",
            detail="child reviewer timed out",
        )
        # The counts are the work that was pending when the child died —
        # what the run was in the middle of, not what it finished.
        self.assertEqual(records[0]["counts"]["candidates"], 1)

    def test_journal_records_a_child_reviewer_that_could_not_start(self):
        def cannot_start(*args, **kwargs):
            raise OSError("hermes not on PATH")

        with self._temp_root() as root:
            status, output, records, _ = self._journalled_gate(
                root, self._CHILD_REACHING_SCHEDULE, run=cannot_start
            )
        self.assertEqual(status, 1)
        self.assertIn("could not start", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "child_start",
            detail="could not start",
        )

    def test_journal_records_a_child_reviewer_that_exited_nonzero(self):
        def failed(*args, **kwargs):
            return vig_review_gate_common.subprocess.CompletedProcess(
                args[0], 7, stdout="", stderr=""
            )

        with self._temp_root() as root:
            status, output, records, _ = self._journalled_gate(
                root, self._CHILD_REACHING_SCHEDULE, run=failed
            )
        self.assertEqual(status, 7)
        self.assertIn("child reviewer exited 7", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "child_exit",
            detail="child reviewer exited 7",
        )

    def test_journal_records_a_child_that_left_unparseable_reviewed_state(self):
        def corrupt(*args, **kwargs):
            path = args[0]
            self._schedule_path.write_text("{")
            return vig_review_gate_common.subprocess.CompletedProcess(
                path, 0, stdout="", stderr=""
            )

        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            status, output, records, _ = self._journalled_gate(
                root, self._CHILD_REACHING_SCHEDULE, run=corrupt
            )
        self.assertEqual(status, 1)
        self.assertIn("could not validate reviewed state", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "reviewed_state_parse",
            detail="could not validate reviewed state",
        )

    def test_journal_records_a_child_that_replaced_the_object_with_an_array(self):
        def rewrite_as_array(*args, **kwargs):
            self._schedule_path.write_text("[]")
            return vig_review_gate_common.subprocess.CompletedProcess(
                args[0], 0, stdout="", stderr=""
            )

        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            status, output, records, _ = self._journalled_gate(
                root, self._CHILD_REACHING_SCHEDULE, run=rewrite_as_array
            )
        self.assertEqual(status, 1)
        self.assertIn("must remain an object", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "reviewed_state_type",
            detail="must remain an object",
        )

    # The two stages below need a review that gets far enough to be routed,
    # so they carry a full candidate rather than the stub above.
    _ROUTABLE_CANDIDATE = {
        "event_id": "401816156",
        "side": "CWS",
        "unit_size": 18,
        "polymarket_ask": 0.51,
        "vig_approved": None,
        "executed": False,
    }

    def _approving_child(self, **candidate_overrides):
        """A child that approves _ROUTABLE_CANDIDATE, with fields overridable.

        Omitting approved_polymarket_ask is how the routing normalizer is
        driven to fail closed — the same fail-closed path that crashed the
        live gate three runs running on 2026-08-18.
        """
        def review(*args, **kwargs):
            updated = dict(self._ROUTABLE_CANDIDATE)
            updated.update(
                PROBABILITY_TRAIL,
                vig_approved=True,
                vig_notes="All gates hold.",
                approved_polymarket_ask=0.48,
                execution_mode="manual",
                execution_status="pending_manual_fill",
                manual_bet_status="awaiting_jerry",
            )
            updated.update(candidate_overrides)
            for field, value in list(updated.items()):
                if value is _OMITTED:
                    del updated[field]
            self._schedule_path.write_text(
                json.dumps({"candidates": [updated], "lineup_watchlist": []})
            )
            return vig_review_gate_common.subprocess.CompletedProcess(
                args[0], 0, stdout="Vig review complete", stderr=""
            )

        return review

    def test_journal_records_a_routing_normalization_that_failed_closed(self):
        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            status, output, records, _ = self._journalled_gate(
                root,
                json.dumps({"candidates": [self._ROUTABLE_CANDIDATE], "lineup_watchlist": []}),
                run=self._approving_child(approved_polymarket_ask=_OMITTED),
                extra_patches=[
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    )
                ],
            )
        self.assertEqual(status, 1)
        self.assertIn("routing normalization failed closed", output)
        # And the rollback ran, which is precisely why the journal matters
        # here: the restore puts the pre-review bytes back, so afterwards the
        # schedule file carries no sign a review happened at all.
        self.assertIn("pre-review schedule restored", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "routing_normalization",
            detail="routing normalization failed closed",
        )

    def test_a_refused_review_is_archived_before_the_restore_destroys_it(self):
        """The reviewed state a refusal throws away is the evidence about it.

        Diagnosing the 2026-09-03 refusals meant reading Vig's agent session
        database on the VPS, because ``_restore_pre_review_state`` had already
        put the pre-review bytes back over the only copy of what the child
        wrote and nothing under ``.picks`` recorded that a review had been
        refused at all.

        Both halves are asserted on the SAME run: the schedule really was
        restored (so this is the destructive path, not a lucky no-op) and the
        archive holds the reviewer's own object.
        """
        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            status, output, _, schedule_path = self._journalled_gate(
                root,
                json.dumps({"candidates": [self._ROUTABLE_CANDIDATE], "lineup_watchlist": []}),
                run=self._approving_child(approved_polymarket_ask=_OMITTED),
                extra_patches=[
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    )
                ],
            )
            self.assertEqual(status, 1)
            self.assertIn("pre-review schedule restored", output)
            # The live file is the PRE-review state again: no decision on it.
            restored = json.loads(schedule_path.read_text())
            self.assertIsNone(restored["candidates"][0].get("vig_approved"))

            archived = sorted((root / ".picks" / "refused").glob("*.json"))
            self.assertEqual(len(archived), 1, archived)
            payload = json.loads(archived[0].read_text())
            self.assertEqual(payload["stage"], "routing_normalization")
            self.assertIn("routing normalization failed closed", payload["detail"])
            self.assertEqual(payload["day"], day)
            self.assertEqual(payload["sport"], "MLB")
            # The reviewer's decision — the thing the restore erased — is here.
            self.assertIs(
                payload["reviewed_schedule"]["candidates"][0]["vig_approved"], True
            )
            self.assertIn(str(archived[0]), output)

    def test_the_archive_holds_the_child_output_not_a_normalized_copy(self):
        """Archive what the reviewer wrote, because that is what is in question.

        ``normalize_review_routing`` legitimately mutates the reviewed state
        before some of its refusals — it stamps ``sport``/``market_type`` and
        rewrites routable candidates — so archiving the live object would file
        a half-normalized artifact under the name of the child's output and
        make the gate look like it had written fields the child did not.
        """
        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            self._journalled_gate(
                root,
                json.dumps({"candidates": [self._ROUTABLE_CANDIDATE], "lineup_watchlist": []}),
                run=self._approving_child(approved_polymarket_ask=_OMITTED),
                extra_patches=[
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    )
                ],
            )
            payload = json.loads(
                sorted((root / ".picks" / "refused").glob("*.json"))[0].read_text()
            )
        reviewed = payload["reviewed_schedule"]
        # The child wrote neither of these; normalization does, and it ran
        # before this refusal was reported.
        self.assertNotIn("sport", reviewed)
        self.assertNotIn("market_type", reviewed)

    def test_an_unwritable_archive_does_not_change_the_verdict(self):
        """Observability that can fail the gate is a new failure mode (PR #60).

        A refusal is a refusal whether or not it could be filed. The run must
        still exit 1 for its own reason, still restore, and say plainly that
        the artifact was lost rather than pretending it exists.
        """
        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            status, output, _, schedule_path = self._journalled_gate(
                root,
                json.dumps({"candidates": [self._ROUTABLE_CANDIDATE], "lineup_watchlist": []}),
                run=self._approving_child(approved_polymarket_ask=_OMITTED),
                extra_patches=[
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    ),
                    patch.object(
                        vig_review_gate_common.Path,
                        "mkdir",
                        side_effect=OSError("read-only file system"),
                    ),
                ],
            )
            self.assertEqual(status, 1)
            self.assertIn("could not archive the refused review", output)
            self.assertIn("routing normalization failed closed", output)
            self.assertIn("pre-review schedule restored", output)
            restored = json.loads(schedule_path.read_text())
            self.assertIsNone(restored["candidates"][0].get("vig_approved"))

    # --- 2026-08-30 cron exit-1 regression pair (LW-20260830-PIT-001 and the
    # malformed Phillies candidate). The two failures shared one cron status
    # but are opposite cases: the Pittsburgh entry is DEAD state that must
    # stop alerting without becoming invisible, while the Phillies review is
    # a LIVE contract violation that must keep failing closed every time.

    def _overdue_pending_entry(self, now, **overrides):
        """A valid pending entry whose recheck window closed over an hour ago.

        90 minutes keeps the Chicago-day straddle window (the pre-existing
        midnight flake shape) as small as the overdue threshold allows.
        """
        first_pitch = now - timedelta(minutes=90)
        return self._watch_entry(
            id="LW-20260830-PIT-001",
            side="PIT",
            game="Pittsburgh Pirates at Cincinnati Reds",
            first_pitch_utc=first_pitch.isoformat().replace("+00:00", "Z"),
            recheck_due_utc=(first_pitch - timedelta(minutes=75))
            .isoformat()
            .replace("+00:00", "Z"),
        )

    def test_a_dead_pending_entry_expires_once_and_then_stays_quiet(self):
        # The Pittsburgh shape: valid, pending, window closed, nothing else on
        # the schedule. Before this fix the entry could never be selected
        # again (due_entries keys on the first-pitch window) yet stayed
        # "pending" on disk, so every remaining cycle of the day re-printed
        # the same overdue warning about a state no run could change.
        now = datetime.now(timezone.utc)
        entry = self._overdue_pending_entry(now)
        no_child = AssertionError("no reviewer child may spawn for dead state")
        with self._temp_root() as root:
            payload = with_recorder_record(
                {"candidates": [], "lineup_watchlist": [entry]}
            )
            status, output, records, path = self._journalled_gate(
                root,
                json.dumps(payload),
                scan=payload,
                run=no_child,
                extra_patches=[
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    )
                ],
            )
            # First run: exit 0 — a notice is never an exit-1 condition —
            # with the transition stated once and kept in the journal.
            self.assertEqual(status, 0)
            self.assertIn("lineup recheck overdue on LW-20260830-PIT-001", output)
            self.assertIn("expired the entry", output)
            persisted = json.loads(path.read_text())
            expired = persisted["lineup_watchlist"][0]
            self.assertEqual(expired["status"], "expired")
            self.assertIn("still pending_lineup_recheck", expired["expired_reason"])
            datetime.fromisoformat(expired["expired_at_utc"].replace("Z", "+00:00"))
            # Auditable no-op, not deletion, and never an execution surface:
            # the entry is intact minus the transition fields, no candidate
            # exists, and nothing execution-shaped appeared on it.
            self.assertEqual(persisted["candidates"], [])
            self.assertNotIn("promoted_candidate", expired)
            for field in ("execution_mode", "execution_status", "executed"):
                self.assertNotIn(field, expired)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["outcome"], vig_run_journal.OUTCOME_NO_WORK)
            self.assertTrue(
                any("expired the entry" in notice for notice in records[0]["notices"])
            )

            # Second run over the SAME persisted state: silence. This is the
            # claim the fix exists for — the dead entry alerts exactly once.
            second_out = StringIO()
            with (
                patch.object(
                    vig_review_gate_common.subprocess, "run", side_effect=no_child
                ),
                patch.object(
                    vig_review_gate_common,
                    "standing_authorization_enabled",
                    return_value=True,
                ),
                redirect_stdout(second_out),
            ):
                second_status = vig_review_gate_common.run_gate("MLB")
            self.assertEqual(second_status, 0)
            self.assertEqual(second_out.getvalue(), "")
            second_records = self._journal_records(root, sport="MLB")
            self.assertEqual(len(second_records), 2)
            self.assertEqual(second_records[1]["notices"], [])

    def test_an_expiry_that_cannot_persist_reverts_and_keeps_the_old_warning(self):
        # Bookkeeping must degrade to the old noise, never change gate
        # behavior: if the expiry cannot reach disk, the in-memory mutation is
        # reverted (so before/after comparisons stay honest), the plain
        # overdue warning still fires, and the run still exits 0.
        now = datetime.now(timezone.utc)
        entry = self._overdue_pending_entry(now)
        with self._temp_root() as root:
            payload = with_recorder_record(
                {"candidates": [], "lineup_watchlist": [entry]}
            )
            status, output, records, path = self._journalled_gate(
                root,
                json.dumps(payload),
                scan=payload,
                run=AssertionError("no reviewer child on a no-work cycle"),
                extra_patches=[
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    ),
                    patch.object(
                        vig_review_gate_common,
                        "persist_schedule_locked",
                        side_effect=OSError("disk full"),
                    ),
                ],
            )
            on_disk = json.loads(path.read_text())["lineup_watchlist"][0]
        self.assertEqual(status, 0)
        self.assertIn("could not persist watchlist expiry", output)
        self.assertIn("lineup recheck overdue on LW-20260830-PIT-001", output)
        self.assertEqual(on_disk["status"], "pending_lineup_recheck")
        self.assertNotIn("expired_at_utc", on_disk)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], vig_run_journal.OUTCOME_NO_WORK)

    def test_the_malformed_phillies_review_fails_closed_and_restores(self):
        # The other half of the 2026-08-30 cron status: the child approved a
        # candidate missing current_ask, projected_edge_at_current_ask, and
        # model_version, with baseball environment as a string. That is an
        # actual unaccepted review — exit 1 is CORRECT here and must name the
        # producer defects, restore the pre-review bytes, and route nothing.
        bad_evidence = valid_baseball_evidence()
        bad_evidence["environment"] = "hot, wind out"
        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            pre_review = json.dumps(
                {"candidates": [self._ROUTABLE_CANDIDATE], "lineup_watchlist": []}
            )
            status, output, records, path = self._journalled_gate(
                root,
                pre_review,
                run=self._approving_child(
                    current_ask=_OMITTED,
                    projected_edge_at_current_ask=_OMITTED,
                    model_version=_OMITTED,
                    baseball_evidence=bad_evidence,
                ),
                extra_patches=[
                    patch.object(
                        vig_review_gate_common,
                        "standing_authorization_enabled",
                        return_value=True,
                    )
                ],
            )
            restored = json.loads(path.read_text())
        self.assertEqual(status, 1)
        self.assertIn("routing normalization failed closed", output)
        for defect in (
            "current_ask must be a number between 0 and 1",
            "projected_edge_at_current_ask must be a number between 0 and 1",
            "model_version must be a non-empty string",
            "environment must be an object",
        ):
            self.assertIn(defect, output)
        # Never invent a probability, price, or evidence object: the reviewed
        # state is rejected wholesale and the pre-review bytes come back.
        self.assertIn("pre-review schedule restored", output)
        self.assertEqual(restored, json.loads(pre_review))
        self.assertIsNone(restored["candidates"][0]["vig_approved"])
        self.assertNotIn("execution_mode", restored["candidates"][0])
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "routing_normalization",
            detail="environment must be an object",
        )

    def test_journal_records_a_reviewed_state_that_could_not_be_persisted(self):
        with self._temp_root() as root:
            day = vig_review_gate_common.schedule_day_now()
            self._schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
            status, output, records, _ = self._journalled_gate(
                root,
                json.dumps({"candidates": [self._ROUTABLE_CANDIDATE], "lineup_watchlist": []}),
                run=self._approving_child(),
                extra_patches=[
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
                ],
            )
        self.assertEqual(status, 1)
        self.assertIn("could not persist reviewed state", output)
        self._assert_single_record(
            records,
            vig_run_journal.OUTCOME_ERROR,
            "persist",
            detail="disk full",
        )

    def test_child_failure_is_concise_and_does_not_echo_child_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
                schedule_path = root / ".picks" / "execute" / f"{day}-schedule.json"
                schedule_path.parent.mkdir(parents=True)
                payload = with_recorder_record(
                    {
                        "candidates": [{"event_id": "1", "side": "CWS"}],
                        "lineup_watchlist": [],
                    }
                )
                schedule_path.write_text(json.dumps(payload))
                write_denominator_scan(root, day, payload)
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
                # Derived from the gate's OWN function, not a second clock
                # call: two independent now() calls straddling Chicago midnight
                # write one day's schedule file and read another.
                day = vig_review_gate_common.schedule_day_now()
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


def _promotion_watch_entry(**overrides):
    """A pending watchlist entry in the shape the 2026-09-03 live one had."""
    entry = {
        "id": "LW20260903-TB-001",
        "first_pitch_utc": "2026-09-04T00:05:00Z",
        "recheck_due_utc": "2026-09-03T23:05:00Z",
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
        "original_price": -120,
        "bettable_to_price": -125,
        "status": "pending_lineup_recheck",
        "slate_probability": {
            "dk_fair_prob": 0.55,
            "raw_probability": 0.57,
            "uncertainty_haircut": 0.03,
            "conservative_probability": 0.54,
            "current_ask": 0.48,
            "projected_edge_at_current_ask": 0.06,
            "model_version": "vig-mlb-market-v1",
        },
    }
    entry.update(overrides)
    return entry


def _promoted_candidate(**overrides):
    """The reviewer's approved object, WITHOUT watchlist_id unless asked.

    That omission is the defect: on 2026-09-03 the child wrote a complete
    ``promoted_candidate`` carrying the id and then appended a ``candidates[]``
    element that had no ``watchlist_id`` key at all.
    """
    candidate = {
        "event_id": "401816794",
        "game": "Tampa Bay Rays at Texas Rangers",
        "side": "Tampa Bay Rays",
        "polymarket_slug": "aec-mlb-tb-tex-2026-09-03",
        **PROBABILITY_TRAIL,
        "vig_approved": True,
        "vig_notes": "All gates hold; both lineups confirmed.",
        "approved_polymarket_ask": 0.48,
        # The REFRESHED signed American price the recheck saw. Without it the
        # promotion has no evidence the price was looked at again.
        "supported_price": -120,
    }
    candidate.update(overrides)
    return candidate


def _promotion_recheck(candidate):
    """The refresh audit a promoted entry owes, agreeing with its candidate."""
    return {
        "lineups_confirmed": True,
        "key_injuries_refreshed": True,
        "price_refreshed": True,
        "all_original_gates_hold": True,
        "material_changes": ["both batting orders moved to confirmed"],
        "probability_change_reasons": {},
        # Confirming the lineups the morning already assumed is a material
        # change that moves no number, and the contract requires that be said
        # rather than left to be inferred.
        "probability_unchanged_justification": (
            "Both orders confirmed exactly the assumed lineups; no component moved."
        ),
        "probability": {
            field: candidate[field]
            for field in (
                "dk_fair_prob",
                "raw_probability",
                "uncertainty_haircut",
                "conservative_probability",
                "current_ask",
                "projected_edge_at_current_ask",
                "model_version",
            )
        },
    }


class PromotionCorroborationTests(DeterministicPolicyState, unittest.TestCase):
    """The 2026-09-03 review-gate refusals, and the rail that replaces them.

    The gate recognised a watchlist promotion only when the ``candidates[]``
    element carried a ``watchlist_id`` — a string the child reviewer copies by
    hand out of the entry it also hand-writes. Two cycles (22:46, 23:01) were
    refused because it omitted the field and the third (23:16) passed because
    it did not, same reviewer, same entry, same prompt.
    """

    @staticmethod
    def _promotion(candidate=None, entry_overrides=None, watch_id="LW20260903-TB-001"):
        candidate = _promoted_candidate() if candidate is None else candidate
        entry = _promotion_watch_entry(id=watch_id)
        promoted = _promotion_watch_entry(
            id=watch_id,
            status="promoted",
            rechecked_at_utc="2026-09-03T22:50:00Z",
            recheck_notes="Both orders confirmed; live ask inside the ceiling.",
            recheck=_promotion_recheck(candidate),
            promoted_candidate=dict(candidate, watchlist_id=watch_id),
        )
        promoted.update(entry_overrides or {})
        before = {"candidates": [], "lineup_watchlist": [entry]}
        after = {"candidates": [candidate], "lineup_watchlist": [promoted]}
        return before, after, candidate, promoted

    def test_a_promotion_missing_watchlist_id_is_corroborated_by_its_entry(self):
        before, after, candidate, promoted = self._promotion()
        # The premise: the reviewer really did omit the field the old rail
        # keyed on. Without this the test could pass for the wrong reason.
        self.assertNotIn("watchlist_id", candidate)

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(candidate["watchlist_id"], "LW20260903-TB-001")
        self.assertEqual(candidate["execution_mode"], "standing_authorized")
        self.assertEqual(promoted["promoted_candidate"], candidate)
        # And the transition validator — which demands exactly one candidate
        # carrying the entry id, equal to promoted_candidate — now agrees. It
        # refused this same pair on 2026-09-03 for the same missing field.
        self.assertEqual(
            vig_review_gate_common.validate_review_transition(
                before, after, [], ["LW20260903-TB-001"], "MLB", True
            ),
            [],
        )

    def test_an_uncorroborated_candidate_is_still_refused(self):
        """The rail stays CLOSED; only the evidence it reads has changed."""
        injected = _promoted_candidate(
            event_id="401899999",
            polymarket_slug="aec-mlb-nyy-bos-2026-09-03",
            side="New York Yankees",
        )
        before, after, _, _ = self._promotion()
        after["candidates"] = [injected]

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no promoted watchlist entry corroborates it", errors[0])
        # The message names what WAS promoted — the half the old message left
        # out, and the reason the live refusal took a session-database read to
        # diagnose.
        self.assertIn("aec-mlb-tb-tex-2026-09-03", errors[0])
        self.assertNotEqual(injected.get("execution_mode"), "standing_authorized")

    def test_a_candidate_on_the_other_side_of_the_same_market_is_refused(self):
        """A slug match is not a bet match: the side is part of the address."""
        before, after, candidate, _ = self._promotion()
        after["candidates"] = [_promoted_candidate(side="Texas Rangers")]

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no promoted watchlist entry corroborates it", errors[0])

    def test_a_club_name_with_no_market_corroborates_nothing(self):
        """A ``promoted_candidate`` that addresses no market is not evidence.

        The entry below promotes TB/TEX but its ``promoted_candidate`` names
        only the club, so the three market fields are absent — skipped, not
        disagreed with. If a bare ``side`` could supply ``agree``, this entry
        would corroborate a candidate for an ENTIRELY DIFFERENT market, and
        the failure would be silent: ``normalize_review_routing`` records the
        promotion against ``entry['game_pk']`` — the watchlist game's pk, not
        the candidate's — so the wrong game's read is relabelled, the carded
        game's read keeps what it said, and the reconciliation identity still
        balances (candidates +1, candidate-reads +1, deferred -1). The count
        that caught the 2026-09-03 defect is blind to this one.
        """
        before, after, _, promoted = self._promotion()
        promoted["promoted_candidate"] = {"side": "Tampa Bay Rays"}
        elsewhere = _promoted_candidate(
            event_id="401899999",
            polymarket_slug="aec-mlb-tb-nyy-2026-09-04",
        )
        # The premise: same club, different market, and nothing to stamp on.
        self.assertEqual(elsewhere["side"], promoted["promoted_candidate"]["side"])
        self.assertNotIn("watchlist_id", elsewhere)
        after["candidates"] = [elsewhere]

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no promoted watchlist entry corroborates it", errors[0])
        self.assertNotEqual(elsewhere.get("execution_mode"), "standing_authorized")
        # And the entry was not overwritten with the candidate it did not
        # address — the laundering step that would erase the mismatch.
        self.assertEqual(promoted["promoted_candidate"], {"side": "Tampa Bay Rays"})

    def test_a_market_with_no_side_does_not_corroborate_the_opposite_wager(self):
        """The mirror of the club-name defect, and the better-hidden half.

        Here the entry's ``promoted_candidate`` carries the slug, the event_id
        and the watchlist_id but never restates the side. Both of those fields
        are per-GAME — ``aec-mlb-tb-tex-2026-09-03`` and an ESPN game id name
        the matchup, not the wager — so they address the Texas candidate exactly
        as well as the Tampa Bay one the entry actually promoted.

        Nothing downstream catches it, and unlike the club-name case nothing is
        even off by one: ``entry['game_pk']`` is the RIGHT game, so the read
        that gets relabelled is the correct read and the reconciliation
        identity balances exactly. ``validate_entry`` never mentions ``side``
        on ``promoted_candidate``. The only thing wrong is that the opposite
        bet is the one routed to automatic execution.
        """
        before, after, candidate, promoted = self._promotion()
        promoted["promoted_candidate"].pop("side")
        # The premise: still a complete market address, and still stamped.
        self.assertEqual(
            promoted["promoted_candidate"]["polymarket_slug"],
            "aec-mlb-tb-tex-2026-09-03",
        )
        self.assertEqual(
            promoted["promoted_candidate"]["watchlist_id"], "LW20260903-TB-001"
        )
        self.assertNotIn("side", promoted["promoted_candidate"])
        mirror = _promoted_candidate(side="Texas Rangers")
        # Same game, other side, and nothing for the rail to fall back on.
        self.assertEqual(mirror["polymarket_slug"], "aec-mlb-tb-tex-2026-09-03")
        self.assertNotEqual(mirror["side"], candidate["side"])
        self.assertNotIn("watchlist_id", mirror)
        after["candidates"] = [mirror]

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("no promoted watchlist entry corroborates it", errors[0])
        # Not routed, not stamped...
        self.assertNotEqual(mirror.get("execution_mode"), "standing_authorized")
        self.assertNotIn("watchlist_id", mirror)
        # ...and the entry was NOT overwritten with the wager it did not
        # promote. Without this the entry ends up asserting it promoted Texas.
        self.assertNotIn("side", promoted["promoted_candidate"])
        self.assertNotEqual(
            promoted["promoted_candidate"].get("side"), "Texas Rangers"
        )

    def test_a_candidate_two_promotions_corroborate_is_refused(self):
        """An ambiguous pairing has no fact to route on, so it fails closed."""
        candidate = _promoted_candidate()
        twin = _promotion_watch_entry(
            id="LW20260903-TB-002",
            status="promoted",
            rechecked_at_utc="2026-09-03T22:50:00Z",
            recheck_notes="Duplicate entry for the same game.",
            promoted_candidate=dict(candidate, watchlist_id="LW20260903-TB-002"),
        )
        before, after, _, _ = self._promotion(candidate=candidate)
        before["lineup_watchlist"].append(
            _promotion_watch_entry(id="LW20260903-TB-002")
        )
        after["lineup_watchlist"].append(twin)

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("corroborated by more than one promoted watchlist entry", errors[0])
        self.assertNotIn("watchlist_id", after["candidates"][0])

    def test_a_stamped_watchlist_id_its_own_entry_contradicts_is_refused(self):
        """A hand-copied id may not be trusted against positive contradiction.

        Without this the mis-stamp launders itself: normalization overwrites
        the named entry's ``promoted_candidate`` with whatever candidate
        claimed it, so the equality check in ``validate_review_transition``
        compares the forgery against itself and passes.
        """
        before, after, _, promoted = self._promotion()
        after["candidates"] = [
            _promoted_candidate(
                watchlist_id="LW20260903-TB-001",
                polymarket_slug="aec-mlb-nyy-bos-2026-09-03",
                side="New York Yankees",
                event_id="401899999",
            )
        ]

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("addresses a different bet", errors[0])
        # And the entry was NOT overwritten with the contradicting candidate.
        self.assertEqual(
            promoted["promoted_candidate"]["polymarket_slug"],
            "aec-mlb-tb-tex-2026-09-03",
        )

    def test_a_stamped_watchlist_id_naming_no_promotion_is_still_refused(self):
        before, after, _, _ = self._promotion()
        after["candidates"] = [_promoted_candidate(watchlist_id="LW20260903-XX-999")]

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("not a promoted entry this review created", errors[0])

    def test_address_agreement_separates_absence_of_evidence_from_evidence(self):
        """Three answers, not two — ``unknown`` is neither and never rounds."""
        candidate = {
            "polymarket_slug": "aec-mlb-tb-tex-2026-09-03",
            "side": "Tampa Bay Rays",
            "event_id": "401816794",
        }
        cases = [
            ("same slug and side", dict(candidate), "agree"),
            # A club name addresses no market. The same club plays every day,
            # so a bare matching side is an absence of evidence, not agreement.
            ("side only, matching", {"side": "Tampa Bay Rays"}, "unknown"),
            (
                "side matches but the only market field disagrees",
                {"side": "Tampa Bay Rays", "polymarket_slug": "aec-mlb-nyy-bos-2026-09-03"},
                "disagree",
            ),
            # A slug and an event_id are both per-GAME and name no side, so a
            # market without a restated side is exactly as under-specified as a
            # side without a market — it would corroborate the OPPOSITE wager
            # on the same game.
            ("slug only, matching", {"polymarket_slug": candidate["polymarket_slug"]}, "unknown"),
            ("event_id only, matching", {"event_id": candidate["event_id"]}, "unknown"),
            # Both halves present and equal is the only thing that agrees.
            (
                "market and side, no other field",
                {"polymarket_slug": candidate["polymarket_slug"], "side": candidate["side"]},
                "agree",
            ),
            ("slug matches, side differs", dict(candidate, side="Texas Rangers"), "disagree"),
            (
                "slug differs, side matches",
                dict(candidate, polymarket_slug="aec-mlb-nyy-bos-2026-09-03"),
                "disagree",
            ),
            ("event_id differs alone", dict(candidate, event_id="401899999"), "disagree"),
            ("no comparable field", {"game": "Tampa Bay Rays at Texas Rangers"}, "unknown"),
            ("blank strings address nothing", {"polymarket_slug": "  ", "side": ""}, "unknown"),
            ("not an object", None, "unknown"),
        ]
        for label, other, expected in cases:
            with self.subTest(label):
                self.assertEqual(
                    vig_review_gate_common.promotion_address_agreement(candidate, other),
                    expected,
                )

    def test_an_integer_event_id_addresses_the_same_game_as_its_string(self):
        """The two vocabularies do not agree on the JSON type of an id.

        The side is carried on both sides here only so the pair is a complete
        address; the field under test is ``event_id`` and its type.
        """
        self.assertEqual(
            vig_review_gate_common.promotion_address_agreement(
                {"event_id": 401816794, "side": "Tampa Bay Rays"},
                {"event_id": "401816794", "side": "Tampa Bay Rays"},
            ),
            "agree",
        )
        # And the coercion is not doing the work of the side: drop the side and
        # the same matching id is no longer an address.
        self.assertEqual(
            vig_review_gate_common.promotion_address_agreement(
                {"event_id": 401816794, "side": "Tampa Bay Rays"},
                {"event_id": "401816794"},
            ),
            "unknown",
        )


class PromotionRecordsItsGameReadTests(DeterministicPolicyState, unittest.TestCase):
    """The other half of the 2026-09-03 pair: two candidates, one game_read.

    ``card_reconciliation_errors`` reported ``1 game_reads entries say
    'candidate' but the schedule carries 2 candidates`` at 23:30. The morning
    slate owns ``game_reads`` and writes each read once; the promotion path
    moved a game onto the card and nothing updated its read.
    """

    AWAY, HOME = "Tampa Bay Rays", "Texas Rangers"

    def _read(self, **overrides):
        entry = {
            "game_pk": 824901,
            "event_id": "401816794",
            "away": self.AWAY,
            "home": self.HOME,
            "disposition": "lineup_watchlist",
            "dk_fair_prob": {"away": 0.54, "home": 0.46},
            "polymarket_ask": {"away": 0.54, "home": 0.47},
            "raw_probability": {"away": 0.60, "home": 0.40},
            "uncertainty_haircut": 0.0,
            "conservative_probability": {"away": 0.60, "home": 0.40},
            "model_version": "market-handicap-v1",
            "net_edge": {"away": 0.06, "home": -0.07},
            "refusing_rails": [],
        }
        entry.update(overrides)
        return entry

    def _carded_read(self, **overrides):
        return self._read(
            game_pk=824900,
            event_id="401816700",
            away="Toronto Blue Jays",
            home="Cleveland Guardians",
            disposition="candidate",
            **overrides,
        )

    def _schedule(self, reads, entry_overrides=None, candidate=None):
        candidate = _promoted_candidate() if candidate is None else candidate
        entry = _promotion_watch_entry(game_pk=824901)
        promoted = _promotion_watch_entry(
            game_pk=824901,
            status="promoted",
            rechecked_at_utc="2026-09-03T22:50:00Z",
            recheck_notes="Both orders confirmed.",
            promoted_candidate=dict(candidate, watchlist_id="LW20260903-TB-001"),
        )
        promoted.update(entry_overrides or {})
        morning_candidate = {"polymarket_slug": "aec-mlb-tor-cle-2026-09-03", "side": "Toronto Blue Jays"}
        before = {
            "candidates": [morning_candidate],
            "lineup_watchlist": [entry],
            "game_reads": reads,
        }
        after = {
            "candidates": [morning_candidate, candidate],
            "lineup_watchlist": [promoted],
            "game_reads": reads,
        }
        return before, after

    def test_the_live_defect_reproduces_without_the_recorder(self):
        """Premise first: this is the message the 23:30 cron actually printed.

        The corrected identity reports the promoted game twice — once for the
        candidate it now is and once for the deferral it is no longer — and
        both halves are the same un-updated read. On ``main`` only the first
        was visible, because the promoted entry was counted as a deferral and
        its stale ``lineup_watchlist`` read balanced that phantom.
        """
        _, after = self._schedule([self._carded_read(), self._read()])
        errors = mlb_game_reads.card_reconciliation_errors(after)
        self.assertIn(
            "1 game_reads entries say 'candidate' but the schedule carries 2 candidates",
            errors,
        )
        self.assertIn(
            "1 game_reads entries say 'lineup_watchlist' but the schedule carries 0 "
            "un-promoted lineup_watchlist entries",
            errors,
        )

    def test_a_promotion_moves_its_read_onto_the_card(self):
        before, after = self._schedule([self._carded_read(), self._read()])

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [entry["disposition"] for entry in after["game_reads"]],
            ["candidate", "candidate"],
        )
        self.assertEqual(mlb_game_reads.card_reconciliation_errors(after), [])

    def test_a_promotion_whose_read_is_missing_fails_closed(self):
        before, after = self._schedule([self._carded_read()])

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("matches 0 game_reads entries", errors[0])

    def test_a_promotion_whose_read_says_the_card_refused_it_fails_closed(self):
        """A promoted game whose read says ``pass`` is two records disagreeing."""
        before, after = self._schedule(
            [self._carded_read(), self._read(disposition="pass", refusing_rails=["lineups_unconfirmed"])]
        )

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("that read says 'pass'", errors[0])

    def test_a_doubleheader_is_separated_by_game_pk_not_event_id(self):
        """Same clubs, same day, two games: only ``game_pk`` tells them apart."""
        other_game = self._read(game_pk=824902)
        before, after = self._schedule([self._carded_read(), self._read(), other_game])
        # Both reads carry the promoted game's event_id; the entry's stamped
        # game_pk is what selects one.
        self.assertEqual(other_game["event_id"], "401816794")

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(after["game_reads"][1]["disposition"], "candidate")
        self.assertEqual(after["game_reads"][2]["disposition"], "lineup_watchlist")

    def test_an_unstamped_entry_falls_back_to_the_event_id(self):
        """``game_pk`` is optional on a watchlist entry, so it cannot be required."""
        before, after = self._schedule(
            [self._carded_read(), self._read()],
            entry_overrides={"game_pk": None},
        )
        before["lineup_watchlist"][0]["game_pk"] = None

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(errors, [])
        self.assertEqual(after["game_reads"][1]["disposition"], "candidate")

    def test_an_ambiguous_event_id_fallback_fails_closed(self):
        before, after = self._schedule(
            [self._carded_read(), self._read(), self._read(game_pk=824902)],
            entry_overrides={"game_pk": None},
        )
        before["lineup_watchlist"][0]["game_pk"] = None

        errors = vig_review_gate_common.normalize_review_routing(
            before, after, "MLB", mlb_standing_authorized=True
        )

        self.assertEqual(len(errors), 1, errors)
        self.assertIn("matches 2 game_reads entries", errors[0])

    def test_a_schedule_with_no_reads_still_promotes(self):
        """Every schedule written before the recorder shipped carries none."""
        before, after = self._schedule([])
        del before["game_reads"]
        del after["game_reads"]

        self.assertEqual(
            vig_review_gate_common.normalize_review_routing(
                before, after, "MLB", mlb_standing_authorized=True
            ),
            [],
        )


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


def test_journalled_skips_and_deferrals_are_distinguishable_by_kind(monkeypatch):
    # Both land in the same list, and before the kind discriminator the field
    # name said "deferral" for each — so a permanently-broken entry rendered
    # as something to retry (Reviewer, PR #60). Both sides pinned here: a test
    # that only checked the defect could pass with every item marked a defect.
    import vig_review_gate_common as g

    monkeypatch.setattr(g, "fetch_market_price", lambda slug: None)
    recorded = []
    g._price_context(
        [
            {"id": "defect", "thesis": "no slug here"},
            {"id": "outage", "polymarket_slug": "aec-mlb-x-y-2026-07-27", "thesis": ""},
        ],
        recorded,
    )

    by_id = {item["id"]: item for item in recorded}
    assert by_id["defect"]["kind"] == vig_run_journal.KIND_DATA_DEFECT
    assert by_id["outage"]["kind"] == vig_run_journal.KIND_OUTAGE
    # And the rendering follows the kind, which is where a reader sees it.
    rendered = vig_run_journal.format_record(
        vig_run_journal.build_record(
            sport="MLB", day="2026-08-30", outcome=vig_run_journal.OUTCOME_REVIEWED,
            stage="complete", deferrals=recorded,
        )
    )
    assert "skipped: defect" in rendered
    assert "deferred: outage" in rendered


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


class GateWritesTheSlateReceiptTests(DeterministicPolicyState, unittest.TestCase):
    """The receipt must not depend on the component whose failure it catches.

    On 2026-09-04 the producer skipped ``mlb_slate_writer.py`` and skipped
    ``mlb_slate_receipt.py --write`` with it, so the day that most needed a
    recording verdict — sixteen scanned games, no ``game_reads``, no
    ``slate_denominator`` — ended with no receipt on disk at all. Every check in
    the lane worked; the one artifact that carries the answer was written by the
    same run that failed. These pin the ownership move, not the verdicts: what
    a verdict MEANS is `test_mlb_recorder_wiring.SlateReceiptTests`.
    """

    @contextmanager
    def _runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_root = getattr(vig_review_gate_common, "ROOT")
            try:
                root = Path(tmp)
                setattr(vig_review_gate_common, "ROOT", root)
                # The gate's OWN day function, never a second clock call: two
                # now() calls straddling Chicago midnight write one day's file
                # and look for another's receipt.
                yield root, vig_review_gate_common.schedule_day_now()
            finally:
                setattr(vig_review_gate_common, "ROOT", original_root)

    def _receipt(self, root, day):
        path = root / ".picks" / "journal" / f"{day}-slate-receipt.json"
        self.assertTrue(path.exists(), f"no receipt written at {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_2026_09_04_shape_leaves_a_receipt_the_producer_did_not_write(self):
        # The real artifact, reduced: a hand-authored schedule with no reads and
        # no denominator, against a scan that enumerated the card. Nothing here
        # runs mlb_slate_receipt.py — the gate cycle is the only actor.
        with self._runtime() as (root, day):
            schedule = root / ".picks" / "execute" / f"{day}-schedule.json"
            schedule.parent.mkdir(parents=True)
            schedule.write_text(json.dumps({
                "date": day, "sport": "MLB", "market_type": "moneyline",
                "candidates": [], "lineup_watchlist": [],
            }))
            scan = root / ".picks" / "tmp" / f"stage2-{day}.json"
            scan.parent.mkdir(parents=True)
            scan.write_text(json.dumps([
                {"game_pk": 824424, "event_id": "401877193",
                 "away": "Detroit Tigers", "home": "Cleveland Guardians"},
                {"game_pk": 824387, "event_id": "401816801",
                 "away": "Detroit Tigers", "home": "Cleveland Guardians"},
            ]))

            with redirect_stdout(StringIO()):
                status = vig_review_gate_common.run_gate("MLB")

            self.assertEqual(status, 0)
            receipt = self._receipt(root, day)
            self.assertEqual(
                receipt["verdict"], mlb_slate_receipt.VERDICT_RECORDER_FAILED
            )
            # The count comes from the SCAN, which the run did not write. A
            # denominator-shaped zero would have said 0 games and 0 reads.
            self.assertEqual(receipt["scheduled_games"], 2)
            self.assertEqual(receipt["reads_recorded"], 0)

    def test_a_day_with_no_schedule_at_all_still_leaves_a_receipt(self):
        # The early return. "The producer never ran" and "the producer ran and
        # recorded nothing" are different days, and a receipt written only on
        # the paths that reach the bottom of run_gate would leave the first one
        # looking exactly like a machine that was switched off.
        with self._runtime() as (root, day):
            with redirect_stdout(StringIO()):
                status = vig_review_gate_common.run_gate("MLB")

            self.assertEqual(status, 0)
            self.assertEqual(
                self._receipt(root, day)["verdict"],
                mlb_slate_receipt.VERDICT_NO_SCHEDULE,
            )

    def test_a_receipt_is_written_even_when_the_gate_raises(self):
        # The `finally`, asserted through the MECHANISM rather than an outcome
        # it shares with an ordinary cycle: the failure is injected inside
        # _run_gate, so a receipt on disk here can only have come from the
        # unwind path. An unexpected exception is exactly when a run leaves the
        # least evidence, which is when the day's verdict is worth the most.
        with self._runtime() as (root, day):
            boom = RuntimeError("gate exploded mid-cycle")
            with patch.object(
                vig_review_gate_common, "_schedule_path", side_effect=boom
            ):
                with redirect_stdout(StringIO()):
                    with self.assertRaises(RuntimeError):
                        vig_review_gate_common.run_gate("MLB")

            self.assertEqual(
                self._receipt(root, day)["verdict"],
                mlb_slate_receipt.VERDICT_NO_SCHEDULE,
            )

    def test_the_soccer_gate_does_not_write_an_mlb_receipt(self):
        # The receipt is an MLB artifact keyed on the MLB schedule path. A
        # soccer cycle writing one would file a no_schedule verdict against a
        # sport whose schedule it never looked for, and it would do so ninety-six
        # times a day over any MLB receipt already on disk.
        with self._runtime() as (root, day):
            with redirect_stdout(StringIO()):
                status = vig_review_gate_common.run_gate("intl-soccer")

            self.assertEqual(status, 0)
            self.assertFalse(
                (root / ".picks" / "journal" / f"{day}-slate-receipt.json").exists()
            )

    def test_a_receipt_that_cannot_be_written_does_not_fail_the_review(self):
        # Same asymmetry as the run journal: the gate's verdict is
        # authoritative, and taking the reviewer offline because a measurement
        # artifact could not be persisted would add an outage mode to the lane
        # whose actual problem is losing work silently.
        with self._runtime() as (root, day):
            output = StringIO()
            with patch.object(
                vig_review_gate_common.mlb_slate_receipt,
                "write_receipt",
                side_effect=OSError("read-only filesystem"),
            ):
                with redirect_stdout(output):
                    status = vig_review_gate_common.run_gate("MLB")

            self.assertEqual(status, 0)
            self.assertIn("RECEIPT CRITICAL", output.getvalue())
            self.assertIn("read-only filesystem", output.getvalue())
            self.assertFalse(
                (root / ".picks" / "journal" / f"{day}-slate-receipt.json").exists()
            )

    def test_the_receipt_and_the_journal_do_not_disagree_about_one_day(self):
        # Two artifacts, one file, and they must not be a second opinion about
        # each other: the gate journals `recorder_failed` from
        # validate_with_denominator and the receipt reaches its verdict from the
        # same functions. A day where the journal says the record is broken and
        # the receipt says `complete` would leave a reader no way to decide
        # which one to believe.
        with self._runtime() as (root, day):
            schedule = root / ".picks" / "execute" / f"{day}-schedule.json"
            schedule.parent.mkdir(parents=True)
            schedule.write_text(json.dumps({
                "date": day, "sport": "MLB", "market_type": "moneyline",
                "candidates": [], "lineup_watchlist": [],
            }))
            scan = root / ".picks" / "tmp" / f"stage2-{day}.json"
            scan.parent.mkdir(parents=True)
            scan.write_text(json.dumps([
                {"game_pk": 823418, "event_id": "401816115",
                 "away": "Philadelphia Phillies", "home": "New York Mets"},
            ]))

            with redirect_stdout(StringIO()):
                vig_review_gate_common.run_gate("MLB")

            records, _errors = vig_run_journal.read_records(
                vig_run_journal.journal_path(root, day)
            )
            outcomes = {r.get("outcome") for r in records if r.get("sport") == "MLB"}
            self.assertIn(vig_run_journal.OUTCOME_RECORDER_FAILED, outcomes)
            self.assertEqual(
                self._receipt(root, day)["verdict"],
                mlb_slate_receipt.VERDICT_RECORDER_FAILED,
            )
