"""The observability lane must stay read-only about money.

This slice restores visibility into what the gates did. It must not become a
route to doing anything: no order creation, preview, signing, or submission,
and no widening of a betting rail. A guard, not a promise — the three modules
this feature added are pinned here, and the assertion below fails if that list
stops matching what is on disk, so a fourth module cannot be added silently
and skip the check.
"""

import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# The modules this feature introduced. Named explicitly so the guard states
# what it covers rather than sweeping a directory whose contents it cannot
# describe.
OBSERVABILITY_MODULES = (
    "vig_run_journal.py",
    "vig_ledger_reconcile.py",
    "vig_runtime_verify.py",
)

# Execution surface. Substrings, matched case-insensitively, that no
# observability module may name. Each is a real entrypoint in this repo's
# Polymarket path or the CLOB SDK it wraps.
FORBIDDEN_EXECUTION_TOKENS = (
    "create_order",
    "post_order",
    "create_and_post_order",
    "submit_order",
    "place_order",
    "sign_order",
    "clob",
    "order_args",
    "private_key",
    "polymarket_us_sdk_bet",
    "execution_guard",
)

# Rails an observability change must not touch. A module that writes one of
# these is deciding what gets bet, not recording what happened.
FORBIDDEN_POLICY_TOKENS = (
    "min_conservative_edge",
    "max_mlb_official_bets_per_day",
    "max_small_bets_per_day",
    "standing_authorization",
    "risk_limits",
)


class ObservabilityStaysReadOnlyTests(unittest.TestCase):
    def test_the_module_list_matches_what_is_on_disk(self):
        # Vacuity guard, and more: a NARROW list is the failure this repo
        # keeps rediscovering (PR #59). If a module is added to the feature
        # without being added here, this fails rather than passing over it.
        for name in OBSERVABILITY_MODULES:
            with self.subTest(module=name):
                self.assertTrue((SCRIPTS / name).is_file(), f"{name} is missing")
        self.assertEqual(len(OBSERVABILITY_MODULES), 3)

    def test_no_observability_module_names_an_execution_entrypoint(self):
        for name in OBSERVABILITY_MODULES:
            text = (SCRIPTS / name).read_text(encoding="utf-8").lower()
            for token in FORBIDDEN_EXECUTION_TOKENS:
                with self.subTest(module=name, token=token):
                    self.assertNotIn(token, text)

    def test_no_observability_module_reads_or_writes_a_betting_rail(self):
        for name in OBSERVABILITY_MODULES:
            text = (SCRIPTS / name).read_text(encoding="utf-8").lower()
            for token in FORBIDDEN_POLICY_TOKENS:
                with self.subTest(module=name, token=token):
                    self.assertNotIn(token, text)

    def test_the_reconcilers_and_the_verifier_never_open_a_file_for_writing(self):
        # vig_run_journal is the deliberate exception: it appends its own
        # journal and nothing else. The other two must not write at all, which
        # is what makes them safe to run against live state.
        for name in ("vig_ledger_reconcile.py", "vig_runtime_verify.py"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertIsNone(
                    re.search(r"\.write_text\(|\.write_bytes\(|open\([^)]*['\"][wax]", text),
                    f"{name} must not write",
                )
                self.assertNotIn("mkdir(", text)

    def test_the_journal_writes_only_under_its_own_directory(self):
        text = (SCRIPTS / "vig_run_journal.py").read_text(encoding="utf-8")
        # One write path, and it is built by journal_path.
        self.assertEqual(text.count('.picks" / "journal"'), 1)
        self.assertEqual(len(re.findall(r"\.open\(\"a\"", text)), 1)


if __name__ == "__main__":
    unittest.main()
