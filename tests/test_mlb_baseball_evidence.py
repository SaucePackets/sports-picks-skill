import inspect
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

from scripts import mlb_baseball_evidence, mlb_postgame_evidence
from scripts.mlb_baseball_evidence import (
    baseball_evidence_errors,
    execution_checks_errors,
    valid_baseball_evidence,
    valid_execution_checks,
    validate_baseball_evidence,
    validate_execution_checks,
)
from scripts.mlb_postgame_evidence import usable_expected_ip
from scripts.numeric_util import is_finite_number

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


class SharedNumberPredicateTests(unittest.TestCase):
    """The write gate and the settlement grader share ONE number rule.

    They drifted once: this module gained `math.isfinite` in PR #43 and
    `mlb_postgame_evidence` did not, which is the whole reason a non-finite
    `expected_ip` could crash a settlement grade while the gate that wrote the
    card was correct. Equality of behaviour is not the pin — identity is,
    because a re-derived copy passes a behaviour test on the day it is written
    and fails silently on the day one side moves.
    """

    HUGE_INT = 10 ** 400

    def test_both_sides_are_the_same_function_object(self):
        # The load-bearing identity: the two consumers hold ONE object, so a
        # change to the rule cannot reach one side and miss the other.
        self.assertIs(mlb_baseball_evidence._is_number, mlb_postgame_evidence.is_finite_number)

    def test_each_side_CALLS_the_shared_rule_rather_than_merely_importing_it(self):
        # The identity assertion above compares module-level NAMES. On the
        # write side that name IS what the validators call, but on the read
        # side `usable_expected_ip` could re-derive the rule inline and leave
        # the import untouched — and the whole suite stays green (Reviewer
        # proved it on PR #69's first tip: 754 passed, nothing red).
        #
        # That is the selected-not-applied shape, and the direction it leaves
        # open is the one the drift historically ran in: PR #43 changed the
        # write side and the read side did not follow. So rebind the rule and
        # require the ANSWER to follow, in both directions — a one-directional
        # check is satisfied by a function that returns False for any reason.
        with unittest.mock.patch.object(
            mlb_postgame_evidence, "is_finite_number", lambda _v: False
        ):
            self.assertFalse(mlb_postgame_evidence.usable_expected_ip(6.0))
        with unittest.mock.patch.object(
            mlb_postgame_evidence, "is_finite_number", lambda _v: True
        ):
            self.assertTrue(mlb_postgame_evidence.usable_expected_ip(float("inf")))

    def test_the_write_gate_CALLS_the_shared_rule_at_its_validation_site(self):
        # Same property on the write side: `_is_number` being the shared object
        # says nothing about whether `validate_baseball_evidence` consults it.
        message = "expected_ip must be a positive number"
        with unittest.mock.patch.object(
            mlb_baseball_evidence, "_is_number", lambda _v: False
        ):
            self.assertIn(
                message, validate_baseball_evidence(valid_baseball_evidence())
            )
        with unittest.mock.patch.object(
            mlb_baseball_evidence, "_is_number", lambda _v: True
        ):
            self.assertNotIn(
                message,
                validate_baseball_evidence(
                    valid_baseball_evidence(expected_ip=float("inf"))
                ),
            )

    def test_the_dual_import_convention_duplicates_the_module_not_the_rule(self):
        # Named because it is a real hazard and it surprised me here: this repo
        # imports the same file two ways — as a package member (`scripts.x`,
        # from the tests) and as a bare sibling (`x`, from the runtime profile
        # copies) — and Python caches those as SEPARATE module objects. So
        # `scripts.numeric_util.is_finite_number is numeric_util.
        # is_finite_number` is FALSE while both are the same source.
        #
        # That is why the assertion above compares the two CONSUMERS rather
        # than either of them against a directly-imported copy: identity
        # against the package form would fail for a reason that says nothing
        # about drift, and "fix" it by weakening to equality.
        self.assertIsNot(is_finite_number, mlb_baseball_evidence._is_number)
        self.assertEqual(
            inspect.getsourcefile(is_finite_number),
            inspect.getsourcefile(mlb_baseball_evidence._is_number),
        )
        self.assertEqual(
            {"numeric_util", "scripts.numeric_util"} & set(sys.modules),
            {"numeric_util", "scripts.numeric_util"},
        )

    def test_the_write_gate_rejects_every_unusable_number(self):
        for label, value in (
            ("nan", float("nan")), ("inf", float("inf")),
            ("-inf", float("-inf")), ("huge_int", HUGE := 10 ** 400),
            ("zero", 0), ("negative", -1), ("bool", True), ("string", "6.0"),
        ):
            with self.subTest(value=label):
                evidence = valid_baseball_evidence(expected_ip=value)
                errors = validate_baseball_evidence(evidence)
                self.assertIn("expected_ip must be a positive number", errors)
        self.assertEqual(HUGE, self.HUGE_INT)

    def test_the_write_gate_reports_the_huge_integer_instead_of_raising(self):
        # `_is_number` called `math.isfinite` directly, so this input raised
        # `OverflowError` out of the execution gate's own validator.
        evidence = valid_baseball_evidence(expected_ip=self.HUGE_INT)
        self.assertIsInstance(baseball_evidence_errors(
            {"baseball_evidence": evidence,
             "execution_checks": valid_execution_checks()}
        ), list)

    def test_a_valid_card_is_still_valid(self):
        # The regression rail on the write side: sharing the predicate must
        # not reject anything the gate accepted before.
        self.assertEqual(validate_baseball_evidence(valid_baseball_evidence()), [])

    def test_the_two_sides_agree_on_every_shape_a_card_can_carry(self):
        # Behaviour agreement is the consequence of the identity above, and
        # asserting it separately is what makes a future divergence read as a
        # contradiction rather than as two independent test failures.
        for value in (6.0, 6, 0.1, 0, -1, True, None, "6.0", [6],
                      float("nan"), float("inf"), float("-inf"),
                      self.HUGE_INT, -self.HUGE_INT):
            with self.subTest(value=repr(value)[:32]):
                write_ok = "expected_ip must be a positive number" not in (
                    validate_baseball_evidence(
                        valid_baseball_evidence(expected_ip=value)
                    )
                )
                self.assertEqual(write_ok, usable_expected_ip(value))


if __name__ == "__main__":
    unittest.main()
