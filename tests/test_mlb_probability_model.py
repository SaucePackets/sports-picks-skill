import json
import math
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import mlb_probability_model
from scripts.mlb_baseball_evidence import _is_number as write_gate_is_number
from scripts.mlb_probability_model import (
    MARKET_MODEL_VERSION,
    brier_score,
    build_dataset,
    calibration_error,
    calibration_line,
    compare_to_market,
    deployment_gate_decision,
    evaluate_predictions,
    load_model_deployment_policy,
    log_loss,
    main,
    probability_component_errors,
    probability_contract_prompt_section,
    valid_probability_components,
    validate_probability_components,
    walk_forward_report,
)


def candidate_trail(**overrides):
    trail = {
        "dk_fair_prob": 0.55,
        "raw_probability": 0.57,
        "uncertainty_haircut": 0.03,
        "conservative_probability": 0.54,
        "probability_components": valid_probability_components(),
    }
    trail.update(overrides)
    return trail


class ProbabilityComponentContractTests(unittest.TestCase):
    def test_valid_components_pass(self):
        self.assertEqual(probability_component_errors(candidate_trail()), [])

    def test_missing_object_fails_closed(self):
        errors = probability_component_errors({"dk_fair_prob": 0.55})
        self.assertEqual(errors, ["probability_components must be an object"])

    def test_market_only_fallback_is_the_empty_contract(self):
        candidate = candidate_trail(
            raw_probability=0.55,
            uncertainty_haircut=0.0,
            conservative_probability=0.55,
            probability_components={"adjustments": [], "haircuts": []},
        )
        self.assertEqual(probability_component_errors(candidate), [])

    def test_unknown_park_environment_is_a_priceable_haircut(self):
        # The park-factor loosening (Jerry, 2026-08-23). A venue off the
        # scanner's table used to have no writable form: the haircut allowed-list
        # is fail-closed, so pricing the unknown run environment was REJECTED
        # while staying silent about the park validated cleanly. The honest
        # answer was the only rejected one, and on 2026-08-23 the agent chose
        # discard — the same data-outage-becomes-terminal shape as the price and
        # lineup outages. See test_silence_about_an_unavailable_park_still_
        # validates for the other half of that, which is still true.
        candidate = candidate_trail(
            probability_components=valid_probability_components(
                haircuts=[
                    {
                        "component": "unknown_park_environment",
                        "amount": 0.03,
                        "evidence": (
                            "park.data_status=unavailable for Journey Bank Ballpark; "
                            "run environment unknown"
                        ),
                    }
                ]
            )
        )
        self.assertEqual(probability_component_errors(candidate), [])

    def test_silence_about_an_unavailable_park_still_validates(self):
        # Pins the gap rather than claiming it is closed. Charging the
        # unknown_park_environment haircut when the scanner reports
        # park.data_status == "unavailable" is PROMPT-SIDE ONLY: this validator
        # never receives the scanner payload, so a candidate that simply never
        # mentions the park validates with zero errors and its
        # conservative_probability is unchanged — it clears on exactly the same
        # terms a known-park game would. Making the haircut mandatory needs the
        # park status plumbed onto the candidate; it is a policy call, open with
        # Jerry as of 2026-08-23.
        #
        # An earlier version of this comment said the test would FLIP if that
        # lands. It would not: candidate_trail() carries no park status at all,
        # so it would keep validating no matter what the rule says about an
        # unavailable park. The test that flips has to CONSTRUCT a candidate
        # carrying park.data_status == "unavailable", which cannot be written
        # until the field is plumbed. This one asserts the same expression as
        # test_valid_components_pass and is documentary only.
        self.assertEqual(probability_component_errors(candidate_trail()), [])

    def test_unknown_park_haircut_cannot_accompany_a_park_adjustment(self):
        # The loosening's own guardrail: taking a park_home_context edge while
        # charging for not knowing the park nets to nothing and turns the buffer
        # into a free pass.
        candidate = candidate_trail(
            probability_components=valid_probability_components(
                adjustments=[
                    {
                        "component": "park_home_context",
                        "delta": 0.02,
                        "evidence": "claims a park read",
                    }
                ],
                haircuts=[
                    {
                        "component": "unknown_park_environment",
                        "amount": 0.03,
                        "evidence": "claims the park is unknown",
                    }
                ],
            )
        )
        errors = probability_component_errors(candidate)
        self.assertTrue(
            any("unknown_park_environment haircut cannot accompany" in e for e in errors),
            errors,
        )

    def test_unknown_component_rejected(self):
        components = valid_probability_components(
            adjustments=[{"component": "vibes", "delta": 0.02, "evidence": "x"}]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("component must be one of" in e for e in errors))

    def test_duplicate_component_rejected(self):
        components = valid_probability_components(
            adjustments=[
                {"component": "starter_run_prevention", "delta": 0.01, "evidence": "a"},
                {"component": "starter_run_prevention", "delta": 0.01, "evidence": "b"},
            ]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("duplicate component" in e for e in errors))

    def test_component_requires_written_evidence(self):
        components = valid_probability_components(
            adjustments=[
                {"component": "starter_run_prevention", "delta": 0.02, "evidence": "  "}
            ]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("written evidence" in e for e in errors))

    def test_adjustments_must_sum_to_raw_minus_fair(self):
        components = valid_probability_components(
            adjustments=[
                {
                    "component": "starter_run_prevention",
                    "delta": 0.05,
                    "evidence": "overstated",
                }
            ]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("must be an explicit component" in e for e in errors))

    def test_haircuts_must_sum_to_uncertainty_haircut(self):
        components = valid_probability_components(
            haircuts=[{"component": "small_sample", "amount": 0.01, "evidence": "x"}]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("uncertainty_haircut" in e for e in errors))

    def test_haircut_amount_must_be_positive(self):
        components = valid_probability_components(
            haircuts=[
                {"component": "small_sample", "amount": -0.03, "evidence": "x"},
                {"component": "conflicting_signals", "amount": 0.06, "evidence": "y"},
            ]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("must be positive" in e for e in errors))

    def test_conservative_identity_enforced(self):
        errors = probability_component_errors(
            candidate_trail(conservative_probability=0.5)
        )
        self.assertTrue(
            any("raw_probability - \nuncertainty_haircut" in e or
                "raw_probability - uncertainty_haircut" in e.replace("\n", " ")
                for e in errors)
        )

    def test_recent_form_cannot_be_the_anchor(self):
        components = {
            "adjustments": [
                {"component": "recent_form", "delta": 0.015, "evidence": "hot week"},
                {
                    "component": "starter_run_prevention",
                    "delta": 0.005,
                    "evidence": "modest starter edge",
                },
            ],
            "haircuts": valid_probability_components()["haircuts"],
        }
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("never the anchor" in e for e in errors))

    def test_recent_form_low_weight_bound(self):
        components = {
            "adjustments": [
                {"component": "recent_form", "delta": 0.03, "evidence": "streak"},
                {
                    "component": "starter_run_prevention",
                    "delta": -0.01,
                    "evidence": "starter downgrade",
                },
            ],
            "haircuts": valid_probability_components()["haircuts"],
        }
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("low-weight bound" in e for e in errors))

    def test_single_adjustment_bound(self):
        components = valid_probability_components(
            adjustments=[
                {"component": "starter_run_prevention", "delta": 0.2, "evidence": "x"},
                {
                    "component": "lineup_offense_quality",
                    "delta": -0.18,
                    "evidence": "y",
                },
            ]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("single-component bound" in e for e in errors))

    def test_postgame_leakage_inside_component_rejected(self):
        components = valid_probability_components(
            adjustments=[
                {
                    "component": "starter_run_prevention",
                    "delta": 0.02,
                    "evidence": "boxscore says he dominated",
                    "final_score": "6-2",
                }
            ]
        )
        errors = validate_probability_components(components, candidate_trail())
        self.assertTrue(any("pre-pitch only" in e for e in errors))


