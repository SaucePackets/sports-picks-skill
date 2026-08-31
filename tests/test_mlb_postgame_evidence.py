import json
import unittest
from pathlib import Path

from scripts.mlb_postgame_evidence import (
    PILLARS,
    auto_pillar_grades,
    classify_actual_role,
    collect_postgame_evidence,
    derive_process_grade,
    ip_to_outs,
    postgame_prompt_section,
    side_for_team,
    usable_expected_ip,
    validate_process_grade,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "postgame"

REGRESSION_FIXTURES = (
    "detroit_opener_bulk_misclassification.json",
    "sd_arizona_starter_thesis_reversal.json",
    "mil_min_offense_pillar_failure.json",
    "stl_phi_bullpen_pillar_failure.json",
)


def load_fixture(name):
    return json.loads((FIXTURE_DIR / name).read_text())


def grade_obj(fixture, process_grade=None):
    return {
        "process_grade": process_grade or fixture["process_grade"],
        "pillars": fixture["reviewer_pillars"],
    }


def validate_fixture(fixture, evidence, process_grade=None):
    return validate_process_grade(
        grade_obj(fixture, process_grade),
        result=fixture["result"],
        baseball_evidence=fixture["baseball_evidence"],
        postgame_evidence=evidence,
        team=fixture["team"],
    )


class IpToOutsTests(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(ip_to_outs("5.2"), 17)
        self.assertEqual(ip_to_outs("0.0"), 0)
        self.assertEqual(ip_to_outs("9.0"), 27)

    def test_invalid_inputs(self):
        for bad in (None, 5.2, "5.3", "abc", ""):
            self.assertIsNone(ip_to_outs(bad))


class ClassifyActualRoleTests(unittest.TestCase):
    def test_conventional_starter(self):
        self.assertEqual(classify_actual_role([{"outs": 12}]), "starter")

    def test_opener_bulk_pattern(self):
        lines = [{"outs": 5}, {"outs": 13}, {"outs": 6}]
        self.assertEqual(classify_actual_role(lines), "opener_bulk")

    def test_short_start_without_bulk(self):
        lines = [{"outs": 8}, {"outs": 6}, {"outs": 6}]
        self.assertEqual(classify_actual_role(lines), "short_start")

    def test_opener_without_bulk_reliever_is_short_start(self):
        lines = [{"outs": 5}, {"outs": 6}, {"outs": 6}]
        self.assertEqual(classify_actual_role(lines), "short_start")

    def test_unknown_without_lines(self):
        self.assertEqual(classify_actual_role([]), "unknown")
        self.assertEqual(classify_actual_role([{"outs": None}]), "unknown")


class CollectPostgameEvidenceTests(unittest.TestCase):
    def test_complete_fixture_collects(self):
        fixture = load_fixture("detroit_opener_bulk_misclassification.json")
        evidence = collect_postgame_evidence(fixture["feed"])
        self.assertEqual(evidence["evidence_status"], "complete")
        self.assertEqual(evidence["insufficient_reasons"], [])
        self.assertEqual(evidence["away"], "Detroit Tigers")
        self.assertEqual(evidence["winner"], "San Francisco Giants")
        self.assertEqual(evidence["pitching"]["away"]["actual_role"], "opener_bulk")
        self.assertEqual(evidence["offense"]["away"]["runs"], 2)

    def test_non_dict_feed_is_insufficient(self):
        evidence = collect_postgame_evidence(None)
        self.assertEqual(evidence["evidence_status"], "insufficient")
        self.assertTrue(evidence["insufficient_reasons"])

    def test_non_final_game_is_insufficient(self):
        fixture = load_fixture("insufficient_evidence_no_boxscore.json")
        evidence = collect_postgame_evidence(fixture["feed"])
        self.assertEqual(evidence["evidence_status"], "insufficient")
        joined = " ".join(evidence["insufficient_reasons"])
        self.assertIn("not Final", joined)
        self.assertIn("boxscore", joined)

    def test_missing_batting_totals_fail_loud(self):
        fixture = load_fixture("mil_min_offense_pillar_failure.json")
        feed = fixture["feed"]
        del feed["liveData"]["boxscore"]["teams"]["away"]["teamStats"]["batting"]["runs"]
        evidence = collect_postgame_evidence(feed)
        self.assertEqual(evidence["evidence_status"], "insufficient")
        self.assertTrue(
            any("batting runs" in r for r in evidence["insufficient_reasons"])
        )

    def test_missing_pitching_line_fails_loud(self):
        fixture = load_fixture("mil_min_offense_pillar_failure.json")
        feed = fixture["feed"]
        away = feed["liveData"]["boxscore"]["teams"]["away"]
        del away["players"]["ID121"]["stats"]["pitching"]
        evidence = collect_postgame_evidence(feed)
        self.assertEqual(evidence["evidence_status"], "insufficient")
        self.assertTrue(
            any("pitching line" in r for r in evidence["insufficient_reasons"])
        )

    def test_side_for_team_requires_exact_match(self):
        fixture = load_fixture("detroit_opener_bulk_misclassification.json")
        evidence = collect_postgame_evidence(fixture["feed"])
        self.assertEqual(side_for_team(evidence, "Detroit Tigers"), "away")
        self.assertEqual(side_for_team(evidence, "San Francisco Giants"), "home")
        self.assertIsNone(side_for_team(evidence, "Detroit"))
        self.assertIsNone(side_for_team(evidence, ""))


class AutoPillarGradesTests(unittest.TestCase):
    def test_team_mismatch_raises(self):
        fixture = load_fixture("detroit_opener_bulk_misclassification.json")
        evidence = collect_postgame_evidence(fixture["feed"])
        with self.assertRaises(ValueError):
            auto_pillar_grades(fixture["baseball_evidence"], evidence, "Tigers")

    def test_insufficient_evidence_grades_every_pillar_unknown(self):
        fixture = load_fixture("insufficient_evidence_no_boxscore.json")
        evidence = collect_postgame_evidence(fixture["feed"])
        grades = auto_pillar_grades(fixture["baseball_evidence"], evidence, fixture["team"])
        self.assertEqual(set(grades), set(PILLARS))
        self.assertTrue(all(g["grade"] == "unknown" for g in grades.values()))

    def test_named_risks_require_reviewer_judgment(self):
        fixture = load_fixture("sd_arizona_starter_thesis_reversal.json")
        fixture["baseball_evidence"]["named_risks"] = [
            {"name": "HR risk", "status": "resolved", "evidence": "wind in"}
        ]
        evidence = collect_postgame_evidence(fixture["feed"])
        grades = auto_pillar_grades(fixture["baseball_evidence"], evidence, fixture["team"])
        self.assertEqual(grades["named_risk"]["grade"], "unknown")
        self.assertIn("HR risk", grades["named_risk"]["evidence"])


class NonFiniteExpectedIpTests(unittest.TestCase):
    """A non-finite `expected_ip` grades unknown; it never raises.

    Both settlement-grade entry points are covered, because they fail
    differently: `auto_pillar_grades` raised `OverflowError` out of
    `round(inf * 3)`, and `validate_process_grade` catches only `ValueError`,
    so the same input took down a reviewer's settlement grade rather than
    returning an error list it could report.
    """

    NON_FINITE = (("nan", float("nan")), ("inf", float("inf")), ("-inf", float("-inf")))

    def setUp(self):
        self.fixture = load_fixture("sd_arizona_starter_thesis_reversal.json")
        self.evidence = collect_postgame_evidence(self.fixture["feed"])

    def _evidence_with(self, expected_ip):
        evidence = dict(self.fixture["baseball_evidence"])
        evidence["expected_ip"] = expected_ip
        return evidence

    def test_the_predicate_rejects_every_non_finite_value(self):
        for label, value in self.NON_FINITE:
            with self.subTest(value=label):
                self.assertFalse(usable_expected_ip(value))
        # ...and still accepts the finite values it always did.
        for value in (5.5, 6, 0.1):
            with self.subTest(value=value):
                self.assertTrue(usable_expected_ip(value))
        for value in (0, -1, True, None, "5.5", [5.5]):
            with self.subTest(value=value):
                self.assertFalse(usable_expected_ip(value))

    def test_auto_pillar_grades_returns_unknown_instead_of_raising(self):
        for label, value in self.NON_FINITE:
            with self.subTest(value=label):
                grades = auto_pillar_grades(
                    self._evidence_with(value), self.evidence, self.fixture["team"]
                )
                self.assertEqual(grades["starter_quality"]["grade"], "unknown")
                # The other pillars are unaffected — a bad `expected_ip`
                # disables one pillar, it does not blank the grade.
                self.assertNotEqual(grades["starter_role"]["grade"], "unknown")

    def test_validate_process_grade_reports_instead_of_crashing(self):
        # `validate_process_grade` catches ValueError only, so an OverflowError
        # here escaped the settlement gate entirely.
        for label, value in self.NON_FINITE:
            with self.subTest(value=label):
                errors = validate_process_grade(
                    grade_obj(self.fixture),
                    result=self.fixture["result"],
                    baseball_evidence=self._evidence_with(value),
                    postgame_evidence=self.evidence,
                    team=self.fixture["team"],
                )
                self.assertIsInstance(errors, list)

    def test_the_bare_json_literals_are_what_make_this_reachable(self):
        # `json.loads` accepts bare `NaN`/`Infinity` by default, so a card on
        # disk can carry either without any hand-editing of a Python object.
        # Without this, the fix above reads as defending against nothing.
        loaded = json.loads('{"expected_ip": Infinity, "other": NaN}')
        self.assertTrue(loaded["expected_ip"] == float("inf"))
        self.assertNotEqual(loaded["other"], loaded["other"])  # NaN
        grades = auto_pillar_grades(
            self._evidence_with(loaded["expected_ip"]),
            self.evidence,
            self.fixture["team"],
        )
        self.assertEqual(grades["starter_quality"]["grade"], "unknown")

    def test_a_finite_expected_ip_grades_exactly_as_before(self):
        # The regression rail: finiteness must not move any finite verdict.
        graded = {
            value: auto_pillar_grades(
                self._evidence_with(value), self.evidence, self.fixture["team"]
            )["starter_quality"]["grade"]
            for value in (1.0, 5.5, 9.0, 20.0)
        }
        self.assertNotIn("unknown", graded.values())


class DeriveProcessGradeTests(unittest.TestCase):
    def _grades(self, **overrides):
        grades = {pillar: {"grade": "held"} for pillar in PILLARS}
        for pillar, grade in overrides.items():
            grades[pillar] = {"grade": grade}
        return grades

    def test_insufficient_status_wins(self):
        self.assertEqual(
            derive_process_grade("loss", self._grades(), "insufficient"),
            "insufficient_evidence",
        )

    def test_unknown_pillar_is_insufficient(self):
        self.assertEqual(
            derive_process_grade("loss", self._grades(named_risk="unknown"), "complete"),
            "insufficient_evidence",
        )

    def test_mixed_pillar_must_be_resolved(self):
        with self.assertRaises(ValueError):
            derive_process_grade("loss", self._grades(starter_quality="mixed"), "complete")

    def test_first_failed_pillar_in_order(self):
        grades = self._grades(starter_role="failed", offense_conversion="failed")
        self.assertEqual(
            derive_process_grade("loss", grades, "complete"), "bad_read_starter_role"
        )

    def test_all_held(self):
        self.assertEqual(
            derive_process_grade("loss", self._grades(), "complete"),
            "good_read_bad_variance",
        )
        self.assertEqual(
            derive_process_grade("win", self._grades(), "complete"),
            "good_read_edge_held",
        )


class ValidateProcessGradeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_fixture("mil_min_offense_pillar_failure.json")
        self.evidence = collect_postgame_evidence(self.fixture["feed"])

    def test_non_object_inputs(self):
        self.assertEqual(
            validate_process_grade(
                None,
                result="loss",
                baseball_evidence={},
                postgame_evidence=self.evidence,
                team="Milwaukee Brewers",
            ),
            ["process_grade must be an object"],
        )
        self.assertEqual(
            validate_process_grade(
                {"process_grade": "insufficient_evidence"},
                result="loss",
                baseball_evidence={},
                postgame_evidence=None,
                team="Milwaukee Brewers",
            ),
            ["postgame_evidence must be an object (run the collector first)"],
        )

    def test_missing_and_unknown_pillars(self):
        obj = grade_obj(self.fixture)
        obj["pillars"] = dict(obj["pillars"])
        obj["pillars"].pop("named_risk")
        obj["pillars"]["extra"] = {"grade": "held", "evidence": "x"}
        errors = validate_fixture({**self.fixture, "reviewer_pillars": obj["pillars"]}, self.evidence)
        joined = " ".join(errors)
        self.assertIn("missing required entries: named_risk", joined)
        self.assertIn("unknown entries: extra", joined)

    def test_pillar_requires_written_evidence(self):
        fixture = json.loads(json.dumps(self.fixture))
        fixture["reviewer_pillars"]["offense_conversion"]["evidence"] = "  "
        errors = validate_fixture(fixture, self.evidence)
        self.assertTrue(any("non-empty written evidence" in e for e in errors))

    def test_deterministic_grade_cannot_be_overridden(self):
        fixture = json.loads(json.dumps(self.fixture))
        fixture["reviewer_pillars"]["offense_conversion"]["grade"] = "held"
        fixture["process_grade"] = "good_read_bad_variance"
        errors = validate_fixture(fixture, self.evidence)
        self.assertTrue(
            any("deterministic postgame grade is 'failed'" in e for e in errors)
        )

    def test_variance_requires_every_pillar_held(self):
        errors = validate_fixture(self.fixture, self.evidence, "good_read_bad_variance")
        self.assertTrue(
            any("requires every pillar graded 'held'" in e for e in errors)
        )

    def test_variance_is_a_loss_grade(self):
        fixture = load_fixture("sd_arizona_starter_thesis_reversal.json")
        held = {
            pillar: {"grade": "held", "evidence": "held per boxscore"}
            for pillar in PILLARS
        }
        evidence = collect_postgame_evidence(fixture["feed"])
        errors = validate_process_grade(
            {"process_grade": "good_read_bad_variance", "pillars": held},
            result="win",
            baseball_evidence=fixture["baseball_evidence"],
            postgame_evidence=evidence,
            team=fixture["team"],
        )
        self.assertTrue(any("is a loss grade" in e for e in errors))

    def test_execution_issue_requires_text(self):
        fixture = load_fixture("mil_min_offense_pillar_failure.json")
        evidence = collect_postgame_evidence(fixture["feed"])
        errors = validate_fixture(fixture, evidence, "good_read_execution_issue")
        self.assertTrue(any("execution_issue" in e for e in errors))

    def test_bad_read_requires_matching_failed_pillar(self):
        errors = validate_fixture(self.fixture, self.evidence, "bad_read_named_risk")
        self.assertTrue(
            any("requires pillars.named_risk graded 'failed'" in e for e in errors)
        )

    def test_insufficient_grade_rejected_when_evidence_complete(self):
        errors = validate_fixture(self.fixture, self.evidence, "insufficient_evidence")
        self.assertTrue(
            any("insufficient_evidence is not allowed" in e for e in errors)
        )


class RegressionFixtureTests(unittest.TestCase):
    def test_regression_games(self):
        for name in REGRESSION_FIXTURES:
            with self.subTest(fixture=name):
                fixture = load_fixture(name)
                evidence = collect_postgame_evidence(fixture["feed"])
                self.assertEqual(evidence["evidence_status"], "complete")
                our_side = side_for_team(evidence, fixture["team"])
                self.assertEqual(
                    evidence["pitching"][our_side]["actual_role"],
                    fixture["expected"]["actual_role"],
                )

                auto = auto_pillar_grades(
                    fixture["baseball_evidence"], evidence, fixture["team"]
                )
                for pillar, expected_grade in fixture["expected"]["auto"].items():
                    self.assertEqual(
                        auto[pillar]["grade"], expected_grade, msg=f"{name}:{pillar}"
                    )

                self.assertEqual(
                    derive_process_grade(
                        fixture["result"],
                        fixture["reviewer_pillars"],
                        evidence["evidence_status"],
                    ),
                    fixture["expected"]["process_grade"],
                )

                self.assertEqual(validate_fixture(fixture, evidence), [])

                variance_errors = validate_fixture(
                    fixture, evidence, fixture["rejected_process_grade"]
                )
                self.assertTrue(
                    variance_errors,
                    msg=f"{name}: grading this loss variance must be rejected",
                )

    def test_insufficient_evidence_fixture(self):
        fixture = load_fixture("insufficient_evidence_no_boxscore.json")
        evidence = collect_postgame_evidence(fixture["feed"])
        self.assertEqual(evidence["evidence_status"], "insufficient")

        self.assertEqual(validate_fixture(fixture, evidence), [])

        errors = validate_fixture(fixture, evidence, "good_read_bad_variance")
        self.assertTrue(
            any("cannot be graded variance without evidence" in e for e in errors)
        )


class SettlementPromptTests(unittest.TestCase):
    def test_prompt_section_carries_the_contract(self):
        section = postgame_prompt_section()
        self.assertIn("POSTGAME EVIDENCE + PROCESS GRADE", section)
        self.assertIn("mlb_postgame_evidence.py collect", section)
        self.assertIn("mlb_postgame_evidence.py grade", section)
        self.assertIn("insufficient_evidence", section)
        self.assertIn('do NOT write "I would assign it again"', section)
        for grade in (
            "good_read_bad_variance",
            "good_read_edge_held",
            "good_read_execution_issue",
            "bad_read_starter_role",
            "bad_read_starter_quality",
            "bad_read_bullpen_availability",
            "bad_read_offense_conversion",
            "bad_read_named_risk",
        ):
            self.assertIn(grade, section)

    def test_settlement_gate_prompt_includes_the_section(self):
        from scripts.vig_postgame_gate import build_settlement_prompt

        prompt = build_settlement_prompt(
            open_pick_ids="mlb-2026-08-11-det",
            open_count=1,
            cohort_section="  strong (edge >=4.4%): no settled picks yet",
            small_cohort_section="  small-stake tier (confidence=small): no settled picks yet",
            recon_section="",
        )
        self.assertIn("POSTGAME EVIDENCE + PROCESS GRADE", prompt)
        self.assertIn("process_grade", prompt)
        self.assertNotIn("would we assign it again", prompt)


if __name__ == "__main__":
    unittest.main()
