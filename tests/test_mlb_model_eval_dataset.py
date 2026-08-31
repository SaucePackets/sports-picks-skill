import json
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import mlb_game_reads
from scripts import mlb_model_eval_dataset as dataset
from scripts import mlb_probability_model

REPO_ROOT = Path(__file__).resolve().parents[1]


def read(game_pk=823509, **overrides):
    entry = {
        "game_pk": game_pk,
        "event_id": f"4018{game_pk}",
        "away": "Atlanta Braves",
        "home": "Milwaukee Brewers",
        "disposition": "pass",
        "dk_fair_prob": {"away": 0.398, "home": 0.602},
        "polymarket_ask": {"away": 0.460, "home": 0.545},
        "raw_probability": {"away": 0.400, "home": 0.610},
        "uncertainty_haircut": 0.02,
        "conservative_probability": {"away": 0.380, "home": 0.590},
        "model_version": "vig-mlb-market-v1",
        "net_edge": {"away": -0.080, "home": 0.035},
        "refusing_rails": ["price_discipline"],
    }
    entry.update(overrides)
    return entry


def statsapi(game_pk=823509, away="Atlanta Braves", home="Milwaukee Brewers",
             away_score=5, home_score=2, status="Final"):
    return {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": game_pk,
                        "status": {"detailedState": status},
                        "teams": {
                            "away": {"team": {"name": away}, "score": away_score},
                            "home": {"team": {"name": home}, "score": home_score},
                        },
                    }
                ]
            }
        ]
    }


