import copy
import hashlib
from unittest import mock
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mlb_decision_audit import build_audit, write_snapshot
from mlb_runtime_policy import MlbSelectionPolicy
from mlb_probability_model import main
import vig_policy_state


class DecisionAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.policy = vig_policy_state.loaded_policy(self.state)
        self.now = datetime(2026, 9, 14, 17, tzinfo=timezone.utc)
        self.doc = {
            "date": "2026-09-14",
            "candidates": [],
            "lineup_watchlist": [],
            "slate_denominator": {"games": [{"game_pk": 1}, {"game_pk": 2}]},
            "game_reads": [
                dict(
                    game_pk=1,
                    event_id="a",
                    away="A",
                    home="B",
                    disposition="incomplete_input_data",
                    dk_fair_prob={"away": 0.6, "home": 0.4},
                    raw_probability={"away": 0.6, "home": 0.4},
                    conservative_probability={"away": 0.6, "home": 0.4},
                    polymarket_ask={"away": 0.54, "home": 0.46},
                    net_edge={"away": 0.06, "home": -0.06},
                    uncertainty_haircut=0,
                    model_version="vig-mlb-market-v1",
                    refusing_rails=["real_winner_conviction"],
                    unavailable={"lineups": "not confirmed"},
                )
            ],
        }

    def test_funnel_reports_missing_game_refusal_and_unlinked_recheck_without_mutation(
        self,
    ):
        before = copy.deepcopy(self.doc)
        report = build_audit(self.doc, self.policy, now=self.now, state_dir=self.state)
        self.assertEqual(self.doc, before)
        self.assertEqual(report["missing_game_pks"], ["2"])
        game = report["games"][0]
        self.assertTrue(game["price_qualified_refusal"])
        self.assertTrue(game["incomplete_without_linked_recheck"])
        self.assertEqual(game["model_errors"], [])
        self.assertFalse(report["execution_enabled"])

    def test_research_tracking_does_not_imply_a_pick_or_lineup_recheck(self):
        queue = {"games": {"1": {"identity": [1, "a", "A", "B", "2026-09-14T22:00Z"],
                                  "status": "pending", "next_retry": "2026-09-14T20:00Z"}}}
        report = build_audit(self.doc, self.policy, now=self.now, state_dir=self.state, research=queue)
        game = report["games"][0]
        self.assertTrue(game["incomplete_without_linked_recheck"])
        self.assertFalse(game["incomplete_without_tracked_followup"])
        self.assertEqual(game["research_status"], "pending")
        queue["games"]["1"]["identity"][1] = "wrong event"
        report = build_audit(self.doc, self.policy, now=self.now, state_dir=self.state, research=queue)
        self.assertTrue(report["games"][0]["incomplete_without_tracked_followup"])

    def test_snapshot_hash_matches_input_and_missing_schedule_is_explicit(self):
        root = self.state / "runtime"
        root.mkdir()
        path = write_snapshot(root, "2026-09-14", now=self.now)
        self.assertEqual(json.loads(path.read_text())["status"], "no_schedule")
        source = root / ".picks/execute/2026-09-14-schedule.json"
        source.parent.mkdir(parents=True)
        raw = json.dumps(self.doc).encode()
        source.write_bytes(raw)
        with mock.patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            write_snapshot(root, "2026-09-14", now=self.now)
        self.assertEqual(
            json.loads(path.read_text())["schedule_sha256"],
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(source.read_bytes(), raw)

    def test_gate_writes_audit_on_no_work_and_failure(self):
        import vig_review_gate_common as gate

        for failure in (False, True):
            with self.subTest(failure=failure), mock.patch.object(
                gate, "ROOT", self.state
            ), mock.patch.object(
                gate, "schedule_day_now", return_value="2026-09-14"
            ), mock.patch.object(
                gate, "write_slate_receipt"
            ), mock.patch.object(
                gate, "write_decision_snapshot"
            ) as write, mock.patch.object(
                gate,
                "_run_gate",
                side_effect=ValueError("failed") if failure else None,
                return_value=0,
            ):
                if failure:
                    with self.assertRaises(ValueError):
                        gate.run_gate("MLB")
                else:
                    self.assertEqual(gate.run_gate("MLB"), 0)
                write.assert_called_once_with(self.state, "2026-09-14")

    def test_mislabeled_market_read_is_not_missed_bet_evidence(self):
        self.doc["game_reads"][0]["raw_probability"]["away"] = 0.7
        report = build_audit(self.doc, self.policy, now=self.now, state_dir=self.state)
        self.assertTrue(report["games"][0]["model_errors"])

    def test_unknown_denominator_and_malformed_watchlist_are_loud(self):
        self.doc["slate_denominator"] = None
        report = build_audit(self.doc, self.policy, now=self.now, state_dir=self.state)
        self.assertTrue(report["input_errors"])
        self.doc["lineup_watchlist"] = [None]
        report = build_audit(self.doc, self.policy, now=self.now, state_dir=self.state)
        self.assertTrue(report["input_errors"])
        self.assertEqual(report["games"], [])

    def test_evaluation_requires_one_version_and_counts_excluded_rows(self):
        path = self.state / "dataset.jsonl"
        rows = [dict(model_version=v) for v in ("model-a", "model-b")]
        path.write_text("\n".join(json.dumps(r) for r in rows))
        output = StringIO()
        with redirect_stdout(output):
            code = main(["evaluate", "--dataset", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue())["status"], "model_version_required"
        )
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                ["evaluate", "--dataset", str(path), "--model-version", "model-a"]
            )
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["selected_rows"], 1)
        self.assertEqual(report["excluded_rows"], 1)
        self.assertFalse(report["execution_enabled"])
