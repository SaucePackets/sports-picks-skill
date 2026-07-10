import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vig-review-verify.py"
spec = importlib.util.spec_from_file_location("vig_review_verify", SCRIPT_PATH)
assert spec is not None
vig_review_verify = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["vig_review_verify"] = vig_review_verify
spec.loader.exec_module(vig_review_verify)


class VigReviewVerifyTests(unittest.TestCase):
    DATE = "2026-07-10"
    FIRE = "2026-07-10T22:10:00Z"
    JOB_ID = "abc123cron"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".picks" / "execute").mkdir(parents=True)
        self.schedule_path = self.root / ".picks" / "execute" / f"{self.DATE}-schedule.json"
        self.cron_path = self.root / "jobs.json"
        self.picks_path = self.root / "picks.json"
        self.latest_path = self.root / ".picks" / "latest-action.md"

        self.schedule = {
            "date": self.DATE,
            "status": "vig-reviewed",
            "candidates": [
                {
                    "polymarket_slug": "aec-mlb-abc-def-2026-07-10",
                    "side": "ABC",
                    "unit_size": 18,
                    "vig_review_needed": False,
                    "vig_approved": True,
                    "vig_notes": "Starter, price, lineup, and weather gates hold.",
                    "execution_cron_id": self.JOB_ID,
                    "execution_cron_fire_utc": self.FIRE,
                },
                {
                    "polymarket_slug": "aec-mlb-ghi-jkl-2026-07-10",
                    "side": "GHI",
                    "unit_size": 18,
                    "vig_review_needed": False,
                    "vig_approved": False,
                    "vig_notes": "Price moved through the approved threshold.",
                },
            ],
            "approved_exposure": 18,
            "daily_cap": 90,
        }
        self.job = {
            "id": self.JOB_ID,
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "once", "run_at": "2026-07-10T22:10:00+00:00"},
            "next_run_at": "2026-07-10T22:10:00+00:00",
            "repeat": {"times": 1, "completed": 0},
            "deliver": vig_review_verify.DEFAULT_DELIVER,
            "skills": vig_review_verify.DEFAULT_SKILLS,
            "workdir": str(self.root),
            "provider": vig_review_verify.DEFAULT_PROVIDER,
            "model": vig_review_verify.DEFAULT_MODEL,
        }
        self.picks = {
            "picks": [
                {"market_slug": "old", "side": "ABC", "status": "settled", "result": "win"},
                {"market_slug": "same-slug", "side": "ABC", "status": "active", "result": None},
                {"market_slug": "same-slug", "side": "DEF", "status": "active", "result": None},
            ]
        }
        self.latest = (
            f"{self.DATE}: Vig review complete. 1 approved, 1 flagged. "
            f"One-shots: {self.JOB_ID}. Approved exposure $18 / $90.\n"
        )
        self.write_fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def write_fixture(self):
        self.schedule_path.write_text(json.dumps(self.schedule), encoding="utf-8")
        self.cron_path.write_text(json.dumps({"jobs": [self.job]}), encoding="utf-8")
        self.picks_path.write_text(json.dumps(self.picks), encoding="utf-8")
        self.latest_path.write_text(self.latest, encoding="utf-8")

    def run_main(self):
        return vig_review_verify.main(
            [
                self.DATE,
                "--root",
                str(self.root),
                "--cron-jobs-file",
                str(self.cron_path),
                "--picks-file",
                str(self.picks_path),
                "--latest-action-file",
                str(self.latest_path),
            ]
        )

    def test_valid_review_handoff_exits_zero(self):
        self.assertEqual(self.run_main(), 0)

    def test_every_candidate_requires_boolean_decision_and_notes(self):
        self.schedule["candidates"][0]["vig_approved"] = None
        self.schedule["candidates"][1]["vig_notes"] = "  "
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_approved_candidate_requires_cron_id_and_fire_time(self):
        self.schedule["candidates"][0].pop("execution_cron_id")
        self.schedule["candidates"][0]["execution_cron_fire_utc"] = "not-a-time"
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_cron_must_be_active_matching_one_shot_with_expected_runtime(self):
        mutations = {
            "disabled": lambda job: job.update(enabled=False),
            "wrong fire": lambda job: job["schedule"].update(run_at="2026-07-10T22:11:00Z"),
            "wrong repeat": lambda job: job.update(repeat={"times": 2, "completed": 0}),
            "wrong delivery": lambda job: job.update(deliver="local"),
            "wrong skills": lambda job: job.update(skills=["sports-data-apis"]),
            "wrong workdir": lambda job: job.update(workdir="/tmp/wrong"),
            "wrong provider": lambda job: job.update(provider="openai-codex"),
            "wrong model": lambda job: job.update(model="gpt-5.6-sol"),
        }
        original = json.loads(json.dumps(self.job))
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.job = json.loads(json.dumps(original))
                mutate(self.job)
                self.write_fixture()
                self.assertEqual(self.run_main(), 1)

    def test_duplicate_active_slug_and_side_fails(self):
        self.picks["picks"].append(
            {"market_slug": "same-slug", "side": "abc", "status": "pending", "result": None}
        )
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_final_result_is_not_treated_as_active_even_with_stale_active_status(self):
        self.picks["picks"].append(
            {"market_slug": "same-slug", "side": "ABC", "status": "active", "result": "loss"}
        )
        self.write_fixture()
        self.assertEqual(self.run_main(), 0)

    def test_schedule_exposure_must_equal_approved_units(self):
        self.schedule["approved_exposure"] = 17
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_latest_action_must_match_counts_ids_and_exposure(self):
        self.latest = f"{self.DATE}: Vig review complete. 2 approved, 0 flagged. Approved exposure $36 / $90.\n"
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_read_only_does_not_mutate_runtime_files(self):
        paths = [self.schedule_path, self.cron_path, self.picks_path, self.latest_path]
        before = {path: path.read_bytes() for path in paths}
        self.assertEqual(self.run_main(), 0)
        after = {path: path.read_bytes() for path in paths}
        self.assertEqual(before, after)

    def test_rejects_impossible_calendar_date(self):
        self.assertEqual(vig_review_verify.main(["2026-02-30", "--root", str(self.root)]), 2)


if __name__ == "__main__":
    unittest.main()
