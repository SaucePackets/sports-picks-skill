"""The calibration report must state its population and reconcile to record.json.

The report and record.json legitimately quote different headline totals from
one ledger — the report excludes voids because a void has no outcome to
calibrate against, and record.json includes them. On 2026-08-30 that read as
"44 picks / $842.36" against "45 / $859.96" with nothing on either page saying
why, which is indistinguishable from a stale counter. The difference has to be
printed, not known.
"""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig_calibration_report.py"


def load(picks_file: Path):
    """Load the module with PICKS bound to a fixture (it is module-level)."""
    spec = importlib.util.spec_from_file_location("vig_calibration_report_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict("os.environ", {"VIG_PICKS_FILE": str(picks_file)}):
        spec.loader.exec_module(module)
    return module


def pick(result, notional, pnl, commission=0.0):
    return {
        "status": "settled",
        "result": result,
        "entry_notional": notional,
        "pnl": pnl,
        "commission": commission,
        "entry_price": 0.52,
        "unit_size": notional,
    }


class CalibrationPopulationTests(unittest.TestCase):
    def run_report(self, picks):
        with tempfile.TemporaryDirectory() as tmp:
            picks_file = Path(tmp) / "picks.json"
            picks_file.write_text(json.dumps({"picks": picks}))
            module = load(picks_file)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(module.main(), 0)
            return buffer.getvalue()

    def test_the_header_names_the_population_it_covers(self):
        text = self.run_report([pick("win", 20.0, 8.0), pick("loss", 10.0, -10.0)])
        self.assertIn("2 decided picks", text)
        self.assertIn("population: settled AND decided (win/loss)", text)
        self.assertIn("(canonical)", text)

    def test_a_void_is_excluded_here_and_the_bridge_to_record_json_is_printed(self):
        # The live shape: one void makes the two documents differ by exactly
        # one pick and its stake, and the report has to say so in the numbers.
        text = self.run_report(
            [pick("win", 20.0, 8.0), pick("loss", 10.0, -10.0), pick("void", 17.6, 0.0)]
        )
        self.assertIn("2 decided picks", text)
        self.assertIn("Staked $30.00", text)
        self.assertIn("+ 1 void/push ($17.60) = 3 settled, $47.60 staked", text)
        self.assertIn("vig_ledger_reconcile.py", text)

    def test_without_voids_the_two_populations_are_stated_to_be_the_same(self):
        # Silence would leave the reader to assume it; an unstated equality is
        # how the discrepancy went unexplained in the first place.
        text = self.run_report([pick("win", 20.0, 8.0), pick("loss", 10.0, -10.0)])
        self.assertIn("no voids", text)
        self.assertIn("both documents cover the same 2 settled picks", text)

    def test_the_decided_arithmetic_is_unchanged_by_the_labelling(self):
        # The population fix is presentational; the ROI math must not move.
        text = self.run_report(
            [pick("win", 20.0, 8.0), pick("loss", 10.0, -10.0), pick("void", 17.6, 0.0)]
        )
        self.assertIn("Record: 1-1", text)
        self.assertIn("P&L $-2.00", text)


if __name__ == "__main__":
    unittest.main()
