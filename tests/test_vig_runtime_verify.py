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


def write_jobs(root: Path, jobs: list[dict], profile: str = "vig") -> Path:
    # Under the real ``.../profiles/<name>/cron/`` shape, because that path is
    # what declares who owns these jobs and the ownership check reads it.
    path = root / "profiles" / profile / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs, "updated_at": "2026-08-30T00:00:00Z"}))
    return path


def job(workdir, enabled=True, **overrides):
    base = {
        "id": "c9452052719c",
        "name": "Vig — MLB Daily Slate (10:30am CT)",
        "enabled": enabled,
        "state": "active" if enabled else "paused",
        "workdir": workdir,
        "profile": "vig",
        "origin": {"platform": "telegram", "chat_id": "7680342356"},
    }
    base.update(overrides)
    return base


def absent_profile_scripts(root: Path) -> list[str]:
    """CLI args pointing the profile-scripts check at nothing.

    Deliberate: without it the default is the DEVELOPER'S OWN
    ``~/.hermes/profiles/vig/scripts``, so the suite would read live state off
    whichever machine ran it and give different answers on the VPS than on a
    laptop.
    """
    return ["--profile-scripts", str(root / "no-profile-scripts")]


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


class OwnershipTests(unittest.TestCase):
    """Where a job RUNS and who OWNS it are independent, and only one was checked.

    On 2026-09-03 every live job pointed at the deploy-managed runtime — the
    workdir check was clean — and five of nine carried ``profile: null``,
    including the MLB evening slate that writes the same schedule the morning
    job writes.
    """

    def test_an_owned_job_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_file = write_jobs(root, [job(str(root))])
            findings = verify.ownership_findings([job(str(root))], jobs_file)
            self.assertEqual(levels(findings, "cron-owner"), [verify.LEVEL_OK])
            self.assertEqual(levels(findings, "cron-origin"), [])

    def test_an_enabled_job_with_a_null_profile_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_file = write_jobs(root, [])
            findings = verify.ownership_findings(
                [job(str(root), profile=None)], jobs_file
            )
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "cron-owner"))
            self.assertIn("profile None", messages(findings, "cron-owner"))
            self.assertIn("vig", messages(findings, "cron-owner"))

    def test_a_paused_job_with_a_null_profile_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_file = write_jobs(root, [])
            findings = verify.ownership_findings(
                [job(str(root), enabled=False, profile=None)], jobs_file
            )
            self.assertIn(verify.LEVEL_WARN, levels(findings, "cron-owner"))
            self.assertNotIn(verify.LEVEL_FAIL, levels(findings, "cron-owner"))

    def test_a_job_owned_by_another_profile_fails(self):
        """``profile: null`` is not the only wrong owner; any other one is too."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_file = write_jobs(root, [])
            findings = verify.ownership_findings(
                [job(str(root), profile="rebecca")], jobs_file
            )
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "cron-owner"))
            self.assertIn("'rebecca'", messages(findings, "cron-owner"))

    def test_the_expected_owner_is_read_off_the_path_not_hard_coded(self):
        """A jobs file under ``profiles/taylor`` expects ``taylor``, not ``vig``."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_file = write_jobs(root, [], profile="taylor")
            self.assertEqual(verify.profile_name_for(jobs_file), "taylor")
            self.assertEqual(
                levels(
                    verify.ownership_findings([job(str(root), profile="taylor")], jobs_file),
                    "cron-owner",
                ),
                [verify.LEVEL_OK],
            )

    def test_an_underivable_owner_says_so_instead_of_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stray = root / "jobs.json"
            stray.write_text("{}")
            findings = verify.ownership_findings([job(str(root), profile=None)], stray)
            self.assertEqual(levels(findings, "cron-owner"), [verify.LEVEL_WARN])
            self.assertIn("cannot be derived", messages(findings, "cron-owner"))

    def test_a_missing_origin_warns_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_file = write_jobs(root, [])
            findings = verify.ownership_findings(
                [job(str(root), origin=None)], jobs_file
            )
            self.assertEqual(levels(findings, "cron-origin"), [verify.LEVEL_WARN])
            self.assertNotIn(verify.LEVEL_FAIL, levels(findings, "cron-owner"))


def make_profile_scripts(root: Path, runtime: Path, *, names=None, extra=None) -> Path:
    """A deployed profile scripts dir seeded from the runtime checkout."""
    profile = root / "profile-scripts"
    profile.mkdir(parents=True, exist_ok=True)
    manifest, error = verify.load_profile_manifest(runtime / verify.DEPLOY_SCRIPT_REL)
    assert not error, error
    for name in names if names is not None else manifest:
        (profile / name).write_text((runtime / "scripts" / name).read_text())
    for name, text in (extra or {}).items():
        (profile / name).write_text(text)
    return profile


def make_repo_runtime(root: Path) -> Path:
    """A runtime checkout carrying this repository's real scripts/ tree.

    The real one, because the manifest is parsed out of the real deploy script:
    a hand-written stand-in would pin the test against a fiction of the
    manifest rather than the manifest.
    """
    runtime = make_runtime(root)
    source = Path(__file__).resolve().parents[1] / "scripts"
    (runtime / "scripts").mkdir()
    manifest, error = verify.load_profile_manifest(source / "deploy-runtime.sh")
    assert not error, error
    (runtime / "scripts" / "deploy-runtime.sh").write_text(
        (source / "deploy-runtime.sh").read_text()
    )
    for name in manifest:
        (runtime / "scripts" / name).write_text((source / name).read_text())
    return runtime


