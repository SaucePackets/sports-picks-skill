"""Ledger conflict detection: picks.json vs its derived record.json view."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig_ledger_reconcile.py"
spec = importlib.util.spec_from_file_location("vig_ledger_reconcile_under_test", SCRIPT_PATH)
assert spec is not None
reconcile = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["vig_ledger_reconcile_under_test"] = reconcile
spec.loader.exec_module(reconcile)


def pick(result, notional, pnl, commission=0.0, status="settled"):
    return {
        "status": status,
        "result": result,
        "entry_notional": notional,
        "pnl": pnl,
        "commission": commission,
    }


# Two wins, one loss, one void: the shape that makes the settled and decided
# populations differ, which is where the live 44-vs-45 discrepancy came from.
PICKS = [
    pick("win", 20.0, 8.0, 0.5),
    pick("win", 10.0, 4.0, 0.25),
    pick("loss", 15.0, -15.0, 0.0),
    pick("void", 12.0, 0.0, 0.0),
    pick(None, 9.0, 0.0, 0.0, status="active"),
]


def consistent_record():
    counters = reconcile.recompute_counters(PICKS)
    record = {key: counters[key] for key in
              ("total", "settled", "wins", "losses", "voids", "decision_count",
               "pending", "active", "win_rate", "total_staked",
               "total_commission_paid", "total_pnl")}
    record["current_streak"] = "W1"  # not derivable; must be ignored
    return record


class RecomputeTests(unittest.TestCase):
    def test_counters_match_the_hand_counted_fixture(self):
        counters = reconcile.recompute_counters(PICKS)
        self.assertEqual(counters["total"], 5)
        self.assertEqual(counters["settled"], 4)
        self.assertEqual(counters["wins"], 2)
        self.assertEqual(counters["losses"], 1)
        self.assertEqual(counters["voids"], 1)
        self.assertEqual(counters["decision_count"], 3)
        self.assertEqual(counters["active"], 1)
        # Staked spans the SETTLED population, voids included — the same
        # definition record.json uses, so the check cannot report a
        # population difference as a conflict.
        self.assertEqual(counters["total_staked"], 57.0)
        self.assertEqual(counters["total_pnl"], -3.0)

    def test_a_consistent_record_reports_no_conflicts(self):
        record = consistent_record()
        self.assertEqual(reconcile.counter_conflicts(PICKS, record), [])
        self.assertEqual(reconcile.internal_conflicts(record), [])

    def test_a_field_the_view_does_not_carry_is_not_a_conflict(self):
        # A view carrying fewer counters is allowed; demanding presence would
        # turn a schema addition into a false alarm, and one false alarm makes
        # the whole check ignorable.
        record = {"wins": 2}
        self.assertEqual(reconcile.counter_conflicts(PICKS, record), [])

    def test_a_field_the_ledger_cannot_derive_is_never_judged(self):
        record = consistent_record()
        record["current_streak"] = "L9"
        self.assertEqual(reconcile.counter_conflicts(PICKS, record), [])

    def test_a_stale_counter_is_reported_by_name(self):
        # The live defect: record.json goes stale and every report quoting it
        # is wrong, with nothing saying so.
        record = consistent_record()
        record["wins"] = 3
        conflicts = reconcile.counter_conflicts(PICKS, record)
        self.assertEqual([c["field"] for c in conflicts], ["wins"])
        self.assertEqual(conflicts[0]["stored"], 3)
        self.assertEqual(conflicts[0]["expected"], 2)
        self.assertEqual(conflicts[0]["kind"], "derived")

    def test_a_stale_money_total_is_reported(self):
        record = consistent_record()
        record["total_staked"] = 99.0
        self.assertEqual(
            [c["field"] for c in reconcile.counter_conflicts(PICKS, record)],
            ["total_staked"],
        )

    def test_a_sub_cent_money_difference_is_not_a_conflict(self):
        # Both sides store rounded dollars; exact float equality would report
        # a conflict on every rounding and the check would be ignored.
        record = consistent_record()
        record["total_staked"] = 57.004
        self.assertEqual(reconcile.counter_conflicts(PICKS, record), [])
        record["total_staked"] = 57.02
        self.assertEqual(
            [c["field"] for c in reconcile.counter_conflicts(PICKS, record)],
            ["total_staked"],
        )

    def test_internal_conflict_is_caught_without_reading_the_ledger(self):
        # Bites even when the ledger is unreadable, which is exactly when a
        # derived-view check cannot run.
        conflicts = reconcile.internal_conflicts(
            {"settled": 10, "wins": 2, "losses": 1, "voids": 1}
        )
        self.assertEqual([c["field"] for c in conflicts], ["settled"])
        self.assertEqual(conflicts[0]["expected"], 4)

    def test_decision_count_must_equal_wins_plus_losses(self):
        conflicts = reconcile.internal_conflicts(
            {"wins": 2, "losses": 1, "decision_count": 4}
        )
        self.assertEqual([c["field"] for c in conflicts], ["decision_count"])


class SourceConflictTests(unittest.TestCase):
    def test_a_symlink_to_the_canonical_ledger_is_not_a_conflict(self):
        # This is the 2026-08-23 fix, not the fault: the runtime's
        # .picks/picks.json is a symlink to the canonical file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "picks.json"
            canonical.write_text(json.dumps({"picks": PICKS}))
            link = root / "runtime-picks.json"
            link.symlink_to(canonical)
            self.assertEqual(reconcile.source_conflicts([canonical, link]), [])

    def test_a_second_real_file_with_different_content_is_a_split_brain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "picks.json"
            canonical.write_text(json.dumps({"picks": PICKS}))
            other = root / "runtime-picks.json"
            other.write_text(json.dumps({"picks": PICKS[:2]}))
            conflicts = reconcile.source_conflicts([canonical, other])
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["kind"], "source")
            self.assertIn("DIFFER", conflicts[0]["detail"])

    def test_a_byte_identical_copy_is_reported_as_a_latent_split_brain(self):
        # Identical today is not the same as kept identical. This is how the
        # original split brain started: two files that agreed until they did not.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = json.dumps({"picks": PICKS})
            canonical = root / "picks.json"
            canonical.write_text(payload)
            copy = root / "copy-picks.json"
            copy.write_text(payload)
            conflicts = reconcile.source_conflicts([canonical, copy])
            self.assertEqual(len(conflicts), 1)
            self.assertIn("latent split brain", conflicts[0]["detail"])

    def test_a_path_that_does_not_exist_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "picks.json"
            canonical.write_text(json.dumps({"picks": PICKS}))
            self.assertEqual(
                reconcile.source_conflicts([canonical, root / "absent.json"]), []
            )


class CliTests(unittest.TestCase):
    def write_pair(self, root, record):
        picks_file = root / "picks.json"
        picks_file.write_text(json.dumps({"picks": PICKS}))
        (root / "record.json").write_text(json.dumps(record))
        return picks_file

    def test_clean_pair_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            picks_file = self.write_pair(root, consistent_record())
            self.assertEqual(reconcile.main(["--picks-file", str(picks_file)]), 0)

    def test_conflicting_pair_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = consistent_record()
            record["losses"] = 7
            picks_file = self.write_pair(root, record)
            self.assertEqual(reconcile.main(["--picks-file", str(picks_file)]), 1)

    def test_a_missing_record_view_is_a_problem_not_a_silent_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            picks_file = root / "picks.json"
            picks_file.write_text(json.dumps({"picks": PICKS}))
            self.assertEqual(reconcile.main(["--picks-file", str(picks_file)]), 1)

    def test_an_unreadable_canonical_ledger_is_a_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            picks_file = root / "picks.json"
            picks_file.write_text("{not json")
            (root / "record.json").write_text(json.dumps(consistent_record()))
            self.assertEqual(reconcile.main(["--picks-file", str(picks_file)]), 1)

    def test_also_ledger_surfaces_a_second_claimant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            picks_file = self.write_pair(root, consistent_record())
            other = root / "runtime-picks.json"
            other.write_text(json.dumps({"picks": PICKS[:1]}))
            self.assertEqual(
                reconcile.main([
                    "--picks-file", str(picks_file), "--also-ledger", str(other),
                ]),
                1,
            )


if __name__ == "__main__":
    unittest.main()