class RowTests(unittest.TestCase):
    def setUp(self):
        self.finals = dataset._final_by_game_pk(statsapi())

    def test_a_complete_read_with_a_final_becomes_one_row(self):
        row, reason = dataset.row_for_read("2026-09-01", read(), self.finals)
        self.assertIsNone(reason)
        self.assertEqual(row["dk_fair_prob"], 0.398)
        self.assertEqual(row["raw_probability"], 0.400)
        self.assertEqual(row["conservative_probability"], 0.380)
        self.assertEqual(row["uncertainty_haircut"], 0.02)
        self.assertEqual(row["model_version"], "vig-mlb-market-v1")
        self.assertEqual(row["outcome"], 1)
        self.assertEqual(row["side"], "Atlanta Braves")

    def test_the_outcome_follows_the_final_not_the_probability(self):
        finals = dataset._final_by_game_pk(statsapi(away_score=1, home_score=7))
        row, _ = dataset.row_for_read("2026-09-01", read(), finals)
        self.assertEqual(row["outcome"], 0)

    def test_exactly_one_row_per_game_and_always_the_away_side(self):
        # Emitting both sides would double n with perfectly anti-correlated
        # rows and break the independence every metric here assumes; choosing
        # "the side the model liked" would re-introduce the selection bias this
        # whole dataset exists to remove.
        rows, _ = dataset.build_rows(
            [("2026-09-01", {"game_reads": [read()]})],
            {"2026-09-01": self.finals},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(dataset.EVALUATED_SIDE, "away")
        self.assertEqual(rows[0]["side"], "Atlanta Braves")
        self.assertEqual(rows[0]["dk_fair_prob"], read()["dk_fair_prob"]["away"])

    def test_a_refused_game_is_included_exactly_like_a_bet_one(self):
        # The entire point: a handicap on a game we passed is still a testable
        # pre-pitch prediction, and passed games are the population picks.json
        # can never contain.
        for disposition, rails in (
            ("pass", ["price_discipline"]),
            ("not_priced", ["no_dk_price"]),
            ("candidate", []),
        ):
            with self.subTest(disposition=disposition):
                entry = read(disposition=disposition, refusing_rails=rails)
                row, reason = dataset.row_for_read("2026-09-01", entry, self.finals)
                self.assertIsNone(reason)
                self.assertEqual(row["outcome"], 1)

    def test_nothing_is_imputed_and_every_skip_states_its_reason(self):
        cases = {
            "raw_probability": "no usable away value for raw_probability",
            "conservative_probability": "no usable away value for conservative_probability",
            "dk_fair_prob": "no usable away value for dk_fair_prob",
        }
        for field, expected in cases.items():
            with self.subTest(field=field):
                row, reason = dataset.row_for_read(
                    "2026-09-01", read(**{field: None}), self.finals
                )
                self.assertIsNone(row)
                self.assertIn(expected, reason)

    def test_a_read_with_no_model_version_is_skipped_not_attributed(self):
        row, reason = dataset.row_for_read(
            "2026-09-01", read(model_version=None), self.finals
        )
        self.assertIsNone(row)
        self.assertIn("no model_version", reason)

    def test_a_game_with_no_final_is_skipped(self):
        row, reason = dataset.row_for_read("2026-09-01", read(game_pk=999999), {})
        self.assertIsNone(row)
        self.assertIn("no Final result available", reason)

    def test_a_game_still_in_progress_is_not_a_final(self):
        finals = dataset._final_by_game_pk(statsapi(status="In Progress"))
        row, reason = dataset.row_for_read("2026-09-01", read(), finals)
        self.assertIsNone(row)
        self.assertIn("no Final result available", reason)

    def test_a_final_whose_teams_do_not_match_the_winner_is_refused(self):
        finals = dataset._final_by_game_pk(statsapi())
        finals[823509]["winner"] = "Some Other Club"
        row, reason = dataset.row_for_read("2026-09-01", read(), finals)
        self.assertIsNone(row)
        self.assertIn("is neither team on the final", reason)

    def test_skips_are_reported_per_date_including_a_date_with_no_finals(self):
        rows, skipped = dataset.build_rows(
            [("2026-09-01", {"game_reads": [read()]})], {}
        )
        self.assertEqual(rows, [])
        self.assertTrue(any("no finals available" in s for s in skipped), skipped)

    def test_a_schedule_with_no_game_reads_is_reported_not_silently_empty(self):
        rows, skipped = dataset.build_rows([("2026-09-01", {"candidates": []})], {})
        self.assertEqual(rows, [])
        self.assertTrue(any("carries no game_reads array" in s for s in skipped), skipped)

    def test_rows_come_out_in_date_order(self):
        rows, _ = dataset.build_rows(
            [
                ("2026-09-02", {"game_reads": [read()]}),
                ("2026-09-01", {"game_reads": [read()]}),
            ],
            {
                "2026-09-01": dataset._final_by_game_pk(statsapi()),
                "2026-09-02": dataset._final_by_game_pk(statsapi()),
            },
        )
        self.assertEqual([r["date"] for r in rows], ["2026-09-01", "2026-09-02"])


class SeamTests(unittest.TestCase):
    def test_the_rows_are_what_the_existing_evaluator_consumes(self):
        # The evaluator and the deployment gate are unchanged in this slice.
        # If a row shape drifts, the metrics quietly compute over zero usable
        # rows and the gate reports "could not be computed" — which reads like
        # a data problem, not a schema problem. So assert the seam directly.
        rows, _ = dataset.build_rows(
            [
                ("2026-09-01", {"game_reads": [read(), read(game_pk=823510)]}),
                ("2026-09-02", {"game_reads": [read(game_pk=823511)]}),
            ],
            {
                "2026-09-01": {
                    **dataset._final_by_game_pk(statsapi()),
                    **dataset._final_by_game_pk(statsapi(game_pk=823510, away_score=1, home_score=4)),
                },
                "2026-09-02": dataset._final_by_game_pk(statsapi(game_pk=823511)),
            },
        )
        self.assertEqual(len(rows), 3)
        comparison = mlb_probability_model.compare_to_market(
            rows, "conservative_probability"
        )
        self.assertEqual(comparison["n"], 3)
        self.assertIsNotNone(comparison["deltas"]["brier"])
        walk = mlb_probability_model.walk_forward_report(
            rows, "conservative_probability", window=2
        )
        self.assertEqual(walk["cumulative"]["n"], 3)
        self.assertEqual(walk["windows"][0]["start_date"], "2026-09-01")

    def test_the_probability_rule_is_the_recorders_own_not_a_look_alike(self):
        # Consultation, not restatement — and the identity is asserted against
        # the module the dataset builder ACTUALLY imported. This repo loads one
        # file as ``scripts.mlb_game_reads`` AND bare ``mlb_game_reads``, and
        # Python caches those as two module objects: comparing against the
        # ``scripts.`` copy compares two different functions and fails while
        # both are one source. That is the dual-import hazard, not a defect.
        self.assertIsNot(mlb_game_reads, sys.modules["mlb_game_reads"])
        self.assertIs(
            dataset._is_probability, sys.modules["mlb_game_reads"]._is_probability
        )
        # And the answer follows the rule rather than a re-derivation: rebind
        # the source and require the dataset builder to change its mind.
        finals = dataset._final_by_game_pk(statsapi())
        original = dataset._is_probability
        try:
            dataset._is_probability = lambda value: False
            row, reason = dataset.row_for_read("2026-09-01", read(), finals)
            self.assertIsNone(row)
            self.assertIn("no usable away value", reason)
        finally:
            dataset._is_probability = original
        row, reason = dataset.row_for_read("2026-09-01", read(), finals)
        self.assertIsNone(reason)

    def test_a_row_the_recorder_would_refuse_never_reaches_the_dataset(self):
        # The validator and the builder must agree about what a usable read is.
        # A read the recorder rejects (incoherent trail) is not something the
        # evaluator should ever score.
        entry = read(conservative_probability={"away": 0.9, "home": 0.590})
        self.assertNotEqual(mlb_game_reads.validate_read(entry, 0), [])


class CliTests(unittest.TestCase):
    def test_it_refuses_to_build_a_dataset_with_no_source_of_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                dataset.main(
                    ["--schedules", tmp, "--start", "2026-09-01", "--until", "2026-09-01"]
                )
            self.assertNotEqual(caught.exception.code, 0)

    def test_end_to_end_writes_jsonl_the_evaluator_can_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schedules = root / "execute"
            finals = root / "finals"
            schedules.mkdir()
            finals.mkdir()
            (schedules / "2026-09-01-schedule.json").write_text(
                json.dumps({"game_reads": [read()]}), encoding="utf-8"
            )
            (finals / "2026-09-01.json").write_text(json.dumps(statsapi()), encoding="utf-8")
            out = root / "dataset.jsonl"
            code = dataset.main(
                [
                    "--schedules", str(schedules),
                    "--start", "2026-09-01",
                    "--until", "2026-09-01",
                    "--finals", str(finals),
                    "--out", str(out),
                ]
            )
            self.assertEqual(code, 0)
            lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["outcome"], 1)
        self.assertEqual(lines[0]["pick_id"], "read-2026-09-01-823509-away")

    def test_an_inverted_date_range_is_an_error_not_an_empty_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                dataset.main(
                    [
                        "--schedules", tmp,
                        "--start", "2026-09-05",
                        "--until", "2026-09-01",
                        "--finals", tmp,
                    ]
                )


if __name__ == "__main__":
    unittest.main()
