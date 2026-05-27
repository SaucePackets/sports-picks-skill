import json
import tempfile
import unittest
from pathlib import Path

from scripts.execution_guard import (
    acquire_execution_lock,
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


if __name__ == "__main__":
    unittest.main()
