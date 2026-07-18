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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".picks" / "execute").mkdir(parents=True)
        self.schedule_path = self.root / ".picks" / "execute" / f"{self.DATE}-schedule.json"
        self.picks_path = self.root / "picks.json"
        self.latest_path = self.root / ".picks" / "latest-action.md"

        self.schedule = {
            "date": self.DATE,
            "sport": "MLB",
            "market_type": "moneyline",
            "status": "vig-reviewed",
            "candidates": [
                {
                    "polymarket_slug": "aec-mlb-abc-def-2026-07-10",
                    "side": "ABC",
                    "sport": "MLB",
                    "market_type": "moneyline",
                    "first_pitch_utc": "2026-07-10T23:00:00Z",
                    "unit_size": 18,
                    "max_polymarket_price": 0.51,
                    "vig_review_needed": False,
                    "vig_approved": True,
                    "vig_notes": "Starter, price, lineup, and weather gates hold.",
                    "execution_mode": "standing_authorized",
                    "execution_status": "pending",
                    "executed": False,
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
        self.picks = {
            "picks": [
                {"market_slug": "old", "side": "ABC", "status": "settled", "result": "win"},
                {"market_slug": "same-slug", "side": "ABC", "status": "active", "result": None},
                {"market_slug": "same-slug", "side": "DEF", "status": "active", "result": None},
            ]
        }
        self.latest = (
            f"{self.DATE}: Vig review complete. 1 approved, 1 flagged. "
            "Routed to MLB execution poller. Approved exposure $18 / $90.\n"
        )
        self.write_fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def write_fixture(self):
        self.schedule_path.write_text(json.dumps(self.schedule), encoding="utf-8")
        self.picks_path.write_text(json.dumps(self.picks), encoding="utf-8")
        self.latest_path.write_text(self.latest, encoding="utf-8")

    def run_main(self):
        return vig_review_verify.main(
            [
                self.DATE,
                "--root",
                str(self.root),
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

    def test_approved_candidate_requires_standing_authorized_pending_state(self):
        self.schedule["candidates"][0]["execution_mode"] = "manual"
        self.schedule["candidates"][0]["execution_status"] = "awaiting_jerry"
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_approved_candidate_requires_explicit_polymarket_price_cap(self):
        self.schedule["candidates"][0].pop("max_polymarket_price")
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_approved_candidate_rejects_string_polymarket_price_cap(self):
        self.schedule["candidates"][0]["max_polymarket_price"] = "0.51"
        self.write_fixture()
        self.assertEqual(self.run_main(), 1)

    def test_approved_candidate_rejects_execution_artifacts(self):
        mutations = {
            "executed": lambda candidate: candidate.update(executed=True),
            "cron id": lambda candidate: candidate.update(execution_cron_id="unsafe"),
            "cron fire": lambda candidate: candidate.update(execution_cron_fire_utc=self.FIRE),
            "approval token": lambda candidate: candidate.update(approval_token="unsafe"),
        }
        original = json.loads(json.dumps(self.schedule["candidates"][0]))
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.schedule["candidates"][0] = json.loads(json.dumps(original))
                mutate(self.schedule["candidates"][0])
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
        paths = [self.schedule_path, self.picks_path, self.latest_path]
        before = {path: path.read_bytes() for path in paths}
        self.assertEqual(self.run_main(), 0)
        after = {path: path.read_bytes() for path in paths}
        self.assertEqual(before, after)

    def test_rejects_impossible_calendar_date(self):
        self.assertEqual(vig_review_verify.main(["2026-02-30", "--root", str(self.root)]), 2)


if __name__ == "__main__":
    unittest.main()
