"""Tests for the read-only executed-pick postgame pillar analysis.

The properties that matter most are the honesty ones: the analyzed cohort is
exactly the replay's executed decided set with every exclusion named; grading
is byte-deterministic and blind to input order; denominators are explicit and
zero-filled; no game is ever silently dropped; and no outcome field can reach
the grader — flipping every outcome label moves games between cohorts without
changing a single pillar grade.
"""

from __future__ import annotations

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

import vig_loss_evidence_report as loss_report  # noqa: E402
from mlb_postgame_evidence import PILLARS  # noqa: E402
from test_vig_historical_audit import statsapi_payload, write_day  # noqa: E402


def record(**overrides):
    """A minimal audited record with the keys this layer reads."""
    base = {
        "date": "2026-06-10",
        "game": "Alpha at Beta",
        "side_raw": "Alpha",
        "resolved_side": "Alpha",
        "disposition": "executed",
        "skip_reason": None,
        "side_outcome": "win",
        "entry_price": 0.5,
        "slate_price": None,
        "recorded_rationale": {"thesis": "recorded thesis"},
        "official": {"gamePk": 700001, "away": "Alpha", "home": "Beta"},
        "vig_approved": None,
        "unreconciled_reason": None,
    }
    base.update(overrides)
    return base


def postgame_evidence(
    game_pk=700001,
    away="Alpha",
    home="Beta",
    away_runs=5,
    home_runs=2,
    away_starter_outs=18,
    away_reliever_runs=(0,),
    status="complete",
):
    """A collector-shaped evidence object built directly, not via a feed."""
    if status != "complete":
        return {
            "evidence_status": "insufficient",
            "insufficient_reasons": ["game status is 'Postponed', not Final"],
            "game_pk": game_pk,
            "away": away,
            "home": home,
        }

    def side(runs, starter_outs, reliever_runs):
        return (
            {
                "starter": {
                    "name": "Starter",
                    "innings_pitched": f"{starter_outs // 3}.{starter_outs % 3}",
                    "outs": starter_outs,
                    "earned_runs": 2,
                    "runs": 2,
                },
                "relievers": [
                    {
                        "name": f"Reliever {i}",
                        "innings_pitched": "1.0",
                        "outs": 3,
                        "runs": r,
                        "earned_runs": r,
                    }
                    for i, r in enumerate(reliever_runs)
                ],
                "actual_role": "starter" if starter_outs >= 12 else "short_start",
            },
            {"runs": runs, "hits": runs + 3, "walks": 2, "strikeouts": 8, "home_runs": 1},
        )

    away_pitching, away_offense = side(away_runs, away_starter_outs, away_reliever_runs)
    home_pitching, home_offense = side(home_runs, 15, (1,))
    return {
        "evidence_status": "complete",
        "insufficient_reasons": [],
        "game_pk": game_pk,
        "date": "2026-06-10",
        "status": "Final",
        "away": away,
        "home": home,
        "away_score": away_runs,
        "home_score": home_runs,
        "winner": away if away_runs > home_runs else home,
        "pitching": {"away": away_pitching, "home": home_pitching},
        "offense": {"away": away_offense, "home": home_offense},
        "scoring_plays_available": True,
        "scoring_plays": [],
    }


def write_evidence(evidence_dir: Path, evidence: dict) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{evidence['game_pk']}.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return path


class CorpusSelectionTests(unittest.TestCase):
    def test_only_executed_decided_records_are_analyzed(self):
        records = [
            record(),
            record(side_outcome="loss", official={"gamePk": 700002, "away": "Alpha", "home": "Beta"}),
            record(side_outcome="push"),
            record(side_outcome="unreconciled", unreconciled_reason="no official row"),
            record(disposition="skipped", side_outcome="loss"),
            record(disposition="review_rejected", side_outcome="win"),
        ]
        cohort = loss_report.executed_decided(records)
        self.assertEqual(len(cohort), 2)
        self.assertEqual({r["side_outcome"] for r in cohort}, {"win", "loss"})

    def test_excluded_executions_are_named_never_silently_dropped(self):
        records = [
            record(),
            record(side_outcome="push", game="Pushed at Game"),
            record(side_outcome="unreconciled", game="Lost at Sea",
                   unreconciled_reason="no official row"),
        ]
        selection = loss_report.corpus_selection(records)
        self.assertEqual(selection["executed"], 3)
        self.assertEqual(selection["executed_decided"], 1)
        excluded_games = {e["game"] for e in selection["executed_excluded"]}
        self.assertEqual(excluded_games, {"Pushed at Game", "Lost at Sea"})

    def test_loss_classification_counts_are_zero_filled_over_the_closed_set(self):
        selection = loss_report.corpus_selection(
            [record(side_outcome="loss")]
        )
        counts = selection["loss_classification_counts"]
        # Every category present, including the ones that never occurred — a
        # 0 says the axis was checked; an absent key cannot.
        self.assertEqual(set(counts), set(loss_report.MISS_CLASSIFICATIONS))
        self.assertEqual(counts["evidence_process_miss"], 1)
        self.assertEqual(sum(counts.values()), 1)

    def test_cohort_order_is_deterministic_regardless_of_input_order(self):
        a = record(date="2026-06-11", official={"gamePk": 700009, "away": "Alpha", "home": "Beta"})
        b = record(date="2026-06-10")
        self.assertEqual(
            loss_report.executed_decided([a, b]),
            loss_report.executed_decided([b, a]),
        )


