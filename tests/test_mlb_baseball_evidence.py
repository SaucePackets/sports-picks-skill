import json
import unittest
from pathlib import Path

from scripts.mlb_baseball_evidence import (
    baseball_evidence_errors,
    execution_checks_errors,
    valid_baseball_evidence,
    valid_execution_checks,
    validate_baseball_evidence,
    validate_execution_checks,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "baseball_evidence"


class BaseballEvidenceTests(unittest.TestCase):
    def test_valid_evidence_passes(self):
        self.assertEqual(validate_baseball_evidence(valid_baseball_evidence()), [])

    def test_fixture_files_match_expected_validity(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            with self.subTest(fixture=path.name):
                data = json.loads(path.read_text())
                errors = validate_baseball_evidence(data["baseball_evidence"])
                if data.get("valid"):
                    self.assertEqual(errors, [])
                else:
                    self.assertTrue(errors, msg=f"{path.name} should produce hard failures")

    def test_missing_evidence_fails_closed(self):
        self.assertEqual(
            baseball_evidence_errors({}),
            ["baseball_evidence must be an object"],
        )

    def test_missing_required_fields(self):
        evidence = valid_baseball_evidence()
        evidence.pop("starter_role")
        errors = validate_baseball_evidence(evidence)
        self.assertTrue(errors)
        self.assertIn("starter_role", errors[0])

    def test_unknown_starter_role(self):
        errors = validate_baseball_evidence(
            valid_baseball_evidence(starter_role="unknown")
        )
        self.assertTrue(any("unknown" in e.lower() for e in errors))

    def test_opener_requires_bulk_path_plan(self):
        errors = validate_baseball_evidence(
            valid_baseball_evidence(starter_role="opener", bulk_path_plan="")
        )
        self.assertTrue(any("bulk_path_plan" in e for e in errors))

    def test_unresolved_named_risk(self):
        risk = {
            "name": "weather delay",
            "status": "unresolved",
            "evidence": "Start time delayed; no resolution confirmation",
        }
        errors = validate_baseball_evidence(valid_baseball_evidence(named_risks=[risk]))
        self.assertTrue(any("unresolved" in e.lower() for e in errors))

    def test_primary_thesis_requires_six_start_sample(self):
        errors = validate_baseball_evidence(
            valid_baseball_evidence(
                starter_games_started=5,
                primary_thesis_pillar=True,
            )
        )
        self.assertTrue(any("sample too small" in e.lower() for e in errors))

    def test_contact_primary_requires_large_support_layer(self):
        errors = validate_baseball_evidence(
            valid_baseball_evidence(
                contact_hr_risk={"magnitude": "large", "notes": "HR-prone fly-ball profile"},
                support_layers=[{"pillar": "offense", "magnitude": "small"}],
            )
        )
        self.assertTrue(any("large support layer" in e.lower() for e in errors))

    def test_bullpen_requires_leverage_arms_available(self):
        errors = validate_baseball_evidence(
            valid_baseball_evidence(
                bullpen_availability={
                    "magnitude": "small",
                    "leverage_arms_available": False,
                    "notes": "Back-end arms taxed",
                }
            )
        )
        self.assertTrue(any("leverage_arms_available" in e.lower() for e in errors))

    def test_probability_delta_explanation_required_for_large_edge(self):
        evidence = valid_baseball_evidence(probability_delta_explanation="")
        candidate = {"dk_fair_prob": 0.55, "raw_probability": 0.60}
        errors = validate_baseball_evidence(evidence, candidate)
        self.assertTrue(any("quantified explanation" in e for e in errors))

    def test_valid_execution_checks(self):
        self.assertEqual(validate_execution_checks(valid_execution_checks()), [])

    def test_execution_checks_missing_field(self):
        checks = valid_execution_checks()
        checks.pop("liquidity")
        self.assertTrue(validate_execution_checks(checks))

    def test_execution_checks_boolean_fields_must_be_true(self):
        checks = valid_execution_checks(lineup_confirmation=False)
        self.assertTrue(any("lineup_confirmation" in e for e in validate_execution_checks(checks)))

    def test_wrappers(self):
        candidate = {
            "baseball_evidence": valid_baseball_evidence(),
            "execution_checks": valid_execution_checks(),
        }
        self.assertEqual(baseball_evidence_errors(candidate), [])
        self.assertEqual(execution_checks_errors(candidate), [])


if __name__ == "__main__":
    unittest.main()
