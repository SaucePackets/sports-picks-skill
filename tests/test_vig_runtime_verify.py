"""Runtime/cron divergence verification.

The failures being pinned here are real ones from this repo's history: cron
jobs left with ``workdir: null`` (so resolve_root falls back to a developer
checkout), and a runtime checkout that sat eight merges behind main with
nothing on the box saying so.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig_runtime_verify.py"
spec = importlib.util.spec_from_file_location("vig_runtime_verify_under_test", SCRIPT_PATH)
assert spec is not None
verify = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["vig_runtime_verify_under_test"] = verify
spec.loader.exec_module(verify)

DEPLOY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-runtime.sh"


def make_runtime(root: Path, marked: bool = True, commit: bool = True) -> Path:
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    if marked:
        (runtime / ".deploy").mkdir()
        (runtime / ".deploy" / "runtime.marker").write_text(
            "runtime checkout created by deploy-runtime.sh 20260819-200145"
        )
    if commit:
        subprocess.run(["git", "init", "-q", str(runtime)], check=True)
        subprocess.run(["git", "-C", str(runtime), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(runtime), "config", "user.name", "t"], check=True)
        (runtime / "README.md").write_text("runtime\n")
        subprocess.run(["git", "-C", str(runtime), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(runtime), "commit", "-qm", "seed"],
            check=True, env={"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root), "PATH": "/usr/bin:/bin"},
        )
    return runtime


def write_jobs(root: Path, jobs: list[dict]) -> Path:
    path = root / "jobs.json"
    path.write_text(json.dumps({"jobs": jobs, "updated_at": "2026-08-30T00:00:00Z"}))
    return path


def job(workdir, enabled=True, **overrides):
    base = {
        "id": "c9452052719c",
        "name": "Vig — MLB Daily Slate (10:30am CT)",
        "enabled": enabled,
        "state": "active" if enabled else "paused",
        "workdir": workdir,
    }
    base.update(overrides)
    return base


def levels(findings, check):
    return [f["level"] for f in findings if f["check"] == check]


def messages(findings, check):
    return " ".join(f["message"] for f in findings if f["check"] == check)


class MarkerTests(unittest.TestCase):
    def test_the_marker_is_the_predicate_and_picks_state_is_not(self):
        # The distinction that cost PR #59 a review round: "has .picks/" is
        # "has pick state", which a developer checkout also has. Only the
        # marker means deploy-managed.
        #
        # Asserted on the predicate itself, not on a finding. A first version
        # of this test asserted only that a .picks-only dev checkout produced
        # a FAIL — and it kept passing when the predicate was widened to
        # accept .picks/, because the widened code then failed one line later
        # trying to read the absent marker. Same verdict, different cause: the
        # test could not observe the thing it was named for.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dev = root / "dev"
            (dev / ".picks").mkdir(parents=True)
            self.assertFalse(verify.is_deploy_managed(dev))
            runtime = make_runtime(root)
            self.assertTrue(verify.is_deploy_managed(runtime))

            self.assertEqual(levels(verify.marker_findings(dev), "runtime-marker"),
                             [verify.LEVEL_FAIL])
            self.assertEqual(
                levels(verify.marker_findings(runtime), "runtime-marker"),
                [verify.LEVEL_OK],
            )

    def test_a_deploy_managed_runtime_that_also_lacks_picks_state_is_accepted(self):
        # The converse, so the predicate is pinned from both sides: pick state
        # is neither necessary nor sufficient. make_runtime() creates no
        # .picks/ at all and must still read as deploy-managed.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp))
            self.assertFalse((runtime / ".picks").exists())
            self.assertTrue(verify.is_deploy_managed(runtime))

    def test_the_marker_this_reads_is_the_marker_the_deploy_writes(self):
        # Paired against deploy-runtime.sh so the two cannot agree by
        # coincidence and drift apart on a rename.
        self.assertIn(
            f'MARKER_REL="{verify.MARKER_REL}"',
            DEPLOY_SCRIPT.read_text(encoding="utf-8"),
        )

    def test_a_missing_runtime_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = verify.marker_findings(Path(tmp) / "absent")
            self.assertEqual(levels(findings, "runtime-dir"), [verify.LEVEL_FAIL])


class CronTests(unittest.TestCase):
    def test_an_aligned_enabled_job_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp))
            findings = verify.cron_findings([job(str(runtime))], runtime)
            self.assertEqual(levels(findings, "cron-workdir"), [verify.LEVEL_OK])

    def test_a_null_workdir_is_named_as_the_developer_checkout_fallback(self):
        # The two reporting jobs' real defect: workdir null is not "unset and
        # harmless", it silently resolves to the developer checkout.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp))
            findings = verify.cron_findings([job(None)], runtime)
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "cron-workdir"))
            self.assertIn("workdir: null", messages(findings, "cron-workdir"))
            self.assertIn("developer checkout", messages(findings, "cron-workdir"))

    def test_an_enabled_job_on_a_foreign_workdir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            dev = root / "dev"
            dev.mkdir()
            findings = verify.cron_findings([job(str(dev))], runtime)
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "cron-workdir"))
            self.assertIn(str(dev), messages(findings, "cron-workdir"))

    def test_a_paused_job_on_a_foreign_workdir_warns_rather_than_fails(self):
        # A paused job runs nothing, so it is not an outage — but it is one
        # `cron resume` from being one, which is exactly how it happened.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            findings = verify.cron_findings([job(None, enabled=False)], runtime)
            self.assertIn(verify.LEVEL_WARN, levels(findings, "cron-workdir"))
            self.assertNotIn(verify.LEVEL_FAIL, levels(findings, "cron-workdir"))

    def test_a_symlinked_workdir_is_not_reported_as_divergence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            link = root / "runtime-link"
            link.symlink_to(runtime)
            findings = verify.cron_findings([job(str(link))], runtime)
            self.assertEqual(levels(findings, "cron-workdir"), [verify.LEVEL_OK])

    def test_a_failing_aligned_job_still_warns(self):
        # Pointing at the right directory is not the same as working.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp))
            findings = verify.cron_findings(
                [job(str(runtime), failure_streak=3, last_status="error")], runtime
            )
            self.assertEqual(levels(findings, "cron-health"), [verify.LEVEL_WARN])

    def test_a_jobs_file_without_a_jobs_list_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text(json.dumps({"not_jobs": []}))
            jobs, error = verify.load_jobs(path)
            self.assertEqual(jobs, [])
            self.assertIn("no jobs list", error)

    def test_a_missing_jobs_file_is_an_error_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, error = verify.load_jobs(Path(tmp) / "absent.json")
            self.assertIn("not found", error)


class CheckoutTests(unittest.TestCase):
    def head(self, runtime):
        return subprocess.run(
            ["git", "-C", str(runtime), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

    def test_expect_sha_mismatch_fails_and_names_both_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp))
            head = self.head(runtime)
            findings = verify.checkout_findings(runtime, "0" * 40)
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "runtime-head"))
            self.assertIn(head, messages(findings, "runtime-head"))

    def test_a_matching_expect_sha_passes_including_an_abbreviation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp))
            head = self.head(runtime)
            for pin in (head, head[:7]):
                with self.subTest(pin=pin):
                    self.assertEqual(
                        levels(verify.checkout_findings(runtime, pin), "runtime-head"),
                        [verify.LEVEL_OK],
                    )

    def test_a_dirty_runtime_tree_fails(self):
        # deploy-runtime.sh hard-resets, so a local modification is both a
        # deploy blocker and unreviewed code running in production.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp))
            (runtime / "README.md").write_text("hand-patched\n")
            findings = verify.checkout_findings(runtime, None)
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "runtime-clean"))
            self.assertIn("README.md", messages(findings, "runtime-clean"))

    def test_a_non_git_runtime_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = make_runtime(Path(tmp), commit=False)
            self.assertEqual(
                levels(verify.checkout_findings(runtime, None), "runtime-head"),
                [verify.LEVEL_FAIL],
            )


class CliTests(unittest.TestCase):
    def test_a_healthy_runtime_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(str(runtime))])
            self.assertEqual(
                verify.main(["--runtime-dir", str(runtime), "--cron-jobs", str(jobs)]), 0
            )

    def test_a_diverged_cron_job_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(None)])
            self.assertEqual(
                verify.main(["--runtime-dir", str(runtime), "--cron-jobs", str(jobs)]), 1
            )

    def test_strict_promotes_a_warning_to_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(None, enabled=False)])
            argv = ["--runtime-dir", str(runtime), "--cron-jobs", str(jobs)]
            self.assertEqual(verify.main(argv), 0)
            self.assertEqual(verify.main([*argv, "--strict"]), 1)

    def test_it_writes_nothing(self):
        # Read-only is a property of this tool, not an intention.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(None)])
            before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            verify.main(["--runtime-dir", str(runtime), "--cron-jobs", str(jobs)])
            after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