class BetTimeEvidenceTests(unittest.TestCase):
    def test_only_allowlisted_pregame_fields_reach_the_grader(self):
        rec = record(recorded_rationale={
            "thesis": "irrelevant to the grader",
            "named_risks": [{"name": "closer unavailable"}],
            "starter_role": "starter",
            "expected_ip": 5.0,
            # Adversarial junk that must never pass through:
            "side_outcome": "loss",
            "official": {"winner": "Beta"},
            "recorded_result": "loss",
        })
        evidence = loss_report.bet_time_evidence(rec)
        self.assertEqual(
            set(evidence), {"named_risks", "starter_role", "expected_ip"}
        )

    def test_missing_rationale_degrades_to_empty_not_a_crash(self):
        self.assertEqual(loss_report.bet_time_evidence(record(recorded_rationale=None)), {})


class EvidenceCacheTests(unittest.TestCase):
    def _load(self, payload_text, game_pk=700001):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{game_pk}.json"
            path.write_text(payload_text, encoding="utf-8")
            return loss_report.load_cached_evidence(Path(tmp), game_pk)

    def test_missing_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload, status = loss_report.load_cached_evidence(Path(tmp), 700001)
        self.assertIsNone(payload)
        self.assertEqual(status, "missing")

    def test_corrupt_cache_never_launders_into_a_graded_game(self):
        for text in ("not json", '"a string"', "[]",
                     json.dumps({"evidence_status": "weird", "game_pk": 700001})):
            with self.subTest(text=text[:20]):
                payload, status = self._load(text)
                self.assertIsNone(payload)
                self.assertEqual(status, "invalid")

    def test_a_cache_file_naming_a_different_game_pk_is_invalid(self):
        payload, status = self._load(
            json.dumps(postgame_evidence(game_pk=999999)), game_pk=700001
        )
        self.assertIsNone(payload)
        self.assertEqual(status, "invalid")

    def test_valid_complete_and_insufficient_payloads_load(self):
        for evidence in (postgame_evidence(), postgame_evidence(status="insufficient")):
            with self.subTest(status=evidence["evidence_status"]):
                payload, status = self._load(json.dumps(evidence))
                self.assertEqual(status, evidence["evidence_status"])
                self.assertEqual(payload["game_pk"], 700001)


