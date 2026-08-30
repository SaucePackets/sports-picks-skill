"""Tests for the read-only historical pick replay/attribution report.

The properties that matter most here are the honesty ones: controls are never
bets, synthetic economics say None where there is no evidence, and the
leave-one-period-out grader can be PROVEN never to tune and grade on the same
slice — the last of these with a fixture where in-sample selection and
out-of-sample selection genuinely disagree, so a leak changes the answer.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_closure  # noqa: E402
import vig_historical_audit as audit  # noqa: E402
import vig_pick_replay as replay  # noqa: E402
from test_vig_historical_audit import statsapi_payload, write_day, write_results  # noqa: E402

MODULE = "vig_pick_replay.py"


def record(**overrides):
    """A minimal audited record with the keys the replay layer reads."""
    base = {
        "date": "2026-06-10",
        "game": "A at B",
        "side_raw": "A",
        "resolved_side": "A",
        "disposition": "executed",
        "skip_reason": None,
        "side_outcome": "win",
        "entry_price": 0.5,
        "slate_price": None,
        "stated_probability": None,
        "confidence": None,
        "conservative_probability": None,
    }
    base.update(overrides)
    return base


class SyntheticUnitsTests(unittest.TestCase):
    def test_flat_one_unit_payouts_at_the_effective_price(self):
        self.assertAlmostEqual(replay.synthetic_units(record(entry_price=0.5)), 1.0)
        self.assertAlmostEqual(replay.synthetic_units(record(entry_price=0.25)), 3.0)
        self.assertEqual(replay.synthetic_units(record(side_outcome="loss")), -1.0)
        self.assertEqual(replay.synthetic_units(record(side_outcome="push")), 0.0)

    def test_no_evidence_is_none_never_zero(self):
        # "no synthetic result" and "broke even" are different facts; a zero
        # here would flow silently into every cohort sum.
        self.assertIsNone(replay.synthetic_units(record(entry_price=None)))
        self.assertIsNone(replay.synthetic_units(record(side_outcome="unreconciled")))
        self.assertIsNone(replay.synthetic_units(record(side_outcome="final_score_missing")))
        self.assertIsNone(replay.synthetic_units(record(entry_price=0.0)))

    def test_the_slate_ask_is_used_only_when_no_paid_price_exists(self):
        paid = record(entry_price=0.4, slate_price=0.6)
        self.assertAlmostEqual(replay.synthetic_units(paid), 1.5)
        asked = record(entry_price=None, slate_price=0.6)
        self.assertAlmostEqual(replay.synthetic_units(asked), round(0.4 / 0.6, 6), places=6)


class AttributionTests(unittest.TestCase):
    def test_the_matrix_counts_every_candidate_exactly_once(self):
        records = [
            record(),
            record(disposition="skipped", side_outcome="loss"),
            record(disposition="review_rejected", side_outcome="unreconciled"),
            record(disposition="proposed_no_bet", side_outcome="win"),
        ]
        matrix = replay.attribution_matrix(records)
        self.assertEqual(sum(v for row in matrix.values() for v in row.values()), len(records))
        self.assertEqual(matrix["skipped"], {"loss": 1})

    def test_missed_winners_are_passed_winners_only_with_their_reasons(self):
        records = [
            record(),  # executed winner: not missed
            record(disposition="skipped", skip_reason="weather gate", game="C at D"),
            record(disposition="review_rejected", side_outcome="loss"),  # good pass
            record(disposition="skipped", side_outcome="unreconciled"),  # unknowable
        ]
        missed = replay.missed_winners(records)
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["game"], "C at D")
        self.assertEqual(missed[0]["skip_reason"], "weather gate")
        self.assertEqual(missed[0]["disposition"], "skipped")

    def test_executed_losses_never_include_passed_losses(self):
        records = [
            record(side_outcome="loss"),
            record(disposition="skipped", side_outcome="loss"),
        ]
        self.assertEqual(len(replay.executed_losses(records)), 1)

    def test_price_bands_split_at_the_stated_boundaries(self):
        for price, band in ((0.399, "under_0.40"), (0.40, "0.40_to_0.55"),
                            (0.549, "0.40_to_0.55"), (0.55, "0.55_and_up")):
            with self.subTest(price=price):
                self.assertEqual(replay.price_band(record(entry_price=price)), band)
        self.assertIsNone(replay.price_band(record(entry_price=None)))

    def test_cohort_summary_flags_insufficient_samples(self):
        records = [record(), record(side_outcome="loss")]
        summary = replay.cohort_summary(records, min_sample=20)
        self.assertEqual((summary["wins"], summary["losses"]), (1, 1))
        self.assertFalse(summary["sufficient_for_a_claim"])
        self.assertTrue(replay.cohort_summary(records, min_sample=2)["sufficient_for_a_claim"])

    def test_undecided_records_are_in_the_cohort_but_not_the_rate(self):
        records = [record(), record(side_outcome="unreconciled")]
        summary = replay.cohort_summary(records, min_sample=1)
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["decided"], 1)
        self.assertEqual(summary["win_rate"], 1.0)


class PushAccountingTests(unittest.TestCase):
    """The documented push policy: economic sample yes, rate denominator no."""

    def test_a_priced_push_is_replayable_and_an_unpriced_one_is_not(self):
        self.assertTrue(replay.eligible_for_replay(record(side_outcome="push")))
        self.assertFalse(replay.eligible_for_replay(record(side_outcome="push", entry_price=None)))
        self.assertFalse(replay.eligible_for_replay(record(side_outcome="unreconciled")))

    def test_cohort_pushes_enter_the_economic_sample_but_never_the_rate(self):
        records = [
            record(), record(),                                # 2 wins @0.5 → +1 each
            record(side_outcome="loss"),                       # -1
            record(side_outcome="push"), record(side_outcome="push"),
            record(side_outcome="push", entry_price=None),     # push with no price: counted, not replayable
            record(side_outcome="unreconciled"),
        ]
        summary = replay.cohort_summary(records, min_sample=3)
        self.assertEqual(summary["candidates"], 7)
        self.assertEqual(summary["decided"], 3)
        self.assertEqual(summary["pushes"], 3)
        self.assertEqual(summary["resolved"], 6)
        self.assertEqual(summary["replayable_with_price"], 5)
        self.assertEqual(summary["synthetic_units"], 1.0)
        self.assertEqual(summary["win_rate"], round(2 / 3, 6))
        self.assertEqual(summary["wilson_95"], [round(v, 6) for v in replay.wilson_ci(2, 3)])

    def test_sufficiency_stays_on_decided_records_however_many_pushes_land(self):
        # 3 decided + 3 pushes against min_sample=4: resolved (6) clears the
        # bar but the rate's denominator (3) does not — a push-heavy cohort
        # must not get to make a win-rate claim on fewer decided records.
        records = [record(), record(), record(side_outcome="loss")]
        records += [record(side_outcome="push")] * 3
        self.assertFalse(replay.cohort_summary(records, min_sample=4)["sufficient_for_a_claim"])
        self.assertTrue(replay.cohort_summary(records, min_sample=3)["sufficient_for_a_claim"])


class LeaveOnePeriodOutTests(unittest.TestCase):
    """The safeguard under test: selection never sees the held-out slice."""

    @staticmethod
    def month(date, specs):
        """specs: (price, outcome) executed records for one month."""
        return [
            record(date=date, entry_price=price, side_outcome=outcome)
            for price, outcome in specs
        ]

    def _strict_argmax(self, records):
        """The winning EXECUTED_RULES name on `records`, refusing ties.

        A fixture argmax resolved by the name tiebreak proves nothing about
        the economics, so any tie here is a broken fixture, not a result.
        """
        scored = {
            name: replay.rule_units(records, rule)[1]
            for name, rule in replay.EXECUTED_RULES.items()
        }
        top = max(scored.values())
        winners = [name for name, units in scored.items() if units == top]
        self.assertEqual(len(winners), 1, f"fixture tie: {scored}")
        return winners[0], top

    def test_the_held_out_month_never_influences_its_own_rule_choice(self):
        # The three selection sets a leaky grader could use must DISAGREE on
        # the winning rule, each with a strictly best score, so a leak in
        # either direction changes the ANSWER — not just a bookkeeping count.
        # Complement (m1+m2): cheap wins, 0.50s and 0.80s lose
        #   -> keep_under_0.50 (+12.0 vs 10.0 / 8.0 / 1.0).
        # All data (in-sample leak): month 3's sixteen 0.80 winners flip the
        #   total -> keep_all (+11.5 vs 9.5 / 8.5 / 5.5).
        # Month 3 alone (held-out leak): cheap picks lose there
        #   -> keep_0.40_to_0.55 (+4.5 vs 3.5 / -0.5 / -3.5).
        # The 0.50-priced records keep keep_under_0.50 and keep_under_0.55
        # extensionally different (10.0 vs 12.0 on the complement).
        m1 = self.month("2026-05-01", [(0.25, "win")] * 2 + [(0.40, "win")]
                        + [(0.50, "loss")] + [(0.80, "loss")])
        m2 = self.month("2026-06-01", [(0.25, "win")] + [(0.40, "win")]
                        + [(0.50, "loss")] + [(0.80, "loss")])
        m3 = self.month("2026-07-01", [(0.25, "loss")] * 5 + [(0.40, "win")]
                        + [(0.50, "win")] * 3 + [(0.80, "win")] * 16)

        # Prove the fixture discriminates BEFORE trusting the grader with it:
        # each candidate selection set names a different rule, strictly.
        honest, honest_units = self._strict_argmax(m1 + m2)
        leaked_in_sample, _ = self._strict_argmax(m1 + m2 + m3)
        leaked_held_out, _ = self._strict_argmax(m3)
        self.assertEqual(honest, "keep_under_0.50")
        self.assertEqual(leaked_in_sample, "keep_all")
        self.assertEqual(leaked_held_out, "keep_0.40_to_0.55")

        result = replay.leave_one_period_out(m1 + m2 + m3, replay.EXECUTED_RULES, min_selection=5)
        fold = {f["period"]: f for f in result["folds"]}["2026-07"]
        self.assertEqual(fold["chosen_rule"], "keep_under_0.50")
        self.assertEqual(fold["chosen_on_selection_units"], honest_units)
        self.assertEqual(fold["chosen_on_selection_units"], 12.0)
        self.assertEqual(fold["selection_ties"], [])
        self.assertEqual(fold["n_selection"], 9)
        self.assertEqual(fold["held_out_kept"], 6)
        self.assertEqual(fold["held_out_units"], -3.5)

    def test_an_insufficient_selection_set_grades_nothing(self):
        months = self.month("2026-05-01", [(0.45, "win")] * 3)
        months += self.month("2026-06-01", [(0.45, "win")] * 3)
        result = replay.leave_one_period_out(months, replay.EXECUTED_RULES, min_selection=5)
        for fold in result["folds"]:
            self.assertEqual(fold["status"], "insufficient_selection")
            self.assertNotIn("chosen_rule", fold)
        self.assertEqual(result["held_out"]["graded_folds"], 0)
        self.assertEqual(result["held_out"]["kept"], 0)

    def test_the_no_change_rule_wins_when_no_filter_beats_it(self):
        # Every pick wins: any exclusion only discards profit, so the honest
        # selection is keep_all — the machinery must be able to answer
        # "change nothing".
        months = self.month("2026-05-01", [(0.45, "win")] * 8 + [(0.60, "win")] * 8)
        months += self.month("2026-06-01", [(0.45, "win")] * 8 + [(0.60, "win")] * 8)
        result = replay.leave_one_period_out(months, replay.EXECUTED_RULES, min_selection=5)
        for fold in result["folds"]:
            self.assertEqual(fold["chosen_rule"], "keep_all")

    def test_ties_break_deterministically_by_rule_name(self):
        # All records under 0.50 and all winners: keep_all, keep_under_0.50,
        # keep_under_0.55, and keep_0.40_to_0.55 all score identically. The
        # winner must be stable run to run: first in sorted-name order.
        months = self.month("2026-05-01", [(0.45, "win")] * 8)
        months += self.month("2026-06-01", [(0.45, "win")] * 8)
        result = replay.leave_one_period_out(months, replay.EXECUTED_RULES, min_selection=5)
        expected = sorted(replay.EXECUTED_RULES)[0]
        for fold in result["folds"]:
            self.assertEqual(fold["chosen_rule"], expected)
            # …and the fold must SAY the win came from name order, not economics.
            self.assertEqual(fold["selection_ties"], sorted(replay.EXECUTED_RULES)[1:])

    def test_only_resolved_priced_records_enter_the_grader(self):
        # A priced push is resolved economic evidence and enters; undecided
        # and priceless records never do.
        months = self.month("2026-05-01", [(0.45, "win")] * 6 + [(0.45, "push")])
        months += [
            record(date="2026-05-02", side_outcome="unreconciled"),
            record(date="2026-05-02", entry_price=None),
        ]
        result = replay.leave_one_period_out(months, replay.EXECUTED_RULES, min_selection=1)
        self.assertEqual(result["eligible_records"], 7)

    def test_pushes_count_in_selection_sufficiency_and_held_out_at_zero_units(self):
        # 4 decided + 2 pushes per month against min_selection=5: if pushes
        # were excluded from the grader every fold would be
        # insufficient_selection (4 < 5), so a graded fold IS the push
        # accounting. Held-out kept counts the pushes; units come only from
        # the wins.
        m1 = self.month("2026-05-01", [(0.5, "win")] * 4 + [(0.5, "push")] * 2)
        m2 = self.month("2026-06-01", [(0.5, "win")] * 4 + [(0.5, "push")] * 2)
        result = replay.leave_one_period_out(m1 + m2, replay.EXECUTED_RULES, min_selection=5)
        for fold in result["folds"]:
            self.assertEqual(fold["status"], "graded")
            self.assertEqual(fold["n_selection"], 6)
            self.assertEqual(fold["held_out_kept"], 6)
            self.assertEqual(fold["held_out_units"], 4.0)

    def test_the_passed_cohort_no_change_rule_is_taking_nothing(self):
        # For declined proposals the status quo is zero units — add_none must
        # win whenever the passed picks were net losers.
        months = [
            record(date="2026-05-01", disposition="skipped", entry_price=0.5, side_outcome="loss")
            for _ in range(8)
        ] + [
            record(date="2026-06-01", disposition="skipped", entry_price=0.5, side_outcome="loss")
            for _ in range(8)
        ]
        result = replay.leave_one_period_out(months, replay.PASSED_RULES, min_selection=5)
        for fold in result["folds"]:
            self.assertEqual(fold["chosen_rule"], "add_none")
            self.assertEqual(fold["held_out_kept"], 0)


class ReportTests(unittest.TestCase):
    def _report(self):
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-06-10", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ])
        write_day(root, "2026-06-10", {
            "date": "2026-06-10",
            "candidates": [
                {"game": "Athletics at Detroit Tigers", "side": "Detroit Tigers",
                 "executed": True, "polymarket_ask": 0.55},
                {"game": "Athletics at Detroit Tigers", "side": "Athletics",
                 "skipped": True, "skip_reason": "weather", "polymarket_ask": 0.45},
            ],
        })
        # An intentional no-pick control day.
        write_day(root, "2026-06-11", {"date": "2026-06-11", "candidates": []})
        audit_report = audit.build_report(
            root / "execute", root / "results", 0.05, None, None, 0.05, 20
        )
        return replay.replay_report(audit_report, min_sample=20, min_selection=1)

    def test_controls_are_named_and_never_enter_any_cohort(self):
        report = self._report()
        self.assertEqual(report["controls"]["no_pick_control_days"], 1)
        self.assertEqual(report["controls"]["control_dates"], ["2026-06-11"])
        self.assertEqual(report["cohorts"]["executed"]["candidates"], 1)
        self.assertEqual(report["cohorts"]["passed"]["candidates"], 1)
        # The control day added no candidate anywhere: 1 executed + 1 passed
        # is the whole corpus.
        total = sum(v for row in report["attribution_matrix"].values() for v in row.values())
        self.assertEqual(total, 2)

    def test_reconciliation_comes_from_the_audit_not_a_second_derivation(self):
        report = self._report()
        self.assertIn("vig_historical_audit.build_report", report["foundation"]["source"])
        self.assertEqual(report["foundation"]["reconciled"], 2)
        # The skipped Athletics side lost officially — so it is NOT a missed
        # winner, and the executed Tigers side won.
        self.assertEqual(report["missed_winners"], [])
        self.assertEqual(report["cohorts"]["executed"]["wins"], 1)
        self.assertEqual(report["cohorts"]["passed"]["losses"], 1)

    def test_the_render_carries_the_caveats_and_sample_flags(self):
        report = self._report()
        rendered = replay.render(report)
        self.assertIn("SYNTHETIC", rendered)
        self.assertIn("INSUFFICIENT SAMPLE", rendered)
        self.assertIn("out of scope", rendered)
        self.assertIn("reference only, NEVER a verdict", rendered)
        json.dumps(report)  # the full report must stay JSON-serializable

    def test_profiles_carry_the_win_denominator_and_honest_month_key(self):
        report = self._report()
        profiles = report["profiles"]
        # The loss bands need a base rate to be read against — the wins
        # profile is that denominator, in the same bands.
        self.assertEqual(profiles["executed_wins"]["candidates"], 1)
        self.assertIn("executed WINS", replay.render(report))
        # The month histogram must be keyed by what it actually counts.
        self.assertEqual(profiles["executed"]["by_month"], {"2026-06": 1})
        self.assertNotIn("by_schema_source", profiles["executed"])

    def test_a_passed_winner_shows_up_with_its_recorded_reason(self):
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-06-10", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ])
        write_day(root, "2026-06-10", {
            "date": "2026-06-10",
            "candidates": [{
                "game": "Athletics at Detroit Tigers", "side": "Detroit Tigers",
                "skipped": True, "skip_reason": "liquidity", "polymarket_ask": 0.5,
            }],
        })
        audit_report = audit.build_report(
            root / "execute", root / "results", 0.05, None, None, 0.05, 20
        )
        report = replay.replay_report(audit_report, 20, 1)
        self.assertEqual(len(report["missed_winners"]), 1)
        self.assertEqual(report["missed_winners"][0]["skip_reason"], "liquidity")
        self.assertEqual(report["missed_winners"][0]["synthetic_units"], 1.0)


class ReadOnlyGuardTests(unittest.TestCase):
    """Same contract as the audit's guards, scoped to this module."""

    def test_sibling_imports_are_pinned_to_the_declared_set(self):
        self.assertEqual(
            import_closure.sibling_imports(MODULE),
            {"vig_historical_audit.py", "vig_calibration_report.py"},
        )

    def test_the_transitive_closure_stays_off_the_execution_path(self):
        # The direct pin alone would not stop vig_historical_audit growing an
        # edge this module inherits; the closure states the whole reachable set.
        self.assertEqual(
            import_closure.closure([MODULE]),
            {"vig_pick_replay.py", "vig_historical_audit.py",
             "vig_calibration_report.py", "mlb_final_scores.py",
             "mlb_runtime_policy.py", "http_util.py"},
        )

    def test_every_sibling_import_is_name_scoped(self):
        tree = ast.parse((SCRIPTS / MODULE).read_text(encoding="utf-8"))
        bound_whole = [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
            if (SCRIPTS / f"{alias.name.split('.', 1)[0]}.py").is_file()
        ]
        self.assertEqual(bound_whole, [])

    def test_the_replay_names_no_execution_entrypoint(self):
        text = (SCRIPTS / MODULE).read_text(encoding="utf-8").lower()
        for token in ("create_order", "post_order", "submit_order", "place_order",
                      "sign_order", "clob", "order_args", "private_key",
                      "polymarket_us_sdk_bet", "execution_guard"):
            with self.subTest(token=token):
                self.assertNotIn(token, text)

    def test_the_module_itself_performs_no_writes(self):
        # Unlike the audit, which owns one write path behind --fetch, this
        # module owns NONE: its --fetch delegates to the audit's helper. The
        # companion assertion below keeps this from passing vacuously by
        # requiring the delegation to actually be present.
        tree = ast.parse((SCRIPTS / MODULE).read_text(encoding="utf-8"))
        writes = [
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("write_text", "write_bytes", "mkdir", "unlink", "rmtree")
        ]
        self.assertEqual(writes, [])
        imported = {
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "vig_historical_audit"
            for alias in node.names
        }
        self.assertIn("fetch_missing_results", imported)

    def test_fetch_is_opt_in_and_delegated(self):
        calls = []
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-06-10", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ])
        write_day(root, "2026-06-10", {
            "date": "2026-06-10",
            "candidates": [{"game": "Athletics at Detroit Tigers", "side": "DET",
                            "executed": True, "polymarket_ask": 0.5}],
        })
        original = replay.fetch_missing_results
        replay.fetch_missing_results = lambda dates, results_dir: calls.append(list(dates)) or []
        try:
            base = ["--picks-dir", str(root), "--results-dir", str(root / "results"), "--json"]
            self.assertEqual(replay.main(base), 0)
            self.assertEqual(calls, [], "--fetch was not passed but the fetch helper ran")
            self.assertEqual(replay.main(base + ["--fetch"]), 0)
            self.assertEqual(calls, [["2026-06-10"]])
        finally:
            replay.fetch_missing_results = original

    def test_the_floor_constant_is_the_audits_not_a_second_copy(self):
        self.assertIs(replay.DEFAULT_MIN_CONSERVATIVE_EDGE, audit.DEFAULT_MIN_CONSERVATIVE_EDGE)


