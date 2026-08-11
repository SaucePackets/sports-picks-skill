import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts.execution_guard import (
    _risk_limit_violation,
    acquire_execution_lock,
    active_pick_exists,
    append_pick_with_dedup,
    find_filled_receipts,
    mark_execution_from_receipts,
    main,
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
            "sport": "MLB",
            "market_type": "moneyline",
            "candidates": [{
                "polymarket_slug": "aec-mlb-nyy-kc-2026-05-27",
                "pick_side": "New York Yankees",
                "unit_size": 15,
                "max_polymarket_price": 0.60,
                "sport": "MLB",
                "market_type": "moneyline",
                "first_pitch_utc": "2026-05-27T23:00:00Z",
                "vig_approved": True,
                "execution_mode": "standing_authorized",
                "execution_status": "pending",
                "executed": False,
                "skipped": False,
                "execution_lock": None,
                "dk_fair_prob": 0.62,
                "raw_probability": 0.66,
                "uncertainty_haircut": 0.01,
                "conservative_probability": 0.65,
                "current_ask": 0.59,
                "projected_edge_at_current_ask": 0.06,
                "model_version": "test-model-v1",
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

    def test_standing_authorized_lock_revalidates_hold_and_first_pitch(self):
        candidate = json.loads(self.schedule_path.read_text())["candidates"][0]
        candidate["held"] = True
        self.schedule_path.write_text(json.dumps({"date": "2026-05-27", "sport": "MLB", "market_type": "moneyline", "candidates": [candidate]}))
        now = datetime(2026, 5, 27, 21, 0, tzinfo=timezone.utc)

        self.assertFalse(
            acquire_execution_lock(
                self.schedule_path,
                "aec-mlb-nyy-kc-2026-05-27",
                "attempt-held",
                require_standing_authorized=True,
                now=now,
            )
        )

        candidate["held"] = False
        candidate["first_pitch_utc"] = "2026-05-27T20:00:00Z"
        self.schedule_path.write_text(json.dumps({"date": "2026-05-27", "sport": "MLB", "market_type": "moneyline", "candidates": [candidate]}))
        self.assertFalse(
            acquire_execution_lock(
                self.schedule_path,
                "aec-mlb-nyy-kc-2026-05-27",
                "attempt-started",
                require_standing_authorized=True,
                now=now,
            )
        )

    def test_check_cli_blocks_active_canonical_pick(self):
        picks_path = self.root / "picks.json"
        picks_path.write_text(
            json.dumps(
                {
                    "picks": [
                        {
                            "market_slug": "aec-mlb-nyy-kc-2026-05-27",
                            "status": "active",
                        }
                    ]
                }
            )
        )
        output = StringIO()
        argv = [
            "execution_guard.py",
            "check",
            "--schedule",
            str(self.schedule_path),
            "--market-slug",
            "aec-mlb-nyy-kc-2026-05-27",
            "--receipts-dir",
            str(self.receipts),
            "--picks-file",
            str(picks_path),
        ]

        with patch("sys.argv", argv), redirect_stdout(output):
            status = main()

        self.assertEqual(status, 2)
        self.assertTrue(json.loads(output.getvalue())["has_active_pick"])

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


class SmallStakeTierRiskLimitTests(unittest.TestCase):
    """Deterministic money rails for the small-stake tier (below-Medium +EV picks)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.limits_path = self.root / "risk_limits.json"
        self.limits_path.write_text(json.dumps({
            "daily_cap_usd": 90,
            "max_unit_usd": {"high": 25, "medium": 15, "elite": 25, "small": 9},
            "max_unit_usd_absolute": 30,
            "max_small_bets_per_day": 3,
            "max_polymarket_price": 0.75,
        }))
        self.picks_path = self.root / "picks.json"
        self.picks_path.write_text(json.dumps({"picks": []}))
        self.now = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)
        self.patcher = patch("scripts.execution_guard.RISK_LIMITS_PATH", self.limits_path)
        self.patcher.start()
        # Isolate the shared MLB selection policy: these tests exercise the base
        # money rails, so no policy block means no probation tightening.
        self.env_patcher = patch.dict(
            "os.environ", {"VIG_STATE_DIR": str(self.root / "no-policy-state")}
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        self.patcher.stop()
        self.tmp.cleanup()

    def cand(self, **kw):
        base = {"unit_size": 9, "confidence": "small", "max_polymarket_price": 0.55}
        base.update(kw)
        return base

    def set_picks(self, picks):
        self.picks_path.write_text(json.dumps({"picks": picks}))

    def test_small_tier_within_cap_ok(self):
        self.assertIsNone(_risk_limit_violation(self.cand(unit_size=9), self.picks_path, self.now))

    def test_small_tier_over_cap_blocked(self):
        v = _risk_limit_violation(self.cand(unit_size=15), self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("small cap", v)

    def test_medium_tier_unaffected_by_small_cap(self):
        # A real $18 Medium bet must still pass — only the absolute $30 cap applies
        # to non-small tiers, so adding max_unit_usd.medium=15 must NOT block it.
        self.assertIsNone(
            _risk_limit_violation(self.cand(confidence="medium", unit_size=18), self.picks_path, self.now)
        )

    def test_small_tier_daily_count_cap(self):
        today = "2026-08-04T18:00:00Z"
        self.set_picks([
            {"execution_timestamp": today, "confidence": "small", "unit_size": 9, "entry_notional": 9},
            {"execution_timestamp": today, "confidence": "small", "unit_size": 9, "entry_notional": 9},
            {"execution_timestamp": today, "confidence": "small", "unit_size": 9, "entry_notional": 9},
        ])
        v = _risk_limit_violation(self.cand(), self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("small-tier daily count breach", v)

    def test_small_count_cap_ignores_other_tiers_and_prior_days(self):
        self.set_picks([
            {"execution_timestamp": "2026-08-04T18:00:00Z", "confidence": "medium", "unit_size": 18, "entry_notional": 18},
            {"execution_timestamp": "2026-08-03T18:00:00Z", "confidence": "small", "unit_size": 9, "entry_notional": 9},
            {"execution_timestamp": "2026-08-04T18:00:00Z", "confidence": "small", "unit_size": 9, "entry_notional": 9},
        ])
        # only ONE small bet counts toward today -> a second small is still allowed
        self.assertIsNone(_risk_limit_violation(self.cand(), self.picks_path, self.now))

    def test_daily_dollar_cap_still_applies_to_small(self):
        self.set_picks([
            {"execution_timestamp": "2026-08-04T18:00:00Z", "confidence": "medium", "unit_size": 18, "entry_notional": 85},
        ])
        v = _risk_limit_violation(self.cand(unit_size=9), self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("daily cap breach", v)

    def test_probation_policy_tightens_small_daily_count_to_one(self):
        # With the shared MLB selection policy present, at most ONE Small bet per
        # day during probation — tighter than the base max_small_bets_per_day=3.
        state = Path(self.tmp.name) / "policy-state"
        state.mkdir()
        (state / "risk_limits.json").write_text(json.dumps({
            "mlb_selection_policy": {
                "schema": "vig-mlb-selection-policy-v1",
                "policy_version": "test",
                "effective_at": "2026-08-11T00:00:00Z",
                "min_conservative_edge": 0.05,
                "max_mlb_official_bets_per_day": 2,
                "starter_pending_promotions_enabled": False,
                "max_small_bets_per_day_probation": 1,
            }
        }))
        self.set_picks([
            {"execution_timestamp": "2026-08-04T18:00:00Z", "confidence": "small", "unit_size": 9, "entry_notional": 9},
        ])
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(state)}):
            # One small already today: a second is refused under probation…
            v = _risk_limit_violation(self.cand(), self.picks_path, self.now)
            self.assertIsNotNone(v)
            self.assertIn("small-tier daily count breach", v or "")
            # …while the FIRST small of the day is still allowed.
            self.set_picks([])
            self.assertIsNone(_risk_limit_violation(self.cand(), self.picks_path, self.now))


class FinalLockPolicyIntegrationTests(unittest.TestCase):
    """PR-1 review integration coverage: the final lock must fail closed on
    missing/invalid policy, missing/stale probability trail, and a third
    official MLB bet — using the canonical DEPLOYED policy JSON shape."""

    DEPLOYED_POLICY_BLOCK = {
        # Mirrors /home/clawdbot/.hermes/vig/state/risk_limits.json exactly:
        # deployed key names, not the reviewed key names.
        "mlb_policy": {
            "schema": "vig-mlb-selection-policy-v1",
            "policy_version": "2026-08-11-hardening-pr1",
            "policy_effective_at": "2026-08-11T00:00:00Z",
            "min_conservative_edge": 0.05,
            "max_mlb_official_bets_per_day": 2,
            "starter_pending_promotions_enabled": False,
            "max_small_bets_per_day_during_probation": 1,
        }
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.limits_path = self.root / "risk_limits.json"
        self.limits_path.write_text(json.dumps({
            "daily_cap_usd": 90,
            "max_unit_usd_absolute": 30,
            "max_polymarket_price": 0.75,
        }))
        self.picks_path = self.root / "picks.json"
        self.picks_path.write_text(json.dumps({"picks": []}))
        self.now = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)
        self.limits_patcher = patch(
            "scripts.execution_guard.RISK_LIMITS_PATH", self.limits_path
        )
        self.limits_patcher.start()
        self.state = self.root / "state"
        self.state.mkdir()

    def tearDown(self):
        self.limits_patcher.stop()
        self.tmp.cleanup()

    def _write_policy(self, block):
        (self.state / "risk_limits.json").write_text(json.dumps(block))

    def _candidate(self, **kw):
        base = {
            "unit_size": 15,
            "confidence": "medium",
            "max_polymarket_price": 0.60,
            "execution_mode": "standing_authorized",
            "dk_fair_prob": 0.62,
            "raw_probability": 0.66,
            "uncertainty_haircut": 0.01,
            "conservative_probability": 0.65,
            "current_ask": 0.59,
            "projected_edge_at_current_ask": 0.06,
            "model_version": "test-model-v1",
        }
        base.update(kw)
        return base

    def test_deployed_policy_shape_loads_and_allows_valid_candidate(self):
        self._write_policy(self.DEPLOYED_POLICY_BLOCK)
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            self.assertIsNone(
                _risk_limit_violation(self._candidate(), self.picks_path, self.now)
            )

    def test_missing_policy_fails_closed(self):
        # No policy block at all: standing-authorized lock must refuse, never
        # fall back to legacy rails.
        self._write_policy({})
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            v = _risk_limit_violation(self._candidate(), self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("policy missing or invalid", v)

    def test_missing_probability_trail_refused(self):
        self._write_policy(self.DEPLOYED_POLICY_BLOCK)
        candidate = self._candidate()
        for field in (
            "dk_fair_prob", "raw_probability", "uncertainty_haircut",
            "conservative_probability", "current_ask",
            "projected_edge_at_current_ask", "model_version",
        ):
            candidate.pop(field)
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            v = _risk_limit_violation(candidate, self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("probability contract violation", v)

    def test_stale_stored_edge_refused(self):
        self._write_policy(self.DEPLOYED_POLICY_BLOCK)
        # Stored edge claims +0.06 but live recomputation gives -0.06.
        candidate = self._candidate(
            conservative_probability=0.54,
            current_ask=0.60,
            projected_edge_at_current_ask=0.06,
        )
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            v = _risk_limit_violation(candidate, self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("stale", v)

    def test_live_edge_below_floor_refused(self):
        self._write_policy(self.DEPLOYED_POLICY_BLOCK)
        # Consistent stored/live edge of 0.03 < 0.05 floor.
        candidate = self._candidate(
            conservative_probability=0.56,
            current_ask=0.53,
            projected_edge_at_current_ask=0.03,
        )
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            v = _risk_limit_violation(candidate, self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("below policy floor", v)

    def test_third_official_mlb_bet_refused(self):
        self._write_policy(self.DEPLOYED_POLICY_BLOCK)
        self.set_picks_two_official_mlb_today()
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            v = _risk_limit_violation(self._candidate(), self.picks_path, self.now)
        self.assertIsNotNone(v)
        self.assertIn("official MLB daily count breach", v)

    def set_picks_two_official_mlb_today(self):
        self.picks_path.write_text(json.dumps({"picks": [
            {"execution_timestamp": "2026-08-11T18:00:00Z", "sport": "MLB",
             "confidence": "medium", "unit_size": 18, "entry_notional": 18},
            {"execution_timestamp": "2026-08-11T19:00:00Z", "sport": "MLB",
             "confidence": "medium", "unit_size": 15, "entry_notional": 15},
        ]}))

    def test_two_official_mlb_with_edge_below_floor_still_refused(self):
        # Combined adversarial case from the review: missing fields AND a third
        # bet AND negative live edge — any one alone must refuse.
        self._write_policy(self.DEPLOYED_POLICY_BLOCK)
        self.set_picks_two_official_mlb_today()
        candidate = self._candidate(
            conservative_probability=0.54,
            current_ask=0.60,
            projected_edge_at_current_ask=0.06,
        )
        with patch.dict("os.environ", {"VIG_STATE_DIR": str(self.state)}):
            v = _risk_limit_violation(candidate, self.picks_path, self.now)
        self.assertIsNotNone(v)


if __name__ == "__main__":
    unittest.main()