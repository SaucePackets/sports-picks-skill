import copy
import json
from pathlib import Path
import tempfile
import unittest
import sys
import vig_policy_state

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mlb_candidate_contract import candidate_errors
from mlb_probability_model import probability_component_errors


def market_candidate():
    return dict(
        model_version="vig-mlb-market-v1",
        dk_fair_prob=0.60,
        raw_probability=0.60,
        conservative_probability=0.60,
        uncertainty_haircut=0,
        current_ask=0.54,
        projected_edge_at_current_ask=0.06,
        probability_components=dict(adjustments=[], haircuts=[]),
        confidence="Medium",
        unit_size=15,
    )


class CandidateContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.limits = {
            "mlb_policy": vig_policy_state.policy_block(),
            "max_unit_usd": {"small": 9, "medium": 15, "high": 25},
            "mlb_deployed_models": {
                "schema": "vig-mlb-deployed-models-v1",
                "versions": ["test-model"],
            },
        }
        (self.state / "risk_limits.json").write_text(json.dumps(self.limits))

    def check(self, candidate):
        return candidate_errors(candidate, state_dir=self.state)

    def test_valid_market_and_admitted_component_models(self):
        candidate = market_candidate()
        self.assertEqual(self.check(candidate), [])
        candidate.update(model_version="test-model", dk_fair_prob=0.58)
        candidate["probability_components"]["adjustments"] = [
            dict(
                component="starter_run_prevention",
                delta=0.02,
                evidence="Pregame season FIP advantage",
            )
        ]
        self.assertEqual(self.check(candidate), [])
        candidate["model_version"] = "unadmitted-model"
        self.assertTrue(any("not deployed" in e for e in self.check(candidate)))

    def test_false_market_label_rejected_even_when_arithmetic_reconciles(self):
        candidate = market_candidate()
        candidate["dk_fair_prob"] = 0.58
        candidate["probability_components"]["adjustments"] = [
            dict(
                component="starter_run_prevention",
                delta=0.02,
                evidence="Pregame season FIP advantage",
            )
        ]
        self.assertTrue(any("market-only model" in e for e in self.check(candidate)))
        candidate["model_version"] = "test-model"
        self.assertEqual(self.check(candidate), [])

    def test_market_semantics_are_stricter_than_arithmetic_tolerance(self):
        candidate = market_candidate()
        candidate["raw_probability"] += 0.0001
        self.assertTrue(
            any(
                "market-only model" in e
                for e in probability_component_errors(candidate)
            )
        )

    def test_each_tier_boundary_and_nonfinite_size(self):
        for tier, cap in self.limits["max_unit_usd"].items():
            with self.subTest(tier=tier):
                candidate = market_candidate()
                candidate.update(confidence=tier, unit_size=cap)
                self.assertEqual(self.check(candidate), [])
                candidate["unit_size"] = cap + 0.01
                self.assertTrue(any("exceeds cap" in e for e in self.check(candidate)))
        for bad in (True, "15", 0, -1, float("nan"), float("inf")):
            candidate = market_candidate()
            candidate["unit_size"] = bad
            self.assertTrue(any("positive finite" in e for e in self.check(candidate)))

    def test_live_edge_floor_is_shared_by_producer_and_executor(self):
        candidate = market_candidate()
        candidate.update(current_ask=0.56, projected_edge_at_current_ask=0.04)
        self.assertTrue(any("below policy floor" in e for e in self.check(candidate)))

    def test_missing_caps_and_unknown_confidence_fail_closed(self):
        self.assertTrue(
            candidate_errors(market_candidate(), limits={}, state_dir=self.state)
        )
        candidate = market_candidate()
        candidate["confidence"] = "confident"
        self.assertTrue(self.check(candidate))