class CliValidationTests(unittest.TestCase):
    """Nonsense thresholds must fail closed before any report is built:
    a negative --min-sample marks every cohort sufficient and --min-selection
    0 grades folds with no selection data."""

    def assert_rejected(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                replay.main(argv)
        self.assertEqual(ctx.exception.code, 2, f"{argv} was not rejected")

    def test_out_of_range_thresholds_are_rejected(self):
        for argv in (
            ["--min-sample", "0"],
            ["--min-sample", "-1"],
            ["--min-selection", "0"],
            ["--min-selection", "-3"],
            ["--edge-floor", "0"],
            ["--edge-floor", "1"],
            ["--edge-floor", "-0.05"],
            ["--since", "06-10-2026"],
            ["--until", "2026-6-1"],
        ):
            with self.subTest(argv=argv):
                self.assert_rejected(argv)

    def test_the_minimal_legal_thresholds_still_run(self):
        root = Path(tempfile.mkdtemp())
        write_results(root, "2026-06-10", [
            {"away": "Athletics", "home": "Detroit Tigers", "away_score": 1, "home_score": 6},
        ])
        write_day(root, "2026-06-10", {
            "date": "2026-06-10",
            "candidates": [{"game": "Athletics at Detroit Tigers", "side": "DET",
                            "executed": True, "polymarket_ask": 0.5}],
        })
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(replay.main([
                "--picks-dir", str(root), "--results-dir", str(root / "results"),
                "--min-sample", "1", "--min-selection", "1", "--edge-floor", "0.05",
                "--json",
            ]), 0)


if __name__ == "__main__":
    unittest.main()
