"""Changed evening scans through the real writer and postflight receipt."""

import hashlib
import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

import test_mlb_writer_occupancy as occupancy
from test_mlb_writer_occupancy import candidate, watchlist
from test_mlb_slate_writer import DAY, read_for, WriterTestCase
from scripts import mlb_slate_receipt as receipt
from scripts import mlb_slate_writer as writer


class AppendProvenanceTests(WriterTestCase):
    existing = occupancy.OccupancyTests.existing

    def test_changed_evening_scan_lands_and_retry_preserves_bytes(self):
        rows, draft, original, before = self.existing(promoted=True)
        scan = writer.denominator_output_path(DAY, self.root)
        old_digest = hashlib.sha256(scan.read_bytes()).hexdigest()
        # Same roster, genuinely different scan payload and artifact timestamp.
        rows[1]["away_fair"] = 0.410
        self.write_scan(rows)
        os.utime(scan, (1800000000, 1800000000))
        draft["candidates"] = [candidate(rows[1])]
        draft["game_reads"][1] = read_for(rows[1], disposition="candidate", refusing_rails=[])
        draft["lineup_watchlist"] = [watchlist(rows[2])]
        draft["game_reads"][2] = read_for(rows[2], disposition="lineup_watchlist", refusing_rails=[])
        # Attempted fresh rewrite of occupied evidence must be discarded.
        draft["game_reads"][0]["model_version"] = "evening-version"
        draft_path = self.root / "evening.json"
        draft_path.write_text(json.dumps(draft))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(writer.main(["--land", str(draft_path), "--day", DAY,
                                          "--root", str(self.root)]), 0)
        after = self.schedule_path().read_bytes()
        landed = json.loads(after)
        self.assertNotEqual(old_digest, hashlib.sha256(scan.read_bytes()).hexdigest())
        self.assertEqual(landed["slate_denominator"]["fetched_at_utc"], writer.scan_fetched_at(scan))
        self.assertNotEqual(landed["slate_denominator"]["fetched_at_utc"],
                            original["slate_denominator"]["fetched_at_utc"])
        self.assertEqual(landed["game_reads"][0], original["game_reads"][0])
        # Exact lexemes of every retained object survive, not just decoded values.
        for key in ("candidates", "lineup_watchlist", "game_reads"):
            text = before.decode()
            start = text.index("[", text.index('"' + key + '"')) + 1
            while text[start].isspace():
                start += 1
            _, end = json.JSONDecoder().raw_decode(text, start)
            self.assertIn(text[start:end].encode(), after)
        result = receipt.build_receipt(self.root, DAY)
        self.assertEqual(result["verdict"], receipt.VERDICT_COMPLETE, result)
        self.assertEqual(result["writer_provenance"], "corroborated")
        self.assertEqual(result["schedule_sha256"], hashlib.sha256(after).hexdigest())
        self.assertEqual(result["scan_sha256_recorded"], result["scan_sha256_actual"])
        self.assertEqual(result["scheduled_games"], 3)
        self.assertEqual(result["reads_recorded"], 3)
        self.assertEqual(result["read_scan_bindings"], {
            "current_scan": [rows[1]["game_pk"], rows[2]["game_pk"]],
            "earlier_scan": [rows[0]["game_pk"]], "unknown": [],
            "acquisition_freshness": "not_established",
        })
        for _ in range(2):
            with mock.patch.object(writer, "atomic_write") as write:
                writer.land(self.root, DAY, draft)
                write.assert_not_called()
            self.assertEqual(self.schedule_path().read_bytes(), after)
            self.assertEqual(receipt.build_receipt(self.root, DAY)["read_scan_bindings"],
                             result["read_scan_bindings"])

    def test_legacy_or_modified_retained_binding_is_not_fresh(self):
        for mode in ("missing", "modified", "malformed"):
            with self.subTest(mode=mode):
                self.schedule_path().unlink(missing_ok=True)
                rows, draft, existing, _ = self.existing()
                if mode == "missing":
                    existing["slate_denominator"].pop("read_bindings")
                elif mode == "modified":
                    existing["game_reads"][0]["model_version"] = "downstream-change"
                else:
                    existing["slate_denominator"]["read_bindings"] = []
                self.schedule_path().write_text(json.dumps(existing))
                rows[1]["away_fair"] = 0.41
                self.write_scan(rows)
                writer.land(self.root, DAY, draft)
                result = receipt.build_receipt(self.root, DAY)
                self.assertEqual(result["writer_provenance"], "corroborated")
                self.assertEqual(result["read_scan_bindings"]["unknown"], [rows[0]["game_pk"]])
                self.assertEqual(result["read_scan_bindings"]["earlier_scan"], [])

    def test_changed_identity_refuses_without_rebinding(self):
        rows, draft, _, before = self.existing()
        rows[0]["event_id"] = "different-event"
        self.write_scan(rows)
        draft["game_reads"][0]["event_id"] = "different-event"
        draft["candidates"][0]["event_id"] = "different-event"
        with self.assertRaises(writer.SlateWriteError):
            writer.land(self.root, DAY, draft)
        self.assertEqual(self.schedule_path().read_bytes(), before)

    def test_verified_coverage_sidecar_append_and_stale_sidecar_refusal(self):
        from test_mlb_data_completeness import row, NOW
        from scripts import mlb_data_completeness as data
        from test_mlb_slate_writer import draft_for

        day = "2026-09-09"
        source = row()
        path = writer.denominator_output_path(day, self.root)
        draft = draft_for([source], date=day)
        with mock.patch("mlb_data_completeness.utc_now", return_value=NOW):
            for version in range(2):
                source["away_offense"]["woba"] = .30 + version * .01
                path.write_text(json.dumps([source]))
                if version:
                    before = writer.schedule_path_for(self.root, day).read_bytes()
                    with self.assertRaisesRegex(writer.SlateWriteError, "cannot reconcile"):
                        writer.land(self.root, day, draft)
                    self.assertEqual(writer.schedule_path_for(self.root, day).read_bytes(), before)
                coverage = data.coverage([source], NOW, scheduled_games=1, schedule_verified=True)
                coverage["scan_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                path.with_suffix(".coverage.json").write_text(json.dumps(coverage))
                writer.land(self.root, day, draft)
                result = receipt.build_receipt(self.root, day)
                self.assertEqual(result["verdict"], receipt.VERDICT_COMPLETE, result)
                self.assertTrue(result["data_coverage"]["reconciled"])
                self.assertEqual(result["writer_provenance"], "corroborated")
                self.assertEqual(result["read_scan_bindings"]["acquisition_freshness"], "not_established")
