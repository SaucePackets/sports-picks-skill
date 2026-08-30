"""The observability lane must stay read-only about money.

This slice restores visibility into what the gates did. It must not become a
route to doing anything: no order creation, preview, signing, or submission,
and no widening of a betting rail.

WHAT THIS GUARD COVERS, exactly, and nothing beyond it:

1. Three named root modules are scanned for execution and policy tokens.
2. Those roots' sibling imports under `scripts/` are pinned to one documented
   edge, so the lane cannot quietly acquire a dependency on the execution
   path — which is the route by which a token-clean module still becomes a
   way to place a bet.

What it does NOT do is discover the lane's membership. A fourth observability
module added to `scripts/` and imported by none of these three is not scanned,
and nothing here will say so.

That limit is now stated rather than papered over. The previous wording
claimed the module list "fails if it stops matching what is on disk". It did
not: the length assertion behind that claim could only fire when someone ADDED
a name to the tuple, which is the correct action — it could not fire on the
omission it was named for. Reviewer demonstrated it by dropping a module into
`scripts/` that imports the CLOB SDK and holds a private key; the suite stayed
green (PR #60). An overclaiming guard is worse than a narrow one, because it
retires the attention that would otherwise go on covering the gap.
"""

import ast
import re
import unittest
from pathlib import Path

import import_closure

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# The roots. Named explicitly — see the module docstring for what that does
# and does not buy.
OBSERVABILITY_MODULES = (
    "vig_run_journal.py",
    "vig_ledger_reconcile.py",
    "vig_runtime_verify.py",
)

# The sibling imports each root is allowed to have, and the names it may take.
# vig_run_journal defers its one import into the function body because the
# module-level import in the other direction would be circular; a deferred
# import is still a dependency, and this is where it is bounded. Everything
# else must reach scripts/ not at all.
ALLOWED_SIBLING_IMPORTS = {
    "vig_run_journal.py": {"vig_review_gate_common.py": {"resolve_root"}},
    "vig_ledger_reconcile.py": {},
    "vig_runtime_verify.py": {},
}

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
    def test_every_named_root_is_a_file_that_actually_gets_scanned(self):
        # What this genuinely catches: a root renamed or deleted while the
        # name stays here, which would silently empty the scan below. It does
        # NOT catch a fourth module being added to the feature — see the
        # module docstring.
        for name in OBSERVABILITY_MODULES:
            with self.subTest(module=name):
                self.assertTrue((SCRIPTS / name).is_file(), f"{name} is missing")
        self.assertEqual(
            set(OBSERVABILITY_MODULES), set(ALLOWED_SIBLING_IMPORTS),
            "every scanned root needs a declared import allowance, and vice versa",
        )

    def test_no_root_imports_a_sibling_beyond_its_declared_allowance(self):
        # A token-clean module that IMPORTS the execution path is still a
        # route into it, and the token scan below cannot see an import edge.
        # This is the check that can, and it is the one that widens on its
        # own: it reds on any new sibling import, including one added to a
        # module that is clean today.
        for name, allowed in ALLOWED_SIBLING_IMPORTS.items():
            with self.subTest(module=name):
                self.assertEqual(
                    import_closure.sibling_imports(name), set(allowed),
                    "a new sibling import needs a deliberate decision here",
                )

    def test_the_one_allowed_edge_imports_only_the_name_it_is_allowed(self):
        # Bounding the module is not enough: `from vig_review_gate_common
        # import resolve_root` and `... import normalize_review_routing` are
        # the same edge to the check above and very different dependencies.
        #
        # The allowance is a set of NAMES, so `import vig_review_gate_common`
        # can never satisfy it — a whole-module bind takes every name on the
        # module, which is strictly wider than either of those two and was the
        # one form an earlier version of this check could not see (Reviewer,
        # PR #60 round 2). Both forms are walked here; the plain form is a
        # violation outright.
        #
        # Vacuity guard first — the edge this pins must actually exist, or
        # the loop below asserts nothing.
        self.assertEqual(
            import_closure.sibling_imports("vig_run_journal.py"),
            {"vig_review_gate_common.py"},
        )
        for root, edges in ALLOWED_SIBLING_IMPORTS.items():
            tree = ast.parse((SCRIPTS / root).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        target = f"{alias.name.split('.', 1)[0]}.py"
                        if not (SCRIPTS / target).is_file():
                            continue
                        with self.subTest(module=root, whole_module=target):
                            self.fail(
                                f"{root} binds the whole {target} namespace; the "
                                "allowance is name-scoped, so use the from-form"
                            )
                    continue
                if not isinstance(node, ast.ImportFrom) or node.level:
                    continue
                target = f"{(node.module or '').split('.', 1)[0]}.py"
                if target not in edges:
                    continue
                with self.subTest(module=root, imports=target):
                    self.assertEqual(
                        {alias.name for alias in node.names}, edges[target]
                    )

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
