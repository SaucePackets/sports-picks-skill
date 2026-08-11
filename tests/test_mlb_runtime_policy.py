import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mlb_runtime_policy.py"
spec = importlib.util.spec_from_file_location("mlb_runtime_policy_test", SCRIPT_PATH)
assert spec is not None
mlb_runtime_policy = importlib.util.module_from_spec(spec)
assert spec.loader is not None
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


class MlbPolicyLoaderTests(unittest.TestCase):
    def _write(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload))

    def test_absent_file_fails_closed_to_conservative_defaults(self):
        policy = mlb_runtime_policy.load_mlb_policy(Path("/nonexistent/risk_limits.json"))
        self.assertEqual(policy["min_conservative_edge"], 0.05)
        self.assertEqual(policy["max_mlb_official_bets_per_day"], 2)
        self.assertIs(policy["starter_pending_promotions_enabled"], False)
        self.assertEqual(policy["max_small_bets_per_day_during_probation"], 1)
        self.assertEqual(policy["policy_version"], "vig-mlb-policy-v1")

    def test_valid_section_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_limits.json"
            self._write(
                path,
                {
                    "mlb_policy": {
                        "min_conservative_edge": 0.06,
                        "max_mlb_official_bets_per_day": 3,
                        "starter_pending_promotions_enabled": True,
                        "max_small_bets_per_day_during_probation": 2,
                        "policy_version": "vig-mlb-policy-v2",
                        "policy_effective_at": "2026-08-12T00:00:00Z",
                    }
                },
            )
            policy = mlb_runtime_policy.load_mlb_policy(path)
            self.assertEqual(policy["min_conservative_edge"], 0.06)
            self.assertEqual(policy["max_mlb_official_bets_per_day"], 3)
            self.assertIs(policy["starter_pending_promotions_enabled"], True)
            self.assertEqual(policy["max_small_bets_per_day_during_probation"], 2)
            self.assertEqual(policy["policy_version"], "vig-mlb-policy-v2")
            self.assertEqual(policy["policy_effective_at"], "2026-08-12T00:00:00Z")

    def test_malformed_values_fail_closed_per_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_limits.json"
            self._write(
                path,
                {
                    "mlb_policy": {
                        "min_conservative_edge": "0.05",
                        "max_mlb_official_bets_per_day": 0,
                        "starter_pending_promotions_enabled": "yes",
                        "max_small_bets_per_day_during_probation": -1,
                    }
                },
            )
            policy = mlb_runtime_policy.load_mlb_policy(path)
            self.assertEqual(policy["min_conservative_edge"], 0.05)
            self.assertEqual(policy["max_mlb_official_bets_per_day"], 2)
            self.assertIs(policy["starter_pending_promotions_enabled"], False)
            self.assertEqual(policy["max_small_bets_per_day_during_probation"], 1)

    def test_corrupt_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_limits.json"
            path.write_text("{not json")
            policy = mlb_runtime_policy.load_mlb_policy(path)
            self.assertEqual(policy["min_conservative_edge"], 0.05)

    def test_executable_price_ceiling_math(self):
        policy = {"min_conservative_edge": 0.05}
        self.assertAlmostEqual(
            mlb_runtime_policy.executable_price_ceiling(0.60, policy), 0.55
        )

    def test_executable_price_ceiling_boundary(self):
        policy = {"min_conservative_edge": 0.05}
        # 0.049 edge-equivalent ceiling stays positive; a probability at or
        # under the floor leaves no positive ceiling and fails closed.
        self.assertIsNotNone(mlb_runtime_policy.executable_price_ceiling(0.051, policy))
        self.assertIsNone(mlb_runtime_policy.executable_price_ceiling(0.05, policy))
        self.assertIsNone(mlb_runtime_policy.executable_price_ceiling(0.04, policy))

    def test_executable_price_ceiling_rejects_bad_probability(self):
        self.assertIsNone(mlb_runtime_policy.executable_price_ceiling("0.60", {"min_conservative_edge": 0.05}))
        self.assertIsNone(mlb_runtime_policy.executable_price_ceiling(1.2, {"min_conservative_edge": 0.05}))

    def test_projected_edge(self):
        self.assertAlmostEqual(mlb_runtime_policy.projected_edge(0.60, 0.55), 0.05)
        self.assertIsNone(mlb_runtime_policy.projected_edge(0.60, "0.55"))
        self.assertIsNone(mlb_runtime_policy.projected_edge(None, 0.55))

    def test_missing_probability_fields(self):
        complete = {
            "dk_fair_prob": 0.55,
            "raw_probability": 0.63,
            "uncertainty_haircut": 0.03,
            "conservative_probability": 0.60,
            "current_ask": 0.51,
            "projected_edge_at_current_ask": 0.09,
            "model_version": "market-prior-v1",
        }
        self.assertEqual(mlb_runtime_policy.missing_probability_fields(complete), [])
        stale = dict(complete, current_ask=None)
        self.assertEqual(mlb_runtime_policy.missing_probability_fields(stale), ["current_ask"])
        no_version = dict(complete, model_version="")
        self.assertEqual(
            mlb_runtime_policy.missing_probability_fields(no_version), ["model_version"]
        )
        self.assertEqual(
            len(mlb_runtime_policy.missing_probability_fields({})),
            len(mlb_runtime_policy.REQUIRED_EXECUTION_PROBABILITY_FIELDS),
        )

    def test_missing_probability_fields_rejects_non_finite(self):
        complete = {
            "dk_fair_prob": 0.55,
            "raw_probability": 0.63,
            "uncertainty_haircut": 0.03,
            "conservative_probability": 0.60,
            "current_ask": 0.51,
            "projected_edge_at_current_ask": 0.09,
            "model_version": "market-prior-v1",
        }
        for bad in (float("nan"), float("inf"), float("-inf")):
            candidate = dict(complete, conservative_probability=bad)
            self.assertIn(
                "conservative_probability",
                mlb_runtime_policy.missing_probability_fields(candidate),
            )
            edge_candidate = dict(complete, projected_edge_at_current_ask=bad)
            self.assertIn(
                "projected_edge_at_current_ask",
                mlb_runtime_policy.missing_probability_fields(edge_candidate),
            )

    def test_missing_probability_fields_rejects_out_of_range(self):
        complete = {
            "dk_fair_prob": 0.55,
            "raw_probability": 0.63,
            "uncertainty_haircut": 0.03,
            "conservative_probability": 0.60,
            "current_ask": 0.51,
            "projected_edge_at_current_ask": 0.09,
            "model_version": "market-prior-v1",
        }
        for bad in (0.0, 1.0, 1.5, -0.2):
            candidate = dict(complete, conservative_probability=bad)
            self.assertIn(
                "conservative_probability",
                mlb_runtime_policy.missing_probability_fields(candidate),
            )
        # Edge is signed and unbounded; only finiteness applies.
        self.assertEqual(
            mlb_runtime_policy.missing_probability_fields(
                dict(complete, projected_edge_at_current_ask=-0.25)
            ),
            [],
        )

    def test_projected_edge_rejects_non_finite(self):
        self.assertIsNone(mlb_runtime_policy.projected_edge(float("nan"), 0.51))
        self.assertIsNone(mlb_runtime_policy.projected_edge(0.60, float("inf")))
        self.assertIsNone(
            mlb_runtime_policy.executable_price_ceiling(
                float("nan"), {"min_conservative_edge": 0.05}
            )
        )


if __name__ == "__main__":
    unittest.main()