class GradeRecordTests(unittest.TestCase):
    def grade(self, rec, evidence=None):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp)
            if evidence is not None:
                write_evidence(evidence_dir, evidence)
            return loss_report.grade_record(rec, evidence_dir)

    def test_complete_evidence_grades_all_pillars(self):
        row = self.grade(record(), postgame_evidence())
        self.assertEqual(row["evidence_file_status"], "complete")
        self.assertEqual(set(row["pillars"]), set(PILLARS))
        # Alpha (away) scored 5 with a clean bullpen: postgame-decidable
        # pillars hold; bet-time-evidence pillars are unknown on a bare card.
        self.assertEqual(row["pillars"]["offense_conversion"]["grade"], "held")
        self.assertEqual(row["pillars"]["bullpen_availability"]["grade"], "held")
        self.assertEqual(row["pillars"]["starter_quality"]["grade"], "unknown")
        self.assertEqual(row["descriptive"]["team_runs"], 5)
        self.assertEqual(row["descriptive"]["bullpen_runs_allowed"], 0)
        self.assertEqual(row["descriptive"]["actual_role"], "starter")

    def test_recorded_bet_time_evidence_activates_the_expected_half(self):
        rec = record(recorded_rationale={
            "starter_role": "starter", "expected_ip": 5.0,
            "named_risks": [],
        })
        row = self.grade(rec, postgame_evidence(away_starter_outs=18))
        self.assertEqual(row["pillars"]["starter_role"]["grade"], "held")
        self.assertEqual(row["pillars"]["starter_quality"]["grade"], "held")
        self.assertEqual(row["pillars"]["named_risk"]["grade"], "held")

    def test_descriptive_side_comes_from_this_record_not_a_baked_our_side(self):
        # A cache file written by a CLI `collect --team <other side>` run
        # carries that run's team/our_side; the row must still describe THIS
        # record's backed side.
        evidence = postgame_evidence()
        evidence["team"] = "Beta"
        evidence["our_side"] = "home"
        row = self.grade(record(), evidence)
        self.assertEqual(row["descriptive"]["team_runs"], 5)  # Alpha, the away side

    def test_missing_evidence_keeps_the_row_and_reports_missing(self):
        row = self.grade(record())
        self.assertEqual(row["evidence_file_status"], "missing")
        self.assertIsNone(row["pillars"])
        self.assertEqual(row["game"], "Alpha at Beta")

    def test_insufficient_evidence_grades_every_pillar_unknown(self):
        row = self.grade(record(), postgame_evidence(status="insufficient"))
        self.assertEqual(row["evidence_file_status"], "insufficient")
        for pillar in PILLARS:
            self.assertEqual(row["pillars"][pillar]["grade"], "unknown")
        self.assertIsNone(row["descriptive"])

    def test_team_not_matching_either_side_is_invalid_not_a_crash(self):
        row = self.grade(record(resolved_side="Gamma"), postgame_evidence())
        self.assertEqual(row["evidence_file_status"], "invalid")
        self.assertIn("Gamma", row["note"])

    def test_a_record_without_a_game_pk_reports_missing_with_its_reason(self):
        row = self.grade(record(official=None))
        self.assertEqual(row["evidence_file_status"], "missing")
        self.assertIn("gamePk", row["note"])


class AggregationTests(unittest.TestCase):
    def build_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp)
            # Loss whose offense failed (1 run) and bullpen failed (4 runs).
            write_evidence(evidence_dir, postgame_evidence(
                game_pk=700001, away_runs=1, home_runs=6, away_reliever_runs=(4,)))
            # Win whose pillars held.
            write_evidence(evidence_dir, postgame_evidence(game_pk=700002))
            records = [
                record(side_outcome="loss",
                       official={"gamePk": 700001, "away": "Alpha", "home": "Beta"}),
                record(side_outcome="win", game="Alpha at Delta",
                       official={"gamePk": 700002, "away": "Alpha", "home": "Beta"}),
                record(side_outcome="loss", game="Alpha at Echo",
                       official={"gamePk": 700003, "away": "Alpha", "home": "Beta"}),
            ]
            return [loss_report.grade_record(r, evidence_dir) for r in records]

    def test_denominators_and_zero_filled_counts(self):
        aggregates = loss_report.aggregate_pillars(self.build_rows())
        loss = aggregates["loss"]
        self.assertEqual(loss["games"], 2)
        self.assertEqual(loss["graded"], 1)  # 700003 has no evidence file
        self.assertEqual(loss["ungraded"], 1)
        offense = loss["pillars"]["offense_conversion"]
        self.assertEqual(offense["counts"], {"held": 0, "failed": 1, "mixed": 0, "unknown": 0})
        self.assertEqual(offense["decided"], 1)
        self.assertEqual(offense["failed_rate"], 1.0)
        bullpen = loss["pillars"]["bullpen_availability"]
        self.assertEqual(bullpen["counts"]["failed"], 1)
        # Undecidable pillars have a null rate, never a fake 0.
        self.assertIsNone(loss["pillars"]["starter_quality"]["failed_rate"])
        win = aggregates["win"]
        self.assertEqual(win["pillars"]["offense_conversion"]["counts"]["held"], 1)
        self.assertEqual(
            set(win["backed_side_actual_role"]), set(loss_report.ACTUAL_ROLES)
        )

    def test_coverage_names_the_ungradeable_games(self):
        cov = loss_report.coverage(self.build_rows())
        self.assertEqual(cov["counts"]["missing"], 1)
        self.assertEqual(cov["missing"][0]["game"], "Alpha at Echo")
        self.assertEqual(cov["counts"]["complete"], 2)
        self.assertEqual(cov["bet_time_evidence"]["records_with_any_field"], 0)
        self.assertEqual(cov["bet_time_evidence"]["records_total"], 3)


