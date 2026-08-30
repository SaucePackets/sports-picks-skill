"""Tests for the read-only historical pick replay/attribution report.

The properties that matter most here are the honesty ones: controls are never
bets, synthetic economics say None where there is no evidence, and the
leave-one-period-out grader can be PROVEN never to tune and grade on the same
slice — the last of these with a fixture where in-sample selection and
out-of-sample selection genuinely disagree, so a leak changes the answer.
"""

from __future__ import annotations

import ast
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


class LeaveOnePeriodOutTests(unittest.TestCase):
    """The safeguard under test: selection never sees the held-out slice."""

    @staticmethod
    def month(date, specs):
        """specs: (price, outcome) executed records for one month."""
        return [
            record(date=date, entry_price=price, side_outcome=outcome)
            for price, outcome in specs
        ]

    def test_the_held_out_month_never_influences_its_own_rule_choice(self):
        # In months 1-2, cheap (0.35) picks win and expensive (0.60) picks
        # lose, so keep_under_0.50 dominates there. In month 3 the pattern
        # INVERTS. The two ways this grader could leak both change month 3's
        # answer: tuned in-sample on ALL data, keep_all wins (+8.3u vs +4.3u);
        # tuned on month 3 itself, keep_0.40_to_0.55 wins (0u vs -2u). Only
        # the honest complement-selection chooses keep_under_0.50 for month 3
        # — and then it must eat the -6u loss rather than dodge it.
        m1 = self.month("2026-05-01", [(0.35, "win")] * 6 + [(0.60, "loss")] * 6)
        m2 = self.month("2026-06-01", [(0.35, "win")] * 6 + [(0.60, "loss")] * 6)
        m3 = self.month("2026-07-01", [(0.35, "loss")] * 6 + [(0.60, "win")] * 6)
        result = replay.leave_one_period_out(m1 + m2 + m3, replay.EXECUTED_RULES, min_selection=5)
        folds = {f["period"]: f for f in result["folds"]}
        self.assertEqual(folds["2026-07"]["chosen_rule"], "keep_under_0.50")
        self.assertEqual(folds["2026-07"]["n_selection"], 24)
        self.assertEqual(folds["2026-07"]["held_out_kept"], 6)
        self.assertEqual(folds["2026-07"]["held_out_units"], -6.0)

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

    def test_only_decided_priced_records_enter_the_grader(self):
        months = self.month("2026-05-01", [(0.45, "win")] * 6)
        months += [
            record(date="2026-05-02", side_outcome="unreconciled"),
            record(date="2026-05-02", entry_price=None),
        ]
        result = replay.leave_one_period_out(months, replay.EXECUTED_RULES, min_selection=1)
        self.assertEqual(result["eligible_records"], 6)

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


if __name__ == "__main__":
    unittest.main()
