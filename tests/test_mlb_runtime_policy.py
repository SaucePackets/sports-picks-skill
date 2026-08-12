import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_runtime_policy.py"
spec = importlib.util.spec_from_file_location("mlb_runtime_policy_test", SCRIPT_PATH)
assert spec is not None
mlb_runtime_policy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["mlb_runtime_policy_test"] = mlb_runtime_policy
spec.loader.exec_module(mlb_runtime_policy)


def _write_flag(state: Path, **overrides):
    flag = {
        "schema": "vig-standing-authorization-v1",
        "enabled": True,
        "scope": "MLB Polymarket US moneyline only",
    }
    flag.update(overrides)
    (state / "standing_authorization.json").write_text(json.dumps(flag))


class MlbRuntimePolicyTests(unittest.TestCase):
    def test_missing_flag_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(Path(tmp)))

    def test_explicit_flag_enables_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_flag(state)
            self.assertTrue(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_disabled_flag_suspends_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_flag(state, enabled=False)
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_wrong_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_flag(state, schema="something-else")
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_corrupt_flag_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "standing_authorization.json").write_text("{not json")
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))

    def test_prose_policy_files_no_longer_grant_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "policy.md").write_text(
                "## Standing authorization\n"
                "Current market path: Polymarket sports moneyline where exact mapping is verified\n"
            )
            (state / "risk_limits.md").write_text(
                "Auto-entry: only standing-authorized MLB Polymarket moneyline candidates\n"
            )
            self.assertFalse(mlb_runtime_policy.standing_authorization_enabled(state))


def _write_policy(state: Path, **overrides):
    block = {
        "schema": "vig-mlb-selection-policy-v1",
        "policy_version": "2026-08-11-hardening-pr1",
        "effective_at": "2026-08-11T00:00:00Z",
        "min_conservative_edge": 0.05,
        "max_mlb_official_bets_per_day": 2,
        "starter_pending_promotions_enabled": False,
        "max_small_bets_per_day_probation": 1,
    }
    for key, value in overrides.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    (state / "risk_limits.json").write_text(json.dumps({"mlb_selection_policy": block}))


def _trail_candidate(**overrides):
    candidate = {
        "dk_fair_prob": 0.55,
        "raw_probability": 0.57,
        "uncertainty_haircut": 0.03,
        "conservative_probability": 0.54,
        "current_ask": 0.48,
        "projected_edge_at_current_ask": 0.06,
        "model_version": "market-only-fallback-v1",
    }
    candidate.update(overrides)
    return candidate


