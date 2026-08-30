"""The postgame gate's wiring: a reconciler's verdict must reach the prompt.

`vig_ledger_reconcile.py` is covered as a module by its own tests. That is
evidence about the module, not about enforcement — the reconciler could be
perfectly correct and report to nobody if `vig_postgame_gate` dropped its
return code on the floor (Reviewer, PR #60). These tests drive `main()` with
each reconciler stubbed and assert the verdict lands in the settlement prompt
the child agent actually receives.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SCRIPT_PATH = SCRIPTS / "vig_postgame_gate.py"
spec = importlib.util.spec_from_file_location("vig_postgame_gate", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
vig_postgame_gate = importlib.util.module_from_spec(spec)
sys.modules["vig_postgame_gate"] = vig_postgame_gate
spec.loader.exec_module(vig_postgame_gate)


SETTLED_PICK = {
    "pick_id": "MLB-20260824-CWS",
    "status": "settled",
    "result": "win",
    "entry_price": 0.48,
    "win_probability": 0.55,
}


class PostgameGateWiringTests(unittest.TestCase):
    def _run_main(
        self,
        *,
        picks,
        recon_rc=0,
        recon_stdout="",
        ledger_rc=0,
        ledger_stdout="",
    ):
        """Drive main() with both reconcilers stubbed; return (status, prompt).

        prompt is None when the child settlement agent was never spawned,
        which is itself the assertion for the clean case — a test that only
        ever checks prompt CONTENT cannot tell "the conflict reached the
        prompt" from "the prompt is always built".
        """
        seen = {"prompt": None}

        def fake_run(cmd, *args, **kwargs):
            completed = vig_postgame_gate.subprocess.CompletedProcess
            script = Path(cmd[1]).name if len(cmd) > 1 else ""
            if script == "receipts_ledger_reconcile.py":
                return completed(cmd, recon_rc, stdout=recon_stdout, stderr="")
            if script == "vig_ledger_reconcile.py":
                return completed(cmd, ledger_rc, stdout=ledger_stdout, stderr="")
            seen["prompt"] = cmd[cmd.index("-q") + 1]
            return completed(cmd, 0, stdout="[SILENT]", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            picks_file = Path(tmp) / "picks.json"
            picks_file.write_text(json.dumps({"picks": picks}))
            output = StringIO()
            with (
                patch.object(vig_postgame_gate, "PICKS", picks_file),
                patch.object(vig_postgame_gate, "ROOT", Path(tmp)),
                patch.object(vig_postgame_gate.subprocess, "run", side_effect=fake_run),
                redirect_stdout(output),
            ):
                status = vig_postgame_gate.main()
        return status, seen["prompt"]

    def test_a_clean_ledger_with_nothing_open_spawns_no_settlement_agent(self):
        # The discriminator. Without this, every assertion below would also
        # hold for a gate that spawned the child unconditionally.
        status, prompt = self._run_main(picks=[SETTLED_PICK])

        self.assertEqual(status, 0)
        self.assertIsNone(prompt)

    def test_a_ledger_conflict_reaches_the_settlement_prompt(self):
        # vig_ledger_reconcile's verdict is what this PR added; nothing
        # previously asserted it was read by anyone.
        status, prompt = self._run_main(
            picks=[SETTLED_PICK],
            ledger_rc=1,
            ledger_stdout="record.json says 45 settled; picks.json says 44",
        )

        self.assertEqual(status, 0)
        self.assertIsNotNone(prompt, "a ledger conflict must spawn the settlement agent")
        self.assertIn("LEDGER CONFLICTS", prompt)
        self.assertIn("record.json says 45 settled; picks.json says 44", prompt)
        # And it carries the resolution direction, not just the discrepancy:
        # a counter edited to match a report is the failure being prevented.
        self.assertIn("picks.json is canonical", prompt)

    def test_a_receipt_audit_gap_reaches_the_settlement_prompt(self):
        status, prompt = self._run_main(
            picks=[SETTLED_PICK],
            recon_rc=1,
            recon_stdout="filled receipt RCPT-9 has no ledger row",
        )

        self.assertEqual(status, 0)
        self.assertIsNotNone(prompt)
        self.assertIn("RECEIPT AUDIT DISCREPANCIES", prompt)
        self.assertIn("filled receipt RCPT-9 has no ledger row", prompt)

    def test_the_two_reconcilers_report_independently(self):
        # They ask different questions — did every fill reach the ledger, and
        # does everything reading the ledger see the same numbers — so one
        # firing must not be able to stand in for the other.
        _, ledger_only = self._run_main(
            picks=[SETTLED_PICK], ledger_rc=1, ledger_stdout="counter drift"
        )
        _, receipts_only = self._run_main(
            picks=[SETTLED_PICK], recon_rc=1, recon_stdout="missing row"
        )
        _, both = self._run_main(
            picks=[SETTLED_PICK],
            recon_rc=1,
            recon_stdout="missing row",
            ledger_rc=1,
            ledger_stdout="counter drift",
        )

        self.assertNotIn("RECEIPT AUDIT DISCREPANCIES", ledger_only)
        self.assertNotIn("LEDGER CONFLICTS", receipts_only)
        self.assertIn("RECEIPT AUDIT DISCREPANCIES", both)
        self.assertIn("LEDGER CONFLICTS", both)

    def test_an_open_pick_still_spawns_settlement_with_a_clean_ledger(self):
        status, prompt = self._run_main(
            picks=[dict(SETTLED_PICK, status="active", result=None)]
        )

        self.assertEqual(status, 0)
        self.assertIsNotNone(prompt)
        self.assertIn("MLB-20260824-CWS", prompt)
        self.assertNotIn("LEDGER CONFLICTS", prompt)


if __name__ == "__main__":
    unittest.main()