class DatasetBuilderTests(unittest.TestCase):
    def pick(self, **overrides):
        pick = {
            "pick_id": "mlb-2026-08-01-det",
            "status": "settled",
            "result": "win",
            "game_date": "2026-08-01",
            "side": "Detroit Tigers",
            "dk_fair_prob": 0.55,
            "raw_probability": 0.57,
            "uncertainty_haircut": 0.03,
            "conservative_probability": 0.54,
            "entry_price": 0.49,
            "model_version": "vig-mlb-components-v1",
            "process_grade": {"process_grade": "good_read_edge_held"},
            "pnl": 12.5,
        }
        pick.update(overrides)
        return pick

    def test_rows_carry_prepitch_fields_and_outcome_only(self):
        rows, skipped = build_dataset([self.pick()])
        self.assertEqual(skipped, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["outcome"], 1)
        self.assertEqual(row["model_version"], "vig-mlb-components-v1")
        for leaked in ("process_grade", "pnl", "result"):
            self.assertNotIn(leaked, row)

    def test_unsettled_and_incomplete_picks_skip_loudly(self):
        rows, skipped = build_dataset(
            [
                self.pick(pick_id="open", status="active"),
                self.pick(pick_id="no-date", game_date=None, date=None),
                self.pick(pick_id="no-prob", dk_fair_prob=None),
                self.pick(pick_id="ok", result="loss"),
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pick_id"], "ok")
        self.assertEqual(rows[0]["outcome"], 0)
        self.assertEqual(len(skipped), 3)
        self.assertTrue(any("not a settled win/loss" in s for s in skipped))
        self.assertTrue(any("missing game date" in s for s in skipped))
        self.assertTrue(any("missing probability fields" in s for s in skipped))

    def test_rows_sorted_chronologically(self):
        rows, _ = build_dataset(
            [
                self.pick(pick_id="b", game_date="2026-08-03"),
                self.pick(pick_id="a", game_date="2026-08-01"),
                self.pick(pick_id="c", game_date="2026-08-02"),
            ]
        )
        self.assertEqual([r["date"] for r in rows], ["2026-08-01", "2026-08-02", "2026-08-03"])


def make_rows(groups):
    """groups: list of (model_p, market_p, wins, losses) tuples."""
    rows = []
    day = 1
    for model_p, market_p, wins, losses in groups:
        for outcome, count in ((1, wins), (0, losses)):
            for _ in range(count):
                rows.append(
                    {
                        "date": f"2026-07-{day:02d}",
                        "pick_id": f"p{len(rows)}",
                        "conservative_probability": model_p,
                        "dk_fair_prob": market_p,
                        "outcome": outcome,
                    }
                )
                day = day % 28 + 1
    return rows


# A model that is perfectly calibrated where the market is systematically
# under-confident: observed rates match the model exactly.
CALIBRATED_MODEL_GROUPS = [
    (0.8, 0.6, 8, 2),  # model 0.8 group wins 80%
    (0.2, 0.4, 2, 8),  # model 0.2 group wins 20%
]


class MetricsTests(unittest.TestCase):
    def test_brier_and_log_loss_hand_checked(self):
        pairs = [(0.8, 1), (0.6, 0)]
        self.assertAlmostEqual(brier_score(pairs), ((0.2**2) + (0.6**2)) / 2)
        expected = (-math.log(0.8) - math.log(0.4)) / 2
        self.assertAlmostEqual(log_loss(pairs), expected)

    def test_empty_metrics_are_none(self):
        self.assertIsNone(brier_score([]))
        self.assertIsNone(log_loss([]))
        self.assertIsNone(calibration_line([(0.5, 1)]))
        self.assertIsNone(calibration_line([(0.5, 1), (0.5, 0)]))

    def test_perfectly_calibrated_slope_and_intercept(self):
        rows = make_rows(CALIBRATED_MODEL_GROUPS)
        pairs = [(r["conservative_probability"], r["outcome"]) for r in rows]
        slope, intercept = calibration_line(pairs)
        self.assertAlmostEqual(slope, 1.0)
        self.assertAlmostEqual(intercept, 0.0)
        self.assertAlmostEqual(calibration_error((slope, intercept)), 0.0)

    def test_evaluate_predictions_reports_buckets(self):
        rows = make_rows(CALIBRATED_MODEL_GROUPS)
        report = evaluate_predictions(rows, "conservative_probability")
        self.assertEqual(report["n"], 20)
        self.assertAlmostEqual(report["brier"], 0.16)
        buckets = {b["bucket"]: b for b in report["reliability_buckets"]}
        self.assertAlmostEqual(buckets["0.80-0.85"]["observed_rate"], 0.8)
        self.assertAlmostEqual(buckets["0.20-0.25"]["observed_rate"], 0.2)


class WalkForwardTests(unittest.TestCase):
    def test_windows_are_chronological_and_never_random(self):
        rows = make_rows(CALIBRATED_MODEL_GROUPS)
        report = walk_forward_report(rows, "conservative_probability", window=7)
        self.assertEqual(report["split"], "time-ordered walk-forward (no random split)")
        self.assertEqual([w["n"] for w in report["windows"]], [7, 7, 6])
        for window in report["windows"]:
            self.assertLessEqual(window["start_date"], window["end_date"])
        boundaries = [
            (w["start_date"], w["end_date"]) for w in report["windows"]
        ]
        for (_, prev_end), (next_start, _) in zip(boundaries, boundaries[1:]):
            self.assertLessEqual(prev_end, next_start)
        self.assertEqual(report["cumulative"]["n"], 20)

    def test_window_must_be_positive(self):
        with self.assertRaises(ValueError):
            walk_forward_report([], "conservative_probability", window=0)


class DeploymentGateTests(unittest.TestCase):
    POLICY = {
        "min_evaluation_picks": 20,
        "min_brier_improvement": 0.005,
        "min_log_loss_improvement": 0.01,
        "max_calibration_regression": 0.0,
        "max_score_regression": 0.0,
    }

    def test_missing_policy_fails_closed(self):
        rows = make_rows(CALIBRATED_MODEL_GROUPS)
        decision = deployment_gate_decision(compare_to_market(rows), None)
        self.assertFalse(decision["deployable"])
        self.assertEqual(decision["fallback_model_version"], MARKET_MODEL_VERSION)
        self.assertTrue(any("failing closed" in r for r in decision["reasons"]))

    def test_better_calibrated_model_deploys(self):
        rows = make_rows(CALIBRATED_MODEL_GROUPS)
        comparison = compare_to_market(rows)
        self.assertLess(comparison["deltas"]["brier"], 0)
        decision = deployment_gate_decision(comparison, self.POLICY)
        self.assertTrue(decision["deployable"], msg=decision["reasons"])

    def test_worse_than_market_model_is_blocked(self):
        # Swap fields: the "model" is now the vaguer market number.
        rows = [
            {**row,
             "conservative_probability": row["dk_fair_prob"],
             "dk_fair_prob": row["conservative_probability"]}
            for row in make_rows(CALIBRATED_MODEL_GROUPS)
        ]
        decision = deployment_gate_decision(compare_to_market(rows), self.POLICY)
        self.assertFalse(decision["deployable"])
        joined = " ".join(decision["reasons"])
        self.assertIn("calibration is worse", joined)
        self.assertIn("regresses vs the market baseline", joined)

    def test_too_small_window_is_blocked(self):
        rows = make_rows(CALIBRATED_MODEL_GROUPS)[:10]
        decision = deployment_gate_decision(compare_to_market(rows), self.POLICY)
        self.assertFalse(decision["deployable"])
        self.assertTrue(any("20 required" in r for r in decision["reasons"]))

    def test_no_improvement_by_margin_is_blocked(self):
        # Identical model and market predictions: calibration is equal, but
        # nothing improves by the predeclared margin.
        rows = [
            {**row, "conservative_probability": row["dk_fair_prob"]}
            for row in make_rows(CALIBRATED_MODEL_GROUPS)
        ]
        decision = deployment_gate_decision(compare_to_market(rows), self.POLICY)
        self.assertFalse(decision["deployable"])
        self.assertTrue(any("predeclared margin" in r for r in decision["reasons"]))


class DeploymentPolicyLoaderTests(unittest.TestCase):
    def write_policy(self, block):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        (state / "risk_limits.json").write_text(
            json.dumps({"mlb_model_deployment_policy": block})
        )
        return state

    def test_valid_policy_loads(self):
        state = self.write_policy(
            {
                "schema": "vig-mlb-model-deployment-policy-v1",
                "min_evaluation_picks": 50,
                "min_brier_improvement": 0.004,
            }
        )
        policy = load_model_deployment_policy(state)
        self.assertIsNotNone(policy)
        self.assertEqual(policy["min_evaluation_picks"], 50)
        self.assertEqual(policy["min_brier_improvement"], 0.004)
        # Undeclared margins take the documented defaults.
        self.assertEqual(policy["min_log_loss_improvement"], 0.01)

    def test_missing_file_wrong_schema_and_bad_values_fail_closed(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.assertIsNone(load_model_deployment_policy(Path(empty.name)))
        self.assertIsNone(load_model_deployment_policy(self.write_policy({"schema": "other"})))
        self.assertIsNone(
            load_model_deployment_policy(
                self.write_policy(
                    {
                        "schema": "vig-mlb-model-deployment-policy-v1",
                        "min_evaluation_picks": 0,
                    }
                )
            )
        )
        self.assertIsNone(
            load_model_deployment_policy(
                self.write_policy(
                    {
                        "schema": "vig-mlb-model-deployment-policy-v1",
                        "min_brier_improvement": -0.01,
                    }
                )
            )
        )


class CliGateTests(unittest.TestCase):
    def test_gate_exits_nonzero_without_policy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dataset = Path(tmp.name) / "dataset.jsonl"
        dataset.write_text(
            "\n".join(json.dumps(row) for row in make_rows(CALIBRATED_MODEL_GROUPS))
        )
        import contextlib, io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(
                [
                    "gate",
                    "--dataset",
                    str(dataset),
                    "--model-version",
                    "vig-mlb-components-v1",
                    "--state-dir",
                    tmp.name,
                ]
            )
        self.assertEqual(code, 1)
        decision = json.loads(buffer.getvalue())
        self.assertFalse(decision["deployable"])


class PromptWiringTests(unittest.TestCase):
    def test_contract_section_content(self):
        section = probability_contract_prompt_section()
        self.assertIn("PROBABILITY COMPONENTS", section)
        self.assertIn("raw_probability - dk_fair_prob", section)
        self.assertIn("uncertainty_haircut", section)
        self.assertIn("recent_form", section)
        self.assertIn("vig-mlb-market-v1", section)
        self.assertIn("mlb_probability_model.py gate", section)
        # The unknown-park route has to reach the handicapper, not just the
        # validator: the child only knows an unavailable park factor is priceable
        # if the prompt says so. Without this the allowed-list widens and the
        # behaviour that discarded the game stays exactly as it was.
        self.assertIn("unknown_park_environment", section)
        self.assertIn("Discarding a game over an unavailable input", section)

    def test_review_prompt_scoping(self):
        import vig_review_gate_common

        mlb = vig_review_gate_common.build_regular_review_prompt(
            "MLB", "2026-08-12", Path("/tmp/schedule.json"), [], True
        )
        self.assertIn("PROBABILITY COMPONENTS", mlb)
        soccer = vig_review_gate_common.build_regular_review_prompt(
            "SOCCER", "2026-08-12", Path("/tmp/schedule.json"), [], False
        )
        self.assertNotIn("PROBABILITY COMPONENTS", soccer)

    def test_lineup_recheck_prompt_includes_contract(self):
        import vig_review_gate_common
        from unittest.mock import patch

        with patch.object(
            vig_review_gate_common,
            "fetch_lineup_snapshot",
            side_effect=Exception("offline"),
        ):
            prompt, _ = vig_review_gate_common.build_lineup_recheck_prompt(
                Path("/tmp/schedule.json"), [{"id": "lineup-abc"}]
            )
        self.assertIn("PROBABILITY COMPONENTS", prompt)


class SharedNumberPredicateTests(unittest.TestCase):
    """This module validates recorded numbers with the SHARED rule.

    `_is_number` here was a verbatim copy of the body PR #69 deleted from
    `mlb_baseball_evidence`, and it carried the copy's defect: `math.isfinite`
    raises `OverflowError` on an integer too large to convert to a float, and
    `json.loads` parses one straight off a card. So a `10 ** 400` value made
    `probability_component_errors` RAISE instead of returning its error list —
    the same shape as PR #68's settlement grade, except this validator is on
    the execution path.
    """

    HUGE_INT = 10 ** 400

    def test_this_module_holds_the_same_object_as_the_write_gate(self):
        # Identity, not behaviour: a re-derived copy passes a behaviour test on
        # the day it is written and fails silently on the day one side moves.
        # That is exactly how this copy came to exist.
        self.assertIs(mlb_probability_model._is_number, write_gate_is_number)

    def test_the_component_entry_path_CALLS_the_shared_rule_rather_than_merely_importing_it(self):
        # `_is_number` being the shared object says nothing about whether
        # `validate_probability_components` consults it — the import name can
        # stay bound while the check is re-derived inline at the call site, and
        # the whole suite stays green (Reviewer proved that shape on PR #69's
        # first tip). So rebind the rule and require the ANSWER to follow, in
        # BOTH directions: a one-directional check is satisfied by a function
        # that returns False for any reason at all.
        message = (
            "probability_components.adjustments[0].delta must be a finite number"
        )
        with unittest.mock.patch.object(
            mlb_probability_model, "_is_number", lambda _v: False
        ):
            self.assertIn(message, probability_component_errors(candidate_trail()))
        with unittest.mock.patch.object(
            mlb_probability_model, "_is_number", lambda _v: True
        ):
            self.assertNotIn(
                message,
                probability_component_errors(
                    candidate_trail(
                        probability_components=valid_probability_components(
                            adjustments=[{
                                "component": "starter_run_prevention",
                                "delta": float("inf"),
                                "evidence": "would be rejected by the real rule",
                            }],
                        ),
                    )
                ),
            )

    def test_validate_probability_components_CALLS_the_shared_rule_at_BOTH_of_its_own_sites(self):
        # The test above pins the entry path (`_component_entries`), which is
        # where the huge-int defect surfaced. `validate_probability_components`
        # has two `_is_number` call sites of ITS OWN — the `uncertainty_haircut`
        # numeric check and the `conservative_probability` consistency check —
        # and neither was pinned: re-deriving the rule inline at the haircut
        # check, total and behaviourally identical with `_is_number` still bound
        # to the shared object, left the FULL suite green at 762 (Reviewer, PR
        # #70). Same selected-not-applied shape as #69's round-2 blocker, one
        # level down, inside the function the docstring named as covered.
        #
        # Two things make these sites awkward to pin, and both shaped the stubs:
        #
        # 1. A blanket `lambda _v: False` cannot reach either site.
        #    `_is_probability` is built on `_is_number`, so the `dk_fair_prob`
        #    gate returns early first.
        # 2. `validate_probability_components` returns as soon as the entry
        #    lists produce any error, so a stub keyed on a plain value that the
        #    fixture also uses inside a component entry (0.03 is both the
        #    haircut total AND the haircut component's amount) short-circuits
        #    before the site under test.
        #
        # So the stubs differ from the real rule at ONE probe OBJECT — a float
        # subclass carried only by the field under test — and agree with it
        # everywhere else. That is what makes the assertion about this site.
        real = mlb_probability_model._is_number

        class Probe(float):
            """A float the stub can recognise; the real rule accepts it."""

        self.assertTrue(real(Probe(0.03)), "the real rule must accept the probe")

        # --- site 1: the `uncertainty_haircut` numeric check ---
        # subTest so a failure names WHICH of the two sites regressed; the
        # test covers both by design, and the name says so, but on the day it
        # fires the label is what saves a bisect.
        with self.subTest(site="uncertainty_haircut numeric check"):
            message = "uncertainty_haircut must be a non-negative number"
            probed = candidate_trail(uncertainty_haircut=Probe(0.03))
            self.assertNotIn(
                message,
                probability_component_errors(probed),
                "positive control: the probe validates cleanly when unstubbed, "
                "so a failure below is the stub being consulted and not the "
                "probe itself",
            )
            with unittest.mock.patch.object(
                mlb_probability_model,
                "_is_number",
                lambda v: real(v) and not isinstance(v, Probe),
            ):
                # The shared rule now refuses this haircut, so the ANSWER must
                # follow. An inline copy keeps accepting it and stays silent.
                self.assertIn(message, probability_component_errors(probed))
            with unittest.mock.patch.object(
                mlb_probability_model,
                "_is_number",
                lambda v: real(v) or v == float("inf"),
            ):
                # And the other direction — a one-directional check is
                # satisfied by a stub that returns False for any reason at all.
                self.assertNotIn(
                    message,
                    probability_component_errors(
                        candidate_trail(uncertainty_haircut=float("inf"))
                    ),
                )

        # --- site 2: the `conservative_probability` consistency check ---
        with self.subTest(site="conservative_probability consistency check"):
            prefix = "conservative_probability must equal raw_probability - "

            def reported(trail):
                return any(
                    e.startswith(prefix)
                    for e in probability_component_errors(trail)
                )

            # raw 0.57 - haircut 0.03 is 0.54, so 0.10 is inconsistent and
            # reported.
            inconsistent = candidate_trail(conservative_probability=Probe(0.10))
            self.assertTrue(
                reported(inconsistent),
                "positive control: an inconsistent conservative_probability is "
                "reported when the shared rule is not stubbed",
            )
            with unittest.mock.patch.object(
                mlb_probability_model,
                "_is_number",
                lambda v: real(v) and not isinstance(v, Probe),
            ):
                # The check is GUARDED by the predicate, so refusing the value
                # must skip it. An inline copy keeps accepting and reporting.
                self.assertFalse(reported(inconsistent))
            with unittest.mock.patch.object(
                mlb_probability_model,
                "_is_number",
                lambda v: real(v) or v == float("inf"),
            ):
                self.assertTrue(
                    reported(
                        candidate_trail(conservative_probability=float("inf"))
                    )
                )

    def test__is_probability_CALLS_the_shared_rule_at_the_dk_fair_and_raw_gate(self):
        # The last unpinned site on the defect path (Reviewer, PR #70).
        # `_is_probability` is `_is_number(value) and 0 < value < 1`, and it is
        # where `dk_fair_prob` and `raw_probability` reach the rule —
        # `raw_probability = 10 ** 400` was one of the four fields that raised
        # `OverflowError` at 4a6f66e. Identity was shared, consultation was not
        # pinned: re-deriving the rule inline INSIDE `_is_probability` left the
        # full suite green at 763.
        #
        # The "shared rule REFUSES" direction is straightforward. The "shared
        # rule ACCEPTS" direction is not, and the reason is worth stating
        # because it constrains what this test can be: no plain value the
        # shared rule refuses is also inside `0 < v < 1`. `nan`, `inf`, `-inf`,
        # `10 ** 400` and both bools all fail the range clause too, so
        # loosening `_is_number` alone can never change the answer for any of
        # them — the range clause dominates. A probe that isolates the
        # predicate therefore has to be an object the range clause accepts and
        # the numeric rule does not, which is what `AcceptedByStub` is. It is
        # not a card shape and does not claim to be; it exists only to make the
        # predicate the single thing the answer depends on.
        real = mlb_probability_model._is_number
        message = (
            "dk_fair_prob and raw_probability must be numbers between 0 and 1 "
            "before components can be validated"
        )

        class Probe(float):
            """A float the stub can recognise; the real rule accepts it."""

        class AcceptedByStub:
            """In range, and not a number the real rule will ever accept.

            The `accepts` arm is only as wide as this object's DISAGREEMENTS
            with a drifted copy: any redundant clause the probe happens to
            SATISFY is a clause the arm cannot see. My first version defined
            only the comparisons and `__float__`, and it missed the canonical
            redundant clause this lane has been chasing since #69 —
            `_is_number(v) and value == value and 0 < v < 1`. Identity equality
            made `value == value` True for the probe, so the whole suite stayed
            green (Reviewer, PR #71).

            So the disagreements are deliberate, one per clause shape:

            - `__eq__` returns False, so a NaN-flavoured `value == value`
              clause changes the answer. `__hash__` is None because an
              `__eq__` override without it would be a silent lie about
              hashability, and nothing here needs to hash.
            - `__float__` returns NaN. It has to exist at all because the
              validator calls `float(raw)` downstream once the gate passes,
              and returning NaN is what makes a redundant `math.isfinite(v)`
              clause disagree too — `math.isfinite` converts through
              `__float__`, so a probe returning 0.57 there would satisfy that
              clause silently. The NaN then propagates into the sum
              comparisons, where every comparison is False, so no unrelated
              error appears to muddy the assertion.
            - `__abs__` and `__round__` are deliberately NOT defined, so
              `abs(v) != inf` or a `round(v)` opinion RAISES rather than
              quietly agreeing. An error is a worse diagnostic than a failure
              but it is still red; silently agreeing is the only outcome that
              is not.

            A clause this object satisfies is still invisible. That is a
            property of probe-based pins, not a claim this test can retire.
            """

            def __gt__(self, other):
                return 0.57 > other

            def __lt__(self, other):
                return 0.57 < other

            def __eq__(self, other):
                return False

            __hash__ = None

            def __float__(self):
                return float("nan")

        self.assertTrue(real(Probe(0.57)), "the real rule must accept the probe")
        self.assertFalse(
            real(AcceptedByStub()),
            "the real rule must refuse the object the stub will accept",
        )
        self.assertTrue(0 < AcceptedByStub() < 1, "the probe must pass the range clause")
        probe = AcceptedByStub()
        self.assertFalse(
            probe == probe,
            "the probe must DISAGREE with a NaN-flavoured `value == value` "
            "clause, or a copy layered with one is invisible to this arm",
        )
        self.assertFalse(
            math.isfinite(AcceptedByStub()),
            "the probe must DISAGREE with a redundant `math.isfinite` clause; "
            "isfinite converts through __float__, so a finite __float__ would "
            "satisfy that clause silently",
        )
        for opinion in (abs, round):
            with self.subTest(opinion=opinion.__name__):
                with self.assertRaises(TypeError):
                    opinion(AcceptedByStub())

        for field in ("raw_probability", "dk_fair_prob"):
            with self.subTest(field=field, direction="shared rule refuses"):
                probed = candidate_trail(**{field: Probe(0.57 if field == "raw_probability" else 0.55)})
                self.assertNotIn(
                    message,
                    probability_component_errors(probed),
                    "positive control: the probe validates cleanly when unstubbed",
                )
                with unittest.mock.patch.object(
                    mlb_probability_model,
                    "_is_number",
                    lambda v: real(v) and not isinstance(v, Probe),
                ):
                    # An inline copy keeps accepting the probe and stays silent.
                    self.assertIn(message, probability_component_errors(probed))

            with self.subTest(field=field, direction="shared rule accepts"):
                accepted = candidate_trail(**{field: AcceptedByStub()})
                self.assertIn(
                    message,
                    probability_component_errors(accepted),
                    "positive control: the real rule refuses this object",
                )
                with unittest.mock.patch.object(
                    mlb_probability_model,
                    "_is_number",
                    lambda v: real(v) or isinstance(v, AcceptedByStub),
                ):
                    # The other direction — a one-directional check is satisfied
                    # by a stub that returns False for any reason at all.
                    self.assertNotIn(message, probability_component_errors(accepted))

    def test_the_huge_raw_probability_is_reported_at_the_gate_not_raised(self):
        # The specific defect-path field this pin covers, kept as its own
        # named assertion so a regression says which field.
        errors = probability_component_errors(
            candidate_trail(raw_probability=self.HUGE_INT)
        )
        self.assertIn(
            "dk_fair_prob and raw_probability must be numbers between 0 and 1 "
            "before components can be validated",
            errors,
        )

    def test_the_validator_RETURNS_A_LIST_for_the_huge_integer_instead_of_raising(self):
        # The defect, at each field that reaches the predicate. Every one of
        # these raised `OverflowError` out of the validator before the fix.
        #
        # Named for what it asserts and nothing more: list-ness, not an error.
        # Three of the four fields do produce an error (covered by
        # test_every_unusable_number_is_reported_as_an_error and by the
        # consultation pins above), but `conservative_probability` returns an
        # EMPTY list — its consistency check is guarded by `_is_number` and is
        # simply skipped for a value the rule refuses, before and after this
        # change. That is bounded downstream rather than here:
        # `vig_review_gate_common` fails closed on any conservative_probability
        # outside `0 < x < 1`, which rejects a huge int by integer comparison
        # without ever converting it to a float. Calling this test "reports"
        # would have been the overclaim it exists to avoid.
        for label, trail in (
            ("adjustment delta", candidate_trail(
                probability_components=valid_probability_components(
                    adjustments=[{
                        "component": "starter_run_prevention",
                        "delta": self.HUGE_INT,
                        "evidence": "huge integer straight off a card",
                    }],
                ),
            )),
            ("uncertainty_haircut", candidate_trail(uncertainty_haircut=self.HUGE_INT)),
            ("raw_probability", candidate_trail(raw_probability=self.HUGE_INT)),
            ("conservative_probability",
             candidate_trail(conservative_probability=self.HUGE_INT)),
        ):
            for sign, value in (("+", 1), ("-", -1)):
                with self.subTest(field=label, sign=sign):
                    scaled = _rescale_huge(trail, value)
                    errors = probability_component_errors(scaled)
                    self.assertIsInstance(errors, list)

    def test_the_huge_integer_is_what_json_parses_off_a_card(self):
        # Without this the guard reads as defending against nothing: an
        # arbitrarily long integer literal is not hand-built, it is what
        # `json.loads` returns for a value already written to disk.
        parsed = json.loads('{"delta": 1' + "0" * 400 + "}")["delta"]
        self.assertIsInstance(parsed, int)
        self.assertEqual(parsed, self.HUGE_INT)
        self.assertFalse(mlb_probability_model._is_number(parsed))

    def test_every_unusable_number_is_reported_as_an_error(self):
        # Errors, not exceptions, for every shape the arithmetic cannot take.
        for label, value in (
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("-inf", float("-inf")),
            ("huge_int", HUGE := 10 ** 400),
            ("-huge_int", -HUGE),
        ):
            with self.subTest(value=label):
                errors = probability_component_errors(
                    candidate_trail(
                        probability_components=valid_probability_components(
                            adjustments=[{
                                "component": "starter_run_prevention",
                                "delta": value,
                                "evidence": "unusable value",
                            }],
                        ),
                    )
                )
                self.assertIn(
                    "probability_components.adjustments[0].delta must be a "
                    "finite number",
                    errors,
                )
        self.assertEqual(HUGE, self.HUGE_INT)

    def test_a_valid_contract_is_still_valid(self):
        # The regression rail: sharing the predicate must not reject anything
        # the contract accepted before. Finite positives, at the boundaries the
        # component bounds allow.
        self.assertEqual(probability_component_errors(candidate_trail()), [])
        for delta in (0.015, 0.005, -0.02, 0.0):
            with self.subTest(delta=delta):
                self.assertTrue(mlb_probability_model._is_number(delta))
        for value in (0, 1, -1, 0.5, 1e308, -1e308):
            with self.subTest(value=value):
                self.assertTrue(mlb_probability_model._is_number(value))
        for value in (True, False, None, "0.5", [1]):
            with self.subTest(value=repr(value)):
                self.assertFalse(mlb_probability_model._is_number(value))


def _rescale_huge(value, sign):
    """Return `value` with every `10 ** 400` replaced by `sign * 10 ** 400`.

    The signed variant matters because the old body rejected `-inf` and `nan`
    by accident but raised on an integer of either sign — the magnitude is what
    `math.isfinite` cannot convert, not the sign.
    """
    if sign == 1:
        return value
    huge = 10 ** 400
    if isinstance(value, dict):
        return {k: _rescale_huge(v, sign) for k, v in value.items()}
    if isinstance(value, list):
        return [_rescale_huge(v, sign) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and value == huge:
        return -huge
    return value


if __name__ == "__main__":
    unittest.main()