class MlbSelectionPolicyLoaderTests(unittest.TestCase):
    def test_loads_rollout_defaults_from_policy_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_policy(state)
            policy = mlb_runtime_policy.load_mlb_selection_policy(state)
            self.assertIsNotNone(policy)
            self.assertEqual(policy.min_conservative_edge, 0.05)
            self.assertEqual(policy.max_mlb_official_bets_per_day, 2)
            self.assertFalse(policy.starter_pending_promotions_enabled)
            self.assertEqual(policy.max_small_bets_per_day_probation, 1)
            self.assertEqual(policy.policy_version, "2026-08-11-hardening-pr1")

    def test_missing_block_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "risk_limits.json").write_text(json.dumps({"daily_cap_usd": 90}))
            self.assertIsNone(mlb_runtime_policy.load_mlb_selection_policy(state))

    def test_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(mlb_runtime_policy.load_mlb_selection_policy(Path(tmp)))

    def test_wrong_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_policy(state, schema="other")
            self.assertIsNone(mlb_runtime_policy.load_mlb_selection_policy(state))

    def test_invalid_values_fail_closed(self):
        for key, bad in (
            ("min_conservative_edge", 0),
            ("min_conservative_edge", 1),
            ("min_conservative_edge", "0.05"),
            ("max_mlb_official_bets_per_day", 0),
            ("max_mlb_official_bets_per_day", 2.5),
            ("max_small_bets_per_day_probation", -1),
            ("starter_pending_promotions_enabled", "false"),
            ("policy_version", ""),
            ("effective_at", ""),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                _write_policy(state, **{key: bad})
                self.assertIsNone(
                    mlb_runtime_policy.load_mlb_selection_policy(state),
                    msg=f"{key}={bad!r} must fail closed",
                )

    def test_ceiling_is_conservative_probability_minus_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_policy(state)
            policy = mlb_runtime_policy.load_mlb_selection_policy(state)
            self.assertAlmostEqual(policy.ceiling_for(0.54), 0.49)


class ProbabilityContractTests(unittest.TestCase):
    def test_complete_trail_has_no_errors(self):
        self.assertEqual(
            mlb_runtime_policy.stale_probability_field_errors(_trail_candidate()), []
        )

    def test_missing_numeric_fields_are_rejected(self):
        for field in mlb_runtime_policy.REQUIRED_EXECUTION_NUMERIC_FIELDS:
            candidate = _trail_candidate()
            candidate.pop(field)
            errors = mlb_runtime_policy.stale_probability_field_errors(candidate)
            self.assertTrue(
                any(field in message for message in errors),
                msg=f"missing {field} must be rejected: {errors}",
            )

    def test_non_numeric_and_out_of_range_fields_are_rejected(self):
        for bad in ("0.54", True, 0, 1, -0.1, 1.5):
            errors = mlb_runtime_policy.stale_probability_field_errors(
                _trail_candidate(conservative_probability=bad)
            )
            self.assertTrue(errors, msg=f"conservative_probability={bad!r}")

    def test_non_finite_fields_are_rejected(self):
        # Root-parser regression: NaN/Inf must be rejected by the strict
        # helpers so no downstream guard can treat a poisoned probability as
        # meeting a floor (all NaN comparisons are false).
        for field in mlb_runtime_policy.REQUIRED_EXECUTION_NUMERIC_FIELDS:
            for bad in (float("nan"), float("inf"), float("-inf")):
                errors = mlb_runtime_policy.stale_probability_field_errors(
                    _trail_candidate(**{field: bad})
                )
                self.assertTrue(
                    errors,
                    msg=f"{field}={bad} must be rejected by the contract",
                )

    def test_missing_model_version_is_rejected(self):
        errors = mlb_runtime_policy.stale_probability_field_errors(
            _trail_candidate(model_version="")
        )
        self.assertIn("model_version must be a non-empty string", errors)

    def test_stale_stored_edge_is_rejected(self):
        # Morning edge no longer matches the live recomputation: price moved.
        errors = mlb_runtime_policy.stale_probability_field_errors(
            _trail_candidate(projected_edge_at_current_ask=0.09)
        )
        self.assertTrue(any("stale" in message for message in errors))

    def test_live_edge_recomputation(self):
        self.assertAlmostEqual(
            mlb_runtime_policy.live_conservative_edge(_trail_candidate()), 0.06
        )
        self.assertIsNone(
            mlb_runtime_policy.live_conservative_edge(_trail_candidate(current_ask=None))
        )


class DailyCandidateLimitTests(unittest.TestCase):
    def _policy(self, max_bets=2):
        return mlb_runtime_policy.MlbSelectionPolicy(
            min_conservative_edge=0.05,
            max_mlb_official_bets_per_day=max_bets,
            starter_pending_promotions_enabled=False,
            max_small_bets_per_day_probation=1,
            policy_version="test",
            effective_at="2026-08-11T00:00:00Z",
        )

    def test_third_qualified_candidate_is_rejected_by_rank(self):
        candidates = [
            _trail_candidate(conservative_probability=0.55, current_ask=0.48,
                             projected_edge_at_current_ask=0.07, side="LOW"),
            _trail_candidate(conservative_probability=0.60, current_ask=0.48,
                             projected_edge_at_current_ask=0.12, side="HIGH"),
            _trail_candidate(conservative_probability=0.57, current_ask=0.48,
                             projected_edge_at_current_ask=0.09, side="MID"),
        ]
        kept, rejected = mlb_runtime_policy.enforce_daily_candidate_limit(
            candidates, self._policy(max_bets=2)
        )
        self.assertEqual([c["side"] for c in kept], ["HIGH", "MID"])
        self.assertEqual([c["side"] for c in rejected], ["LOW"])

    def test_candidate_without_live_edge_is_rejected_outright(self):
        candidates = [
            _trail_candidate(side="OK"),
            _trail_candidate(current_ask=None, side="BROKEN"),
        ]
        kept, rejected = mlb_runtime_policy.enforce_daily_candidate_limit(
            candidates, self._policy()
        )
        self.assertEqual([c["side"] for c in kept], ["OK"])
        self.assertEqual([c["side"] for c in rejected], ["BROKEN"])

    def test_within_limit_keeps_everything(self):
        candidates = [_trail_candidate(side="A"), _trail_candidate(side="B")]
        kept, rejected = mlb_runtime_policy.enforce_daily_candidate_limit(
            candidates, self._policy()
        )
        self.assertEqual(len(kept), 2)
        self.assertEqual(rejected, [])


if __name__ == "__main__":
    unittest.main()
