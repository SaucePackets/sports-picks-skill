"""The selection handoff: a recorded decision has to agree with its own numbers.

PR #78 made the schedule land through code and PR #80 made its identity joins
corroborate. Both are about the RECORD. This is about the DECISION: on
2026-09-02 the slate enumerated fifteen games, four of them were called out in
prose as mismatch spots, and the day produced an empty candidate list with
narrative pass notes. Every validator in the repo passed it, because a
disposition and the numbers written beside it had never been compared.

Four rails, one shape — two facts written side by side and never joined:

1. ``net_edge`` is ``conservative_probability - polymarket_ask``, per side.
2. A carded game was priced and handicapped; a ``price_discipline`` refusal is
   not named on a side whose own numbers clear the deployed floor.
3. ``mlb_eligibility_report`` states that comparison for every game and side,
   on a DRAFT as well as a landed schedule, and never authors a disposition.
4. A detected recorder gap stops sharing the ``no_work`` outcome with an honest
   empty card.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vig_policy_state
from scripts import mlb_eligibility_report as report_mod
from scripts import mlb_game_reads
from scripts import mlb_runtime_policy
from scripts import vig_run_journal
from test_mlb_game_reads import read, schedule


def policy(**overrides):
    with tempfile.TemporaryDirectory() as tmp:
        return vig_policy_state.loaded_policy(Path(tmp), **overrides)


FLOOR = vig_policy_state.LIVE_MIN_CONSERVATIVE_EDGE


class NetEdgeCoherenceTests(unittest.TestCase):
    """A stored edge is a claim; the subtraction is the fact."""

    def test_an_edge_that_is_not_the_difference_is_refused_per_side(self):
        for side, bad in (("away", {"away": 0.10, "home": 0.045}),
                          ("home", {"away": -0.080, "home": 0.10})):
            with self.subTest(side=side):
                errors = mlb_game_reads.validate_read(read(net_edge=bad), 0)
                self.assertTrue(
                    any(f"net_edge.{side}" in error for error in errors),
                    f"a wrong {side} edge produced {errors}",
                )
                # And ONLY that side: an error naming both would not tell an
                # operator which number to fix.
                other = "home" if side == "away" else "away"
                self.assertFalse(
                    any(f"net_edge.{other}" in error for error in errors), errors
                )

    def test_the_coherent_edge_is_accepted_at_the_tolerance_boundary(self):
        inside = mlb_game_reads.COHERENCE_TOLERANCE * 0.9
        outside = mlb_game_reads.COHERENCE_TOLERANCE * 1.5
        base = read()["net_edge"]
        self.assertEqual(
            mlb_game_reads.validate_read(
                read(net_edge={"away": base["away"] + inside, "home": base["home"]}), 0
            ),
            [],
        )
        self.assertTrue(
            mlb_game_reads.validate_read(
                read(net_edge={"away": base["away"] + outside, "home": base["home"]}), 0
            )
        )

    def test_an_edge_with_nothing_to_subtract_is_refused(self):
        # The half-wired shape PR #74 was blocked on: excusing an operand is
        # exactly what removes the evidence, so it cannot also excuse the check.
        entry = read(
            polymarket_ask=None,
            unavailable={"polymarket_ask": "no Polymarket market for this game"},
            refusing_rails=["no_polymarket_market"],
        )
        entry["net_edge"] = {"away": -0.080, "home": 0.045}
        self.assertIn(
            "game_reads[0] records net_edge but not polymarket_ask; net_edge is "
            "conservative_probability minus polymarket_ask and cannot be checked "
            "without both",
            mlb_game_reads.validate_read(entry, 0),
        )

    def test_an_explained_missing_edge_on_a_priced_game_stays_legal(self):
        # Deliberately NOT required. The 2026-08 corpus contains reads that
        # priced and handicapped a game and never computed the edge;
        # mlb_measurement_lane counts that as a process failure. Refusing the
        # record here would relabel a real recorded state as malformed and hide
        # it from the report that measures it — and it would buy nothing,
        # because the floor rail recomputes the edge either way.
        entry = read(net_edge=None, unavailable={"net_edge": "the edge was never computed"})
        self.assertEqual(mlb_game_reads.validate_read(entry, 0), [])

    def test_the_floor_rail_still_bites_when_the_edge_was_never_written_down(self):
        # The positive control for the paragraph above: if declining to record
        # net_edge dodged the rail, the omission would be a bypass rather than
        # a disclosure.
        entry = read(
            polymarket_ask={"away": 0.460, "home": 0.500},
            net_edge=None,
            unavailable={"net_edge": "the edge was never computed"},
            refusing_rails=["price_discipline"],
        )
        errors = mlb_game_reads.policy_disposition_errors(
            {"game_reads": [entry]}, policy()
        )
        self.assertTrue(
            any("price_discipline" in error and "home" in error for error in errors),
            errors,
        )

    def test_side_edges_are_recomputed_rather_than_read_off_the_field(self):
        # A pin on the SOURCE, not on the value: an assertion that merely
        # equalled the recorded field would pass just as well if the function
        # read the field.
        entry = read()
        entry["net_edge"] = {"away": 0.0, "home": 0.0}
        edges = mlb_game_reads.side_edges(entry)
        self.assertAlmostEqual(edges["away"], 0.380 - 0.460, places=12)
        self.assertAlmostEqual(edges["home"], 0.590 - 0.545, places=12)


class DispositionAgreesWithItsNumbersTests(unittest.TestCase):
    def test_a_card_on_a_game_that_was_never_priced_is_refused(self):
        entry = read(
            disposition="candidate",
            refusing_rails=[],
            polymarket_ask=None,
            net_edge=None,
            unavailable={
                "polymarket_ask": "no Polymarket market for this game",
                "net_edge": "no ask to price against",
            },
        )
        self.assertIn(
            "game_reads[0].disposition is 'candidate' but the read records no "
            "polymarket_ask; a game that was not priced and handicapped cannot be carded",
            mlb_game_reads.validate_read(entry, 0),
        )

    def test_a_card_with_no_model_trail_is_refused(self):
        entry = read(
            disposition="lineup_watchlist",
            refusing_rails=[],
            raw_probability=None,
            uncertainty_haircut=None,
            conservative_probability=None,
            model_version=None,
            net_edge=None,
            unavailable={
                "raw_probability": "never handicapped",
                "uncertainty_haircut": "never handicapped",
                "conservative_probability": "never handicapped",
                "model_version": "never handicapped",
                "net_edge": "never handicapped",
            },
        )
        errors = mlb_game_reads.validate_read(entry, 0)
        self.assertTrue(
            any("cannot be carded" in error for error in errors), errors
        )
        for field in mlb_game_reads.MODEL_TRAIL_FIELDS:
            self.assertTrue(
                any(field in error and "cannot be carded" in error for error in errors),
                f"{field} missing from {errors}",
            )

    def test_a_fully_priced_card_is_accepted(self):
        # The contrast case. Without it the rule above would be satisfied by a
        # validator that refused every candidate.
        entry = read(
            disposition="candidate",
            refusing_rails=[],
            polymarket_ask={"away": 0.300, "home": 0.545},
            net_edge={"away": 0.080, "home": 0.045},
        )
        self.assertEqual(mlb_game_reads.validate_read(entry, 0), [])

    def test_a_price_refusal_on_a_side_that_clears_the_floor_is_refused(self):
        # Rebecca's case: `pass` / price_discipline with an edge that clears.
        entry = read(
            polymarket_ask={"away": 0.460, "home": 0.480},
            net_edge={"away": -0.080, "home": 0.110},
            refusing_rails=["price_discipline"],
        )
        errors = mlb_game_reads.policy_disposition_errors(
            {"game_reads": [entry]}, policy()
        )
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("names 'price_discipline'", errors[0])
        self.assertIn("home", errors[0])
        self.assertIn(f"{FLOOR:.4f} floor", errors[0])

    def test_a_price_refusal_below_the_floor_is_left_alone(self):
        self.assertEqual(
            mlb_game_reads.policy_disposition_errors({"game_reads": [read()]}, policy()),
            [],
        )

    def test_the_floor_is_the_deployed_one_and_not_a_constant_here(self):
        # Move the policy and the same read changes answer. An assertion
        # against 0.05 would pass identically if the module hard-coded it.
        entry = read()  # home edge 0.045, under the live 0.05 floor
        self.assertEqual(
            mlb_game_reads.policy_disposition_errors({"game_reads": [entry]}, policy()), []
        )
        self.assertTrue(
            mlb_game_reads.policy_disposition_errors(
                {"game_reads": [entry]}, policy(min_conservative_edge=0.04)
            )
        )

    def test_an_unloadable_policy_makes_the_claim_uncheckable_rather_than_true(self):
        errors = mlb_game_reads.policy_disposition_errors({"game_reads": [read()]}, None)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("cannot be checked against the edge floor", errors[0])

    def test_a_read_that_never_names_the_price_rail_needs_no_policy(self):
        # Fail-closed, not fail-loud: a day whose refusals do not turn on the
        # floor is not blocked by the absence of one.
        entry = read(refusing_rails=["starter_floor"])
        self.assertEqual(
            mlb_game_reads.policy_disposition_errors({"game_reads": [entry]}, None), []
        )

    def test_the_boundary_function_will_not_default_the_policy(self):
        # An optional rail is the shape of defect this lane keeps paying for:
        # a caller that forgets the floor must get a TypeError, not silence.
        with self.assertRaises(TypeError):
            mlb_game_reads.validate_with_denominator(Path("x"), schedule())


class EligibilityReportTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy()

    def test_a_draft_and_the_schedule_it_lands_as_report_identically(self):
        # The whole point of "before publishing": a preflight view that could
        # differ from the flight would be worse than none. `land()` adds the
        # denominator and canonicalises ids; neither is an input here.
        reads = [read(823509), read(824876, away="New York Mets", home="Chicago Cubs")]
        draft = {
            "date": "2026-08-22",
            "sport": "MLB",
            "market_type": "moneyline",
            "candidates": [],
            "lineup_watchlist": [],
            "game_reads": reads,
        }
        landed = schedule(reads)
        draft_report = report_mod.build_report(draft, self.policy)
        landed_report = report_mod.build_report(landed, self.policy)
        for key in ("games", "counts", "status", "day"):
            self.assertEqual(draft_report[key], landed_report[key], key)

    def test_every_side_carries_the_numbers_an_operator_asked_for(self):
        row = report_mod.build_report(schedule(), self.policy)["games"][0]["sides"][0]
        self.assertEqual(
            sorted(row),
            sorted(
                [
                    "side", "team", "dk_fair_prob", "polymarket_ask",
                    "raw_probability", "conservative_probability",
                    "net_edge_recorded", "net_edge_recomputed",
                    "max_polymarket_price", "verdict",
                ]
            ),
        )
        self.assertEqual(row["polymarket_ask"], 0.460)
        self.assertEqual(row["net_edge_recomputed"], round(0.380 - 0.460, 6))
        # The ceiling comes from the policy's own function, so it moves with the
        # floor rather than restating `conservative - 0.05` here.
        self.assertEqual(row["max_polymarket_price"], self.policy.ceiling_for(0.380))

    def test_a_side_clearing_the_floor_is_eligible_and_one_below_it_is_not(self):
        entry = read(
            polymarket_ask={"away": 0.460, "home": 0.480},
            net_edge={"away": -0.080, "home": 0.110},
            refusing_rails=["starter_floor"],
        )
        game = report_mod.build_report(schedule([entry]), self.policy)["games"][0]
        verdicts = {row["side"]: row["verdict"] for row in game["sides"]}
        self.assertEqual(verdicts["home"], report_mod.SIDE_ELIGIBLE)
        self.assertEqual(verdicts["away"], report_mod.SIDE_BELOW_FLOOR)
        self.assertEqual(game["verdict"], report_mod.SIDE_ELIGIBLE)

    def test_an_unpriced_and_an_unhandicapped_side_are_different_verdicts(self):
        unpriced = read(
            polymarket_ask=None,
            net_edge=None,
            disposition="not_priced",
            refusing_rails=["no_polymarket_market"],
            unavailable={
                "polymarket_ask": "no Polymarket market for this game",
                "net_edge": "no ask to price against",
            },
        )
        unhandicapped = read(
            raw_probability=None,
            uncertainty_haircut=None,
            conservative_probability=None,
            model_version=None,
            net_edge=None,
            refusing_rails=["incomplete_input_data"],
            unavailable={
                "raw_probability": "starter missing for Colorado",
                "uncertainty_haircut": "never handicapped",
                "conservative_probability": "never handicapped",
                "model_version": "never handicapped",
                "net_edge": "never handicapped",
            },
        )
        games = report_mod.build_report(
            {"game_reads": [unpriced, unhandicapped]}, self.policy
        )["games"]
        self.assertEqual(games[0]["verdict"], report_mod.SIDE_NOT_PRICED)
        self.assertEqual(games[1]["verdict"], report_mod.SIDE_UNHANDICAPPED)

    def test_a_pass_on_a_clearing_edge_naming_only_price_disagrees(self):
        entry = read(
            polymarket_ask={"away": 0.460, "home": 0.480},
            net_edge={"away": -0.080, "home": 0.110},
            refusing_rails=["price_discipline"],
        )
        game = report_mod.build_report(schedule([entry]), self.policy)["games"][0]
        self.assertEqual(game["agreement"], report_mod.DISAGREES)

    def test_a_pass_on_a_clearing_edge_with_a_baseball_rail_agrees(self):
        # The degree of freedom the report must NOT take away: refusing an
        # eligible price on a starter or bullpen read is what those rails are
        # for. Calling that a contradiction would be the report ruling on the
        # candidate, which PR #79 forbids.
        entry = read(
            polymarket_ask={"away": 0.460, "home": 0.480},
            net_edge={"away": -0.080, "home": 0.110},
            refusing_rails=["starter_floor"],
        )
        game = report_mod.build_report(schedule([entry]), self.policy)["games"][0]
        self.assertEqual(game["agreement"], report_mod.AGREES)
        self.assertIn("starter_floor", game["agreement_detail"])

    def test_a_card_with_no_eligible_side_disagrees(self):
        entry = read(disposition="candidate", refusing_rails=[])
        game = report_mod.build_report(schedule([entry]), self.policy)["games"][0]
        self.assertEqual(game["agreement"], report_mod.DISAGREES)
        self.assertIn("no side is eligible", game["agreement_detail"])

    def test_a_malformed_read_is_reported_as_a_defect_not_as_a_quiet_day(self):
        entry = read(refusing_rails=["not_a_real_rail"])
        game = report_mod.build_report(schedule([entry]), self.policy)["games"][0]
        self.assertTrue(game["read_errors"])
        self.assertEqual(
            report_mod.build_report(schedule([entry]), self.policy)["counts"][
                "reads_with_errors"
            ],
            1,
        )

    def test_counts_are_zero_filled_over_every_closed_vocabulary(self):
        counts = report_mod.build_report(schedule(), self.policy)["counts"]
        for verdict in report_mod.SIDE_VERDICTS:
            self.assertIn(f"verdict_{verdict}", counts)
        for disposition in mlb_game_reads.DISPOSITIONS:
            self.assertIn(f"disposition_{disposition}", counts)
        # A category that never occurred prints 0; that is how a reader tells a
        # constant axis from an impossible one.
        self.assertEqual(counts["verdict_eligible"], 0)
        self.assertEqual(counts["disposition_pass"], 1)

    def test_an_empty_read_list_is_an_honest_zero_and_a_missing_one_is_not(self):
        # Found by running this report against the REAL 2026-09-02 schedule,
        # which carries no game_reads at all: the first version returned the
        # same status for that file as for a day the scan found no games —
        # exactly the collapse the receipt's honest_zero/recorder_failed split
        # exists to prevent, reintroduced one artifact over.
        empty = report_mod.build_report({"date": "2026-09-02", "game_reads": []}, self.policy)
        self.assertEqual(empty["status"], report_mod.STATUS_OK)
        self.assertEqual(empty["counts"]["games"], 0)
        missing = report_mod.build_report({"date": "2026-09-02"}, self.policy)
        self.assertEqual(missing["status"], report_mod.STATUS_NO_READS)

    def test_a_missing_policy_computes_no_verdicts_at_all(self):
        report = report_mod.build_report(schedule(), None)
        self.assertEqual(report["status"], report_mod.STATUS_POLICY_UNAVAILABLE)
        self.assertEqual(report["games"], [])
        # And the low-level row builder refuses rather than inventing a floor.
        with self.assertRaises(ValueError):
            report_mod.side_row(read(), "away", None)

    def test_the_report_writes_nothing_and_names_no_new_decision(self):
        # Read-only in the sense that matters: the document it was handed is
        # unchanged, byte for byte.
        document = schedule()
        before = json.dumps(document, sort_keys=True)
        report_mod.build_report(document, self.policy)
        self.assertEqual(json.dumps(document, sort_keys=True), before)


class EligibilityReportCliTests(unittest.TestCase):
    def _run(self, document, flag="--schedule", **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "document.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            state = vig_policy_state.write_policy(Path(tmp) / "state", **kwargs)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = report_mod.main([flag, str(path), "--state-dir", str(state)])
            return status, out.getvalue()

    def test_a_coherent_slate_exits_zero(self):
        status, output = self._run(schedule())
        self.assertEqual(status, 0, output)
        self.assertIn("Atlanta Braves at Milwaukee Brewers", output)

    def test_a_contradiction_exits_nonzero(self):
        entry = read(
            polymarket_ask={"away": 0.460, "home": 0.480},
            net_edge={"away": -0.080, "home": 0.110},
            refusing_rails=["price_discipline"],
        )
        status, output = self._run(schedule([entry]))
        self.assertEqual(status, 1)
        self.assertIn(report_mod.DISAGREES, output)

    def test_a_draft_is_accepted_by_the_same_command(self):
        draft = {
            "date": "2026-08-22",
            "sport": "MLB",
            "market_type": "moneyline",
            "game_reads": [read()],
        }
        status, output = self._run(draft, flag="--draft")
        self.assertEqual(status, 0, output)

    def test_json_output_is_the_report_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(schedule()), encoding="utf-8")
            state = vig_policy_state.write_policy(Path(tmp) / "state")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                report_mod.main(
                    ["--schedule", str(path), "--json", "--state-dir", str(state)]
                )
            payload = json.loads(out.getvalue())
        self.assertEqual(payload["schema"], report_mod.REPORT_SCHEMA)
        self.assertEqual(payload["min_conservative_edge"], FLOOR)

    def test_a_missing_policy_exits_nonzero_rather_than_reporting_a_clean_slate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text(json.dumps(schedule()), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = report_mod.main(
                    ["--schedule", str(path), "--state-dir", str(Path(tmp) / "empty")]
                )
        self.assertEqual(status, 1)
        self.assertIn(report_mod.STATUS_POLICY_UNAVAILABLE, out.getvalue())

    def test_the_report_places_no_order_and_opens_no_socket(self):
        # The claim is structural, not a promise in a docstring: the module's
        # sibling imports are the two it needs and nothing that can trade.
        import import_closure

        self.assertEqual(
            import_closure.sibling_imports("mlb_eligibility_report.py"),
            {"mlb_game_reads.py", "mlb_runtime_policy.py", "numeric_util.py"},
        )


class RecorderFailureIsNotNoWorkTests(unittest.TestCase):
    """A detected recorder gap and an honest empty card are different days."""

    def setUp(self):
        from scripts import vig_review_gate_common

        self.gate = vig_review_gate_common
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        original = self.gate.ROOT
        self.gate.ROOT = self.root
        self.addCleanup(lambda: setattr(self.gate, "ROOT", original))
        self.day = self.gate.schedule_day_now()
        self.schedule_path = self.root / ".picks" / "execute" / f"{self.day}-schedule.json"
        self.schedule_path.parent.mkdir(parents=True)
        self.scan_path = self.root / ".picks" / "tmp" / f"stage2-{self.day}.json"
        self.scan_path.parent.mkdir(parents=True)
        self.stack.enter_context(vig_policy_state.deployed_policy(self.root / "state"))

    def _run(self, payload, scan=None):
        self.schedule_path.write_text(json.dumps(payload), encoding="utf-8")
        if scan is not None:
            self.scan_path.write_text(json.dumps(scan), encoding="utf-8")
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    self.gate, "standing_authorization_enabled", return_value=True
                )
            )
            stack.enter_context(contextlib.redirect_stdout(out))
            status = self.gate.run_gate("MLB")
        return status, out.getvalue()

    def _outcomes(self):
        records, _errors = vig_run_journal.read_records(
            vig_run_journal.journal_path(self.root, self.day)
        )
        return [record.get("outcome") for record in records]

    def test_an_unrecorded_day_journals_its_own_outcome(self):
        # THE 2026-09-02 SHAPE, in the field that operational surfaces read.
        # The gate caught the gap forty-one times that day and wrote `no_work`
        # every time; only `stage` carried the difference, and nothing reads it.
        status, _output = self._run(
            {"date": self.day, "candidates": [], "lineup_watchlist": []}
        )
        self.assertEqual(self._outcomes(), [vig_run_journal.OUTCOME_RECORDER_FAILED])
        # Exit code deliberately unchanged: a measurement defect must not take
        # the reviewer offline.
        self.assertEqual(status, 0)

    def test_an_honest_empty_card_still_journals_no_work(self):
        status, _output = self._run(schedule([], date=self.day), scan=[])
        self.assertEqual(self._outcomes(), [vig_run_journal.OUTCOME_NO_WORK])
        self.assertEqual(status, 0)

    def test_the_new_outcome_is_not_a_pass(self):
        self.assertIn(vig_run_journal.OUTCOME_RECORDER_FAILED, vig_run_journal.OUTCOMES)
        self.assertNotIn(
            vig_run_journal.OUTCOME_RECORDER_FAILED, vig_run_journal.PASS_OUTCOMES
        )

    def test_the_journal_accepts_and_renders_the_new_outcome(self):
        record = vig_run_journal.build_record(
            sport="MLB",
            day=self.day,
            outcome=vig_run_journal.OUTCOME_RECORDER_FAILED,
            stage=self.gate.RECORDER_GAP_STAGE,
            detail="the day's refusals were not recorded",
        )
        self.assertIn(
            vig_run_journal.OUTCOME_RECORDER_FAILED,
            vig_run_journal.format_record(record),
        )

    def test_the_gate_and_the_receipt_use_the_same_word_for_the_same_state(self):
        # Two names for one state is how the honest_zero/recorder_failed split
        # gets lost again, one artifact over.
        from scripts import mlb_slate_receipt

        self.assertEqual(
            vig_run_journal.OUTCOME_RECORDER_FAILED,
            mlb_slate_receipt.VERDICT_RECORDER_FAILED,
        )


class TheDocPromisesOnlyRailsThatExistTests(unittest.TestCase):
    """A doc promising a rail the code lacks is worse than no rail at all.

    That is this lane's most expensive lesson (PR #80, round 1): nobody looks
    again at a case the instructions say is handled. These pin the three claims
    the run acts on.
    """

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[1]
        self.mlb_md = (
            self.repo_root / "skills" / "sports-picks" / "references" / "mlb.md"
        ).read_text(encoding="utf-8")
        self.skill_md = (
            self.repo_root / "skills" / "sports-picks" / "SKILL.md"
        ).read_text(encoding="utf-8")

    def test_the_command_the_docs_tell_the_run_to_type_is_a_command_it_accepts(self):
        # Not "the string appears": the string is fed to the module's own
        # parser. A documented flag the CLI does not define is an instruction
        # that fails on a live slate night having passed every test here.
        self.assertIn(
            "python3 scripts/mlb_eligibility_report.py --draft", self.mlb_md
        )
        self.assertIn(
            "python3 scripts/mlb_eligibility_report.py --draft", self.skill_md
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft.json"
            path.write_text(json.dumps({"game_reads": []}), encoding="utf-8")
            for flag in ("--draft", "--schedule"):
                with self.subTest(flag=flag):
                    with contextlib.redirect_stdout(io.StringIO()):
                        report_mod.main(
                            [flag, str(path), "--state-dir", str(Path(tmp) / "state")]
                        )

    def test_the_docs_name_the_floor_as_policy_rather_than_restating_a_number(self):
        # The floor has moved before. A doc quoting 0.05 would be a second copy
        # of a value the loader owns, and the drift would be invisible.
        for text in (self.mlb_md,):
            self.assertIn("min_conservative_edge", text)
            self.assertIn("deployed policy", text)

    def test_the_docs_state_the_rule_the_validator_actually_enforces(self):
        self.assertIn(
            "`conservative_probability - polymarket_ask`", self.mlb_md
        )
        self.assertIn("price_discipline", self.mlb_md)


class PolicyIsLoadedNotRestatedTests(unittest.TestCase):
    def test_a_policy_the_loader_could_not_supply_makes_the_rail_uncheckable(self):
        # Named for what it proves. It patches the loader and then hands the
        # loader's own answer back in as the argument, so it reduces to
        # `policy_disposition_errors(entry, None)` — a second spelling of the
        # no-policy case above, not a consultation pin. The rail takes the floor
        # as a parameter; it cannot read the module, so consultation can only be
        # observed at a CALL SITE. Two pins there, in
        # ThePriceRailIsLoadBearingAtEveryBoundaryTests below:
        # `test_the_gate_loads_the_policy_rather_than_passing_none` catches a
        # caller that passes NO floor, and
        # `test_the_gate_reads_the_deployed_floor_rather_than_restating_it`
        # catches one that passes a floor of its own — the class name's claim
        # rests on the second, because the fixture floor and a plausible
        # hardcoded literal are the same number.
        entry = read()  # home edge 0.045
        with mock.patch.object(
            mlb_runtime_policy, "load_mlb_selection_policy", return_value=None
        ):
            self.assertTrue(
                mlb_game_reads.policy_disposition_errors(
                    {"game_reads": [entry]},
                    mlb_runtime_policy.load_mlb_selection_policy(),
                )
            )

    def test_the_deployed_spelling_of_the_policy_block_loads(self):
        # The live box writes `mlb_policy` with `policy_effective_at`, not the
        # reviewed `mlb_selection_policy`/`effective_at` spelling. A fixture
        # that only exercised the latter would pass while the deployed file
        # failed closed — which is the state this whole lane was opened over.
        self.assertIsNotNone(policy())
        self.assertEqual(policy().min_conservative_edge, FLOOR)


class ThePriceRailIsLoadBearingAtEveryBoundaryTests(unittest.TestCase):
    """The rail is correct. These are about whether anything would notice it go.

    Reviewer, round 1: ``policy_disposition_errors`` is enforced at four call
    sites and deleting it from any one of them left the whole suite green.
    Every boundary fixture in the repo makes the rail a no-op — the gate's names
    no rails at all, the writer's names ``price_discipline`` with a home edge of
    0.045 under a 0.05 floor — so the rail fired in unit tests calling it
    directly and nowhere a caller actually stands. That is this lane's own
    thesis one level up: a check that exists, reads correctly, and is never
    reached. Same shape as PR #77 blocker 2, where deleting a whole report site
    left the suite byte-identical.

    Every case below turns on ONE pair of records that differ in a single input
    and straddle the deployed floor, and the first test proves that premise
    rather than assuming it: both are otherwise valid reads, so nothing except
    the price rail can tell them apart. Each boundary is asserted in BOTH
    directions, because a boundary that refuses the offending record is only
    half the claim — a site that refused everything would satisfy it too.

    The accepting half pins that the gate passed SOME floor. Making its
    ``load_mlb_selection_policy()`` call load-bearing takes one more case,
    because the fixture floor is bit-for-bit the number a restatement would
    hardcode: ``test_the_gate_reads_the_deployed_floor_rather_than_restating_it``
    moves the deployed floor under an unchanged file and requires the verdict to
    follow. Without it a hardcoded literal at that call site is invisible.
    """

    GAME_PK = 823509

    def setUp(self):
        from scripts import mlb_slate_receipt
        from scripts import mlb_slate_writer
        from scripts import vig_review_gate_common

        self.receipt_mod = mlb_slate_receipt
        self.writer = mlb_slate_writer
        self.gate = vig_review_gate_common
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.root / ".picks" / "execute").mkdir(parents=True)
        (self.root / ".picks" / "tmp").mkdir(parents=True)
        original = self.gate.ROOT
        self.gate.ROOT = self.root
        self.addCleanup(lambda: setattr(self.gate, "ROOT", original))
        # The gate reads today's schedule and cannot be told a different day,
        # so every boundary here shares the day the gate would look for.
        self.day = self.gate.schedule_day_now()
        self.stack.enter_context(vig_policy_state.deployed_policy(self.root / "state"))
        self.scan_path = self.root / ".picks" / "tmp" / f"stage2-{self.day}.json"

    # --- the one pair of records every case turns on

    def _read(self, *, clearing):
        """The same read twice: an honest price refusal, and one that is not.

        ``read()`` recomputes ``net_edge`` from the ask it is handed, so moving
        the home ask moves the edge with it and the record stays internally
        coherent — which is the point. A record that ALSO tripped the net_edge
        rail would be refused everywhere for a second reason and prove nothing
        about the floor.
        """
        return read(
            self.GAME_PK,
            polymarket_ask={"away": 0.460, "home": 0.500 if clearing else 0.545},
        )

    def _draft(self, *, clearing):
        return {
            "date": self.day,
            "sport": "MLB",
            "market_type": "moneyline",
            "candidates": [],
            "lineup_watchlist": [],
            "game_reads": [self._read(clearing=clearing)],
        }

    def _write_scan(self):
        entry = self._read(clearing=False)
        rows = [{key: entry[key] for key in ("game_pk", "event_id", "away", "home")}]
        self.scan_path.write_text(json.dumps(rows), encoding="utf-8")

    def _landed(self, *, clearing):
        """A schedule the writer itself certified, then moved one number.

        The offending schedule is NOT hand-written. Composing it here would risk
        it being refused downstream for some defect I introduced, and the test
        would pass with the price rail deleted. Landing the honest draft first
        means the file the gate, the receipt and the CLI are handed differs from
        a record ``land()`` accepted in exactly the home ask and the edge that
        follows from it.
        """
        self._write_scan()
        path, schedule = self.writer.land(
            self.root, self.day, self._draft(clearing=False)
        )
        if clearing:
            entry = dict(schedule["game_reads"][0])
            entry["polymarket_ask"] = {"away": 0.460, "home": 0.500}
            entry["net_edge"] = {"away": -0.080, "home": 0.090}
            schedule["game_reads"] = [entry]
            path.write_text(json.dumps(schedule), encoding="utf-8")
        return path, schedule

    def _run_gate(self):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    self.gate, "standing_authorization_enabled", return_value=True
                )
            )
            stack.enter_context(contextlib.redirect_stdout(out))
            status = self.gate.run_gate("MLB")
        return status, out.getvalue()

    def _records(self):
        records, _errors = vig_run_journal.read_records(
            vig_run_journal.journal_path(self.root, self.day)
        )
        return records

    def _names_the_rail(self, errors):
        return [
            error
            for error in errors
            if mlb_game_reads.PRICE_RAIL in error and "home" in error
        ]

    # --- the premise, asserted rather than assumed

    def test_the_pair_differs_in_one_input_and_straddles_the_floor(self):
        honest, clearing = self._read(clearing=False), self._read(clearing=True)
        moved = ("polymarket_ask", "net_edge")
        self.assertEqual(
            {key: value for key, value in honest.items() if key not in moved},
            {key: value for key, value in clearing.items() if key not in moved},
        )
        self.assertIn(mlb_game_reads.PRICE_RAIL, honest["refusing_rails"])
        self.assertIn(mlb_game_reads.PRICE_RAIL, clearing["refusing_rails"])
        self.assertLess(mlb_game_reads.side_edges(honest)["home"], FLOOR)
        self.assertGreaterEqual(mlb_game_reads.side_edges(clearing)["home"], FLOOR)
        # The load-bearing half: BOTH records pass every rail that does not need
        # the policy. So a boundary that refuses the clearing one is refusing it
        # for the floor and nothing else, and a boundary that accepts the honest
        # one has actually applied a floor rather than skipped the rail.
        self.assertEqual(mlb_game_reads.validate_read(honest, 0), [])
        self.assertEqual(mlb_game_reads.validate_read(clearing, 0), [])

    # --- boundary 1: the producer

    def test_land_refuses_a_clearing_price_discipline_read(self):
        path, _schedule = self._landed(clearing=False)
        before = path.read_bytes()

        with self.assertRaises(self.writer.SlateWriteError) as caught:
            self.writer.land(self.root, self.day, self._draft(clearing=True))

        self.assertTrue(
            self._names_the_rail(caught.exception.errors), caught.exception.errors
        )
        # Refused BEFORE anything was written: the previous day's record is the
        # one artifact a failed landing must not damage.
        self.assertEqual(path.read_bytes(), before)

    def test_land_accepts_the_honest_price_discipline_read(self):
        path, _schedule = self._landed(clearing=False)
        self.assertTrue(path.exists())

    # --- boundary 2: the scheduled gate

    def test_the_gate_journals_a_clearing_price_discipline_read_as_recorder_failed(self):
        self._landed(clearing=True)

        status, _output = self._run_gate()

        records = self._records()
        self.assertEqual(
            [record["outcome"] for record in records],
            [vig_run_journal.OUTCOME_RECORDER_FAILED],
        )
        self.assertTrue(self._names_the_rail([records[0]["detail"]]), records[0])
        # Exit code deliberately unchanged: a measurement defect must not take
        # the reviewer offline (PR #77's design, unaltered here).
        self.assertEqual(status, 0)

    def test_the_gate_loads_the_policy_rather_than_passing_none(self):
        # THE CONSULTATION PIN, and the only one in the suite. With no floor a
        # `price_discipline` claim is UNCHECKABLE and the rail says so, so a
        # gate that passed None would report this ordinary, honest day as a
        # recorder failure. The offending case above cannot see that — it is
        # refused either way, for two different reasons.
        self._landed(clearing=False)

        status, _output = self._run_gate()

        self.assertEqual(
            [record["outcome"] for record in self._records()],
            [vig_run_journal.OUTCOME_NO_WORK],
        )
        self.assertEqual(status, 0)

    def test_the_gate_reads_the_deployed_floor_rather_than_restating_it(self):
        # THE CONSULTATION PIN. Reviewer, round 2: the test above pins that a
        # floor was PASSED, not that it was LOADED — replacing the gate's
        # `load_mlb_selection_policy()` with a hardcoded 0.05 literal passed the
        # whole suite, because the fixture floor is bit-for-bit the number a
        # restatement would hardcode. The gate's own comment names both harms;
        # skipping the rail was pinned and GUESSING the floor was not, and
        # guessing is the half that survives the floor moving.
        #
        # So move the DEPLOYED floor under the record and require the verdict to
        # follow. The bytes on disk never change: the honest read lands under the
        # 0.05 policy the writer certified it against, and only then does the
        # deployed floor become 0.04 — so its `price_discipline` claim now sits
        # on a side that clears, and the gate must refuse the day it accepted a
        # moment ago. A gate restating 0.05 journals `no_work` and this reds.
        #
        # 0.04 and not 0.07: at 0.07 both records keep their current verdicts,
        # so the case would discriminate nothing.
        self._landed(clearing=False)
        self.assertLess(0.04, mlb_game_reads.side_edges(self._read(clearing=False))["home"])

        with vig_policy_state.deployed_policy(
            self.root / "state", min_conservative_edge=0.04
        ):
            status, _output = self._run_gate()

        records = self._records()
        self.assertEqual(
            [record["outcome"] for record in records],
            [vig_run_journal.OUTCOME_RECORDER_FAILED],
        )
        self.assertTrue(self._names_the_rail([records[0]["detail"]]), records[0])
        self.assertEqual(status, 0)

    # --- boundary 3: the receipt

    def test_the_receipt_does_not_call_that_day_complete(self):
        self._landed(clearing=True)

        receipt = self.receipt_mod.build_receipt(self.root, self.day)

        self.assertEqual(receipt["verdict"], self.receipt_mod.VERDICT_RECORDER_FAILED)
        self.assertTrue(
            self._names_the_rail(receipt["recorder_errors"]), receipt["recorder_errors"]
        )

    def test_the_receipt_still_calls_the_honest_day_complete(self):
        self._landed(clearing=False)

        receipt = self.receipt_mod.build_receipt(self.root, self.day)

        self.assertEqual(receipt["verdict"], self.receipt_mod.VERDICT_COMPLETE)
        self.assertEqual(receipt["recorder_errors"], [])

    # --- boundary 4: the command an operator types

    def test_the_cli_refuses_the_same_record_and_names_the_rail(self):
        path, _schedule = self._landed(clearing=True)
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            status = mlb_game_reads.main([str(path)])

        payload = json.loads(out.getvalue())
        self.assertEqual(status, 1)
        self.assertTrue(self._names_the_rail(payload["errors"]), payload)

    def test_the_cli_accepts_the_honest_record(self):
        path, _schedule = self._landed(clearing=False)
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            status = mlb_game_reads.main([str(path)])

        self.assertEqual(status, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
