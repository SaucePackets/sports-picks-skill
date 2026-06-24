import json
import tempfile
import unittest
from pathlib import Path

from scripts.execution_guard import (
    acquire_execution_lock,
    active_pick_exists,
    append_pick_with_dedup,
    find_filled_receipts,
    mark_execution_from_receipts,
)


class ExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.receipts = self.root / ".picks" / "receipts" / "polymarket"
        self.receipts.mkdir(parents=True)
        self.schedule_path = self.root / ".picks" / "execute" / "2026-05-27-schedule.json"
        self.schedule_path.parent.mkdir(parents=True)
        self.schedule_path.write_text(json.dumps({
            "date": "2026-05-27",
            "candidates": [{
                "polymarket_slug": "aec-mlb-nyy-kc-2026-05-27",
                "pick_side": "New York Yankees",
                "unit_size": 15,
                "executed": False,
                "skipped": False,
                "execution_lock": None,
            }],
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def write_filled_receipt(self, name="20260527-213203-sdk-order-aec-mlb-nyy-kc-2026-05-27.json"):
        path = self.receipts / name
        path.write_text(json.dumps({
            "market_slug": "aec-mlb-nyy-kc-2026-05-27",
            "response": {
                "executions": [{
                    "type": "EXECUTION_TYPE_FILL",
                    "lastPx": {"value": "0.590"},
                    "lastShares": "25.000",
                    "tradeId": "TRADE1",
                    "order": {
                        "id": "ORDER1",
                        "state": "ORDER_STATE_FILLED",
                        "marketSlug": "aec-mlb-nyy-kc-2026-05-27",
                        "cumQuantity": 25,
                        "price": {"value": "0.59"},
                    },
                    "commissionNotionalCollected": {"value": "0.300"},
                }],
            },
        }))
        return path

    def test_existing_filled_receipt_blocks_new_order(self):
        self.write_filled_receipt()

        receipts = find_filled_receipts(self.receipts, "aec-mlb-nyy-kc-2026-05-27")

        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["order_id"], "ORDER1")
        self.assertEqual(receipts[0]["trade_id"], "TRADE1")
        self.assertEqual(receipts[0]["fill_quantity"], 25)
        self.assertEqual(receipts[0]["fill_notional"], 14.75)

    def test_mark_execution_from_receipts_sets_executed_before_retry(self):
        self.write_filled_receipt()

        changed = mark_execution_from_receipts(
            self.schedule_path,
            "aec-mlb-nyy-kc-2026-05-27",
            self.receipts,
            note="dedup regression",
        )

        self.assertTrue(changed)
        schedule = json.loads(self.schedule_path.read_text())
        candidate = schedule["candidates"][0]
        self.assertTrue(candidate["executed"])
        self.assertEqual(candidate["fill_quantity"], 25)
        self.assertEqual(candidate["fill_notional"], 14.75)
        self.assertEqual(candidate["polymarket_order_id"], "ORDER1")
        self.assertIn("dedup regression", candidate["execution_note"])

    def test_sell_receipt_does_not_count_as_existing_buy_execution(self):
        path = self.receipts / "20260527-215509-sdk-order-aec-mlb-nyy-kc-2026-05-27.json"
        path.write_text(json.dumps({
            "market_slug": "aec-mlb-nyy-kc-2026-05-27",
            "response": {
                "executions": [{
                    "type": "EXECUTION_TYPE_FILL",
                    "lastPx": {"value": "0.580"},
                    "lastShares": "25.000",
                    "tradeId": "SELLTRADE",
                    "order": {
                        "id": "SELLORDER",
                        "action": "ORDER_ACTION_SELL",
                        "intent": "ORDER_INTENT_SELL_LONG",
                        "state": "ORDER_STATE_FILLED",
                        "marketSlug": "aec-mlb-nyy-kc-2026-05-27",
                        "cumQuantity": 25,
                        "price": {"value": "0.58"},
                    },
                    "commissionNotionalCollected": {"value": "0.300"},
                }],
            },
        }))

        receipts = find_filled_receipts(self.receipts, "aec-mlb-nyy-kc-2026-05-27")

        self.assertEqual(receipts, [])

    def test_acquire_lock_refuses_when_candidate_already_locked(self):
        self.assertTrue(acquire_execution_lock(self.schedule_path, "aec-mlb-nyy-kc-2026-05-27", "attempt-1"))
        self.assertFalse(acquire_execution_lock(self.schedule_path, "aec-mlb-nyy-kc-2026-05-27", "attempt-2"))

    def test_active_pick_exists_ignores_settled_rows(self):
        picks_path = self.root / "picks.json"
        picks_path.write_text(json.dumps({"picks": [
            {"market_slug": "abc", "status": "settled"},
            {"market_slug": "def", "status": "open"},
        ]}))

        self.assertFalse(active_pick_exists(picks_path, "abc"))
        self.assertTrue(active_pick_exists(picks_path, "def"))

    def test_append_pick_with_dedup_merges_nearby_duplicate_fill(self):
        picks_path = self.root / "picks.json"
        picks_path.write_text(json.dumps({"picks": [{
            "pick_id": "P1",
            "market_slug": "aec-mlb-nyy-kc-2026-05-27",
            "execution_timestamp": "2026-05-27T21:32:03Z",
            "fill_shares": 25,
            "entry_notional": 14.75,
            "duplicate_count": 1,
        }]}))

        result = append_pick_with_dedup(picks_path, {
            "pick_id": "P2",
            "market_slug": "aec-mlb-nyy-kc-2026-05-27",
            "execution_timestamp": "2026-05-27T21:32:54Z",
            "fill_shares": 10,
            "entry_notional": 5.90,
        })

        self.assertEqual(result["action"], "merged")
        data = json.loads(picks_path.read_text())
        self.assertEqual(len(data["picks"]), 1)
        pick = data["picks"][0]
        self.assertEqual(pick["fill_shares"], 35)
        self.assertEqual(pick["entry_notional"], 20.65)
        self.assertEqual(pick["duplicate_count"], 2)
        self.assertTrue(pick["duplicate_batch"])
        self.assertEqual(pick["duplicate_pick_ids"], ["P2"])

    def test_append_pick_with_dedup_appends_outside_window(self):
        picks_path = self.root / "picks.json"
        picks_path.write_text(json.dumps({"picks": [{
            "pick_id": "P1",
            "market_slug": "aec-mlb-nyy-kc-2026-05-27",
            "execution_timestamp": "2026-05-27T21:32:03Z",
            "fill_shares": 25,
            "entry_notional": 14.75,
        }]}))

        result = append_pick_with_dedup(picks_path, {
            "pick_id": "P2",
            "market_slug": "aec-mlb-nyy-kc-2026-05-27",
            "execution_timestamp": "2026-05-27T21:34:04Z",
            "fill_shares": 10,
            "entry_notional": 5.90,
        })

        self.assertEqual(result["action"], "appended")
        data = json.loads(picks_path.read_text())
        self.assertEqual(len(data["picks"]), 2)


if __name__ == "__main__":
    unittest.main()
