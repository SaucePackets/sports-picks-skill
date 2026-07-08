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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".picks" / "execute").mkdir(parents=True)
        (self.root / ".picks" / "execute" / "2026-07-08-schedule.json").write_text(json.dumps({
            "date": "2026-07-08",
            "candidates": [{
                "polymarket_slug": "aec-mlb-abc-def-2026-07-08",
                "vig_review_needed": True,
                "vig_approved": True,
                "one_shot_cron_id": "abc123cron",
            }],
        }))
        (self.root / ".picks" / "latest-action.md").write_text("# Latest action\n\n2026-07-08 approved ABC/DEF.\n")
        (self.root / ".picks" / "picks.json").write_text(json.dumps({"picks": [
            {"market_slug": "abc", "status": "settled"},
            {"market_slug": "abc", "status": "open"},
            {"market_slug": "def", "status": "watch"},
        ]}))
        self.cron_list = self.root / "cron-list.txt"
        self.cron_list.write_text("abc123cron [active]\nName: Execute ABC/DEF\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *extra):
        return vig_review_verify.main([
            "2026-07-08",
            "--root", str(self.root),
            "--cron-list-file", str(self.cron_list),
            *extra,
        ])

    def test_valid_runtime_state_exits_zero(self):
        self.assertEqual(self.run_main(), 0)

    def test_missing_vig_approval_fails(self):
        schedule = self.root / ".picks" / "execute" / "2026-07-08-schedule.json"
        data = json.loads(schedule.read_text())
        data["candidates"][0]["vig_approved"] = None
        schedule.write_text(json.dumps(data))

        self.assertEqual(self.run_main(), 1)

    def test_missing_cron_id_fails(self):
        self.cron_list.write_text("different-id [active]\n")

        self.assertEqual(self.run_main(), 1)

    def test_duplicate_active_market_slug_fails_but_settled_duplicate_is_ignored(self):
        (self.root / ".picks" / "picks.json").write_text(json.dumps({"picks": [
            {"market_slug": "abc", "status": "settled"},
            {"market_slug": "abc", "status": "open"},
            {"market_slug": "abc", "status": "watch"},
        ]}))

        self.assertEqual(self.run_main(), 1)

    def test_read_only_does_not_mutate_files(self):
        paths = [
            self.root / ".picks" / "execute" / "2026-07-08-schedule.json",
            self.root / ".picks" / "latest-action.md",
            self.root / ".picks" / "picks.json",
        ]
        before = {path: path.read_text() for path in paths}

        self.assertEqual(self.run_main(), 0)

        after = {path: path.read_text() for path in paths}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
