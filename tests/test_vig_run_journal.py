"""Unit tests for the gate run journal itself.

The gate-integration cases (an explicit pass, a positive candidate, deferred
inputs, a rolled-back transition, an artifact-write failure) live in
tests/test_vig_review_gate_common.py beside the run_gate harness they drive.
These cover the storage layer's own guarantees.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig_run_journal.py"
spec = importlib.util.spec_from_file_location("vig_run_journal_under_test", SCRIPT_PATH)
assert spec is not None
journal = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["vig_run_journal_under_test"] = journal
spec.loader.exec_module(journal)


class RunJournalTests(unittest.TestCase):
    def record(self, **overrides):
        base = dict(
            sport="MLB",
            day="2026-08-19",
            outcome=journal.OUTCOME_NO_SCHEDULE,
            stage="schedule_missing",
            recorded_at="2026-08-19T15:30:00Z",
        )
        base.update(overrides)
        return journal.build_record(**base)

    def test_build_record_is_pure_when_given_an_instant(self):
        # Pinned so a caller (in practice a test) can assert on the record
        # without its own clock call, the schedule_day_now lesson from PR #57.
        first = self.record()
        second = self.record()
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], journal.JOURNAL_SCHEMA)
        self.assertEqual(first["recorded_at"], "2026-08-19T15:30:00Z")

    def test_unknown_outcome_is_rejected(self):
        # An outcome vocabulary nothing enforces is a free-text field with
        # extra steps, and a report that groups by it would silently drop rows.
        with self.assertRaises(ValueError):
            self.record(outcome="probably_fine")

    def test_every_gate_outcome_constant_is_accepted(self):
        for outcome in journal.OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertEqual(self.record(outcome=outcome)["outcome"], outcome)

    def test_unknown_deferral_kind_is_rejected(self):
        # Same argument as the outcome vocabulary: a kind that renders the
        # entry differently but is never validated is free text, and the
        # renderer silently falls through to "deferred" on a typo.
        with self.assertRaises(ValueError):
            journal.deferral("e1", journal.SOURCE_PRICE_FEED, "why", kind="probably_fine")

    def test_every_deferral_kind_constant_is_accepted_and_renders(self):
        for kind in journal.KINDS:
            with self.subTest(kind=kind):
                item = journal.deferral("e1", journal.SOURCE_PRICE_FEED, "why", kind=kind)
                self.assertEqual(item["kind"], kind)
        # An outage is the default, because it is the recoverable case: an
        # unmarked item must not read as permanently broken.
        self.assertEqual(
            journal.deferral("e1", journal.SOURCE_PRICE_FEED, "why")["kind"],
            journal.KIND_OUTAGE,
        )

    def test_records_append_and_survive_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = journal.journal_path(root, "2026-08-19")
            journal.append_record(path, self.record(stage="first"))
            journal.append_record(path, self.record(stage="second"))
            records, problems = journal.read_records(path)
            self.assertEqual(problems, [])
            self.assertEqual([r["stage"] for r in records], ["first", "second"])

    def test_a_corrupt_line_is_reported_without_hiding_the_others(self):
        # One bad append must not cost the day's evidence — the failure mode
        # this whole module exists to remove.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = journal.journal_path(root, "2026-08-19")
            journal.append_record(path, self.record(stage="before"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{not json\n")
            journal.append_record(path, self.record(stage="after"))

            records, problems = journal.read_records(path)
            self.assertEqual([r["stage"] for r in records], ["before", "after"])
            self.assertEqual(len(problems), 1)
            self.assertIn("corrupt record", problems[0])

    def test_a_json_line_that_is_not_an_object_is_a_problem_not_a_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = journal.journal_path(Path(tmp), "2026-08-19")
            path.parent.mkdir(parents=True)
            path.write_text('["a list is not a record"]\n', encoding="utf-8")
            records, problems = journal.read_records(path)
            self.assertEqual(records, [])
            self.assertEqual(len(problems), 1)

    def test_record_run_reports_a_write_failure_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".picks").mkdir()
            (root / ".picks" / "journal").write_text("not a directory")
            error = journal.record_run(root, self.record())
            self.assertIsNotNone(error)
            self.assertIn("could not write run journal", error)

    def test_record_run_returns_none_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(journal.record_run(Path(tmp), self.record()))

    def test_missing_journal_reads_as_empty_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            records, problems = journal.read_records(
                journal.journal_path(Path(tmp), "2026-08-19")
            )
            self.assertEqual((records, problems), ([], []))

    def test_unjournalled_days_names_the_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal.record_run(root, self.record(day="2026-08-19"))
            days = journal.day_range("2026-08-18", "2026-08-20")
            self.assertEqual(days, ["2026-08-18", "2026-08-19", "2026-08-20"])
            self.assertEqual(
                journal.unjournalled_days(root, days), ["2026-08-18", "2026-08-20"]
            )

    def test_unjournalled_days_can_be_scoped_to_one_sport(self):
        # A soccer record on a day MLB never ran must not read as MLB coverage.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal.record_run(root, self.record(day="2026-08-19", sport="SOCCER"))
            self.assertEqual(journal.unjournalled_days(root, ["2026-08-19"]), [])
            self.assertEqual(
                journal.unjournalled_days(root, ["2026-08-19"], sport="MLB"),
                ["2026-08-19"],
            )

    def test_day_range_rejects_an_inverted_window(self):
        with self.assertRaises(ValueError):
            journal.day_range("2026-08-20", "2026-08-18")

    def test_deferral_carries_source_and_instant(self):
        item = journal.deferral("LW-1", journal.SOURCE_PRICE_FEED, "book closed")
        self.assertEqual(item["id"], "LW-1")
        self.assertEqual(item["source"], journal.SOURCE_PRICE_FEED)
        self.assertTrue(item["observed_at"].endswith("Z"))

    def test_cli_reports_a_day_and_fails_on_an_unjournalled_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal.record_run(
                root,
                self.record(
                    day="2026-08-19",
                    outcome=journal.OUTCOME_NO_WORK,
                    stage="no_reviewable_work",
                    deferrals=[journal.deferral("LW-1", journal.SOURCE_LINEUP_FEED, "feed down")],
                ),
            )
            self.assertEqual(
                journal.main(["--root", str(root), "--day", "2026-08-19"]), 0
            )
            self.assertEqual(
                journal.main(["--root", str(root), "--day", "2026-08-18"]), 1
            )
            self.assertEqual(
                journal.main([
                    "--root", str(root), "--since", "2026-08-19", "--until", "2026-08-19",
                ]),
                0,
            )
            self.assertEqual(
                journal.main([
                    "--root", str(root), "--since", "2026-08-18", "--until", "2026-08-19",
                ]),
                1,
            )

    def test_format_record_surfaces_the_deferral_source_and_instant(self):
        text = journal.format_record(
            self.record(
                outcome=journal.OUTCOME_REVIEWED,
                stage="complete",
                counts={"approved": 1},
                notices=["a zombie entry"],
                deferrals=[
                    {
                        "id": "LW-1",
                        "source": journal.SOURCE_PRICE_FEED,
                        "reason": "book closed",
                        "observed_at": "2026-08-19T15:29:00Z",
                    }
                ],
            )
        )
        self.assertIn("approved=1", text)
        self.assertIn("notice: a zombie entry", text)
        self.assertIn("deferred: LW-1 via price_feed at 2026-08-19T15:29:00Z", text)

    def test_journal_path_is_dated_and_shared_across_sports(self):
        # One file per day rather than per (day, sport): "did anything run on
        # 08-19?" has to be one stat(), not a search over per-sport names.
        root = Path("/tmp/example")
        self.assertEqual(
            journal.journal_path(root, "2026-08-19"),
            root / ".picks" / "journal" / "2026-08-19-runs.jsonl",
        )

    def test_records_are_json_objects_one_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = journal.journal_path(Path(tmp), "2026-08-19")
            journal.append_record(path, self.record())
            journal.append_record(path, self.record(stage="second"))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                self.assertIsInstance(json.loads(line), dict)


if __name__ == "__main__":
    unittest.main()