class NoHindsightLeakageTests(unittest.TestCase):
    def test_flipping_every_outcome_label_changes_no_pillar_grade(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_dir = Path(tmp)
            write_evidence(evidence_dir, postgame_evidence(
                game_pk=700001, away_runs=1, home_runs=6, away_reliever_runs=(4,)))
            write_evidence(evidence_dir, postgame_evidence(game_pk=700002))
            originals = [
                record(side_outcome="loss",
                       official={"gamePk": 700001, "away": "Alpha", "home": "Beta"}),
                record(side_outcome="win",
                       official={"gamePk": 700002, "away": "Alpha", "home": "Beta"}),
            ]
            flipped = [
                dict(r, side_outcome=("win" if r["side_outcome"] == "loss" else "loss"))
                for r in originals
            ]
            rows = [loss_report.grade_record(r, evidence_dir) for r in originals]
            flipped_rows = [loss_report.grade_record(r, evidence_dir) for r in flipped]
        for before, after in zip(rows, flipped_rows):
            self.assertNotEqual(before["side_outcome"], after["side_outcome"])
            self.assertEqual(before["pillars"], after["pillars"])
            self.assertEqual(before["descriptive"], after["descriptive"])

    def test_the_grader_input_contains_no_outcome_keys(self):
        # The allowlist is the leakage mechanism's whole defense: assert it
        # cannot name an outcome field even if one is added carelessly later.
        outcome_fields = {
            "side_outcome", "official", "recorded_result", "pnl_usd",
            "recorded_final_score", "winner",
        }
        self.assertFalse(
            outcome_fields & set(loss_report.PREGAME_EVIDENCE_FIELDS)
        )


class EndToEndTests(unittest.TestCase):
    """The CLI over a real on-disk corpus, offline."""

    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = loss_report.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _corpus(self, root):
        root = Path(root)
        write_day(root, "2026-06-10", {
            "date": "2026-06-10", "sport": "mlb", "market_type": "moneyline",
            "candidates": [
                {"game": "Alpha at Beta", "side": "Alpha", "executed": True,
                 "polymarket_ask": 0.5, "thesis": "recorded thesis"},
                {"game": "Gamma at Delta", "side": "Gamma", "executed": True,
                 "polymarket_ask": 0.5, "thesis": "recorded thesis"},
            ],
        })
        results = root / "audit-results"
        results.mkdir()
        (results / "2026-06-10.json").write_text(json.dumps(statsapi_payload([
            {"gamePk": 700001, "away": "Alpha", "home": "Beta",
             "away_score": 5, "home_score": 2},
            {"gamePk": 700002, "away": "Gamma", "home": "Delta",
             "away_score": 1, "home_score": 6},
        ])), encoding="utf-8")
        evidence_dir = root / "postgame-evidence"
        write_evidence(evidence_dir, postgame_evidence(
            game_pk=700001, away="Alpha", home="Beta", away_runs=5, home_runs=2))
        write_evidence(evidence_dir, postgame_evidence(
            game_pk=700002, away="Gamma", home="Delta", away_runs=1, home_runs=6,
            away_reliever_runs=(4,)))
        return root

    def test_full_coverage_run_exits_zero_and_reports_both_cohorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._corpus(tmp)
            code, out, _ = self.run_cli(["--picks-dir", str(root), "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["corpus_selection"]["wins"], 1)
        self.assertEqual(report["corpus_selection"]["losses"], 1)
        self.assertEqual(
            report["aggregates"]["loss"]["pillars"]["offense_conversion"]["counts"]["failed"], 1
        )
        self.assertEqual(report["coverage"]["counts"]["missing"], 0)

    def test_identical_reports_regardless_of_run_and_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._corpus(tmp)
            _, first, _ = self.run_cli(["--picks-dir", str(root), "--json"])
            _, second, _ = self.run_cli(["--picks-dir", str(root), "--json"])
        self.assertEqual(first, second)

    def test_a_coverage_hole_fails_loud_with_exit_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._corpus(tmp)
            (root / "postgame-evidence" / "700002.json").unlink()
            code, out, _ = self.run_cli(["--picks-dir", str(root), "--json"])
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertEqual(report["coverage"]["counts"]["missing"], 1)
        # The game is still in the per-game rows — reported, not dropped.
        self.assertEqual(len(report["games"]), 2)

    def test_render_mode_names_the_denominators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._corpus(tmp)
            code, out, _ = self.run_cli(["--picks-dir", str(root)])
        self.assertEqual(code, 0)
        self.assertIn("failed/decided", out)
        self.assertIn("evidence_process_miss=1", out)

    def test_bad_flags_fail_before_reading_anything(self):
        for argv in (["--picks-dir", "/nonexistent", "--since", "June 1"],
                     ["--picks-dir", "/nonexistent", "--edge-floor", "0"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_cli(argv)
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