class ProfileScriptTests(unittest.TestCase):
    """The deployed copies are the manifest, byte for byte, and nothing else."""

    def test_the_manifest_is_parsed_from_the_deploy_script(self):
        manifest, error = verify.load_profile_manifest(DEPLOY_SCRIPT)
        self.assertIsNone(error)
        self.assertIn("mlb_game_reads.py", manifest)
        self.assertIn("mlb_slate_receipt.py", manifest)
        self.assertTrue(all(name.endswith(".py") for name in manifest), manifest)
        self.assertEqual(len(manifest), len(set(manifest)), manifest)

    def test_a_commented_out_entry_is_not_a_manifest_file(self):
        """The suffix test alone cannot see this, which is why it is its own case.

        `# retired_thing.py` ends in `.py`, so a parser that only checks the
        suffix adopts it — and then FAILs because no such file is deployed, an
        alarm invented by the reader. The real deploy script now carries a
        rationale comment inside the array, so this is not hypothetical.
        """
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "deploy-runtime.sh"
            script.write_text(
                "PROFILE_MANIFEST=(\n"
                "  kept.py\n"
                # Deliberately ends in `.py` with nothing after it: a trailing
                # `— dropped 2026-01-01` would make this line pass the suffix
                # test too, and the fixture would prove nothing.
                "  # retired_thing.py\n"
                "  #already_tight.py\n"
                "  also_kept.py\n"
                ")\n"
            )
            manifest, error = verify.load_profile_manifest(script)
            self.assertIsNone(error)
            self.assertEqual(manifest, ["kept.py", "also_kept.py"])

    def test_an_absent_deploy_script_fails_rather_than_reporting_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            profile = root / "profile-scripts"
            profile.mkdir()
            findings = verify.profile_script_findings(runtime, profile)
            self.assertEqual(levels(findings, "profile-manifest"), [verify.LEVEL_FAIL])

    def test_a_matching_deployment_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_repo_runtime(root)
            profile = make_profile_scripts(root, runtime)
            findings = verify.profile_script_findings(runtime, profile)
            self.assertEqual(levels(findings, "profile-scripts"), [verify.LEVEL_OK])
            self.assertEqual(levels(findings, "profile-unmanaged"), [])

    def test_a_drifted_manifest_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_repo_runtime(root)
            profile = make_profile_scripts(root, runtime)
            (profile / "mlb_game_reads.py").write_text("# an older copy\n")
            findings = verify.profile_script_findings(runtime, profile)
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "profile-scripts"))
            self.assertIn("mlb_game_reads.py differs", messages(findings, "profile-scripts"))

    def test_a_missing_manifest_copy_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_repo_runtime(root)
            profile = make_profile_scripts(root, runtime)
            (profile / "mlb_game_reads.py").unlink()
            findings = verify.profile_script_findings(runtime, profile)
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "profile-scripts"))
            self.assertIn("dies at import time", messages(findings, "profile-scripts"))

    def test_an_unmanaged_copy_of_a_repo_script_fails(self):
        """The 2026-09-01 ``mlb_slate_receipt.py`` shape, before it was managed.

        The installer seeds each stage from the LIVE directory so unmanaged
        files survive every deploy — which means a hand-copied module is
        frozen at the day it was made and no deploy will ever fix it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_repo_runtime(root)
            manifest, _ = verify.load_profile_manifest(runtime / verify.DEPLOY_SCRIPT_REL)
            shadowed = "mlb_slate_receipt.py"
            self.assertIn(shadowed, manifest)
            profile = make_profile_scripts(
                root,
                runtime,
                names=[name for name in manifest if name != shadowed],
                extra={shadowed: "# a copy made by hand on 2026-09-01\n"},
            )
            # Simulate the pre-fix world: the file is a repo script that the
            # manifest does not name.
            deploy = runtime / verify.DEPLOY_SCRIPT_REL
            deploy.write_text(deploy.read_text().replace(f"  {shadowed}\n", ""))

            findings = verify.profile_script_findings(runtime, profile)
            self.assertIn(verify.LEVEL_FAIL, levels(findings, "profile-unmanaged"))
            self.assertIn("shadows a repo script", messages(findings, "profile-unmanaged"))
            self.assertIn(shadowed, messages(findings, "profile-unmanaged"))

    def test_an_unmanaged_file_this_repo_does_not_ship_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_repo_runtime(root)
            profile = make_profile_scripts(
                root, runtime, extra={"local_helper.py": "# not ours\n"}
            )
            findings = verify.profile_script_findings(runtime, profile)
            self.assertEqual(levels(findings, "profile-unmanaged"), [verify.LEVEL_WARN])

    def test_an_absent_profile_directory_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_repo_runtime(root)
            findings = verify.profile_script_findings(runtime, root / "absent")
            self.assertEqual(levels(findings, "profile-scripts"), [verify.LEVEL_WARN])


class CliTests(unittest.TestCase):
    def test_a_healthy_runtime_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(str(runtime))])
            self.assertEqual(
                verify.main(["--runtime-dir", str(runtime), "--cron-jobs", str(jobs),
                             *absent_profile_scripts(root)]), 0
            )

    def test_a_diverged_cron_job_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(None)])
            self.assertEqual(
                verify.main(["--runtime-dir", str(runtime), "--cron-jobs", str(jobs),
                             *absent_profile_scripts(root)]), 1
            )

    def test_strict_promotes_a_warning_to_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(None, enabled=False)])
            argv = ["--runtime-dir", str(runtime), "--cron-jobs", str(jobs),
                    *absent_profile_scripts(root)]
            self.assertEqual(verify.main(argv), 0)
            self.assertEqual(verify.main([*argv, "--strict"]), 1)

    def test_it_writes_nothing(self):
        # Read-only is a property of this tool, not an intention.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = make_runtime(root)
            jobs = write_jobs(root, [job(None)])
            before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            verify.main(["--runtime-dir", str(runtime), "--cron-jobs", str(jobs),
                         *absent_profile_scripts(root)])
            after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
