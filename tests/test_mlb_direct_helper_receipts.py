import hashlib
import json
import unittest

from scripts import mlb_slate_writer as writer
from test_mlb_slate_writer import DAY, WriterTestCase, draft_for, scan_row


class DirectHelperReceiptTests(WriterTestCase):
    """The supported direct API requires the Stage 2 receipt contract."""

    def write_receipt(self, *, nonce="direct-nonce", date=DAY, digest=None):
        scan = writer.denominator_output_path(DAY, self.root)
        path = writer.scan_receipt_path(DAY, self.root)
        path.write_text(json.dumps({
            "schema": "mlb-stage2-run-v1",
            "date": date,
            "run_nonce": nonce,
            "scan_sha256": digest or hashlib.sha256(scan.read_bytes()).hexdigest(),
        }))
        return nonce

    def test_land_rejects_missing_receipt_nonce(self):
        self.write_scan([scan_row(823509)])
        with self.assertRaisesRegex(writer.SlateWriteError, "nonce is required"):
            writer.land(self.root, DAY, draft_for([scan_row(823509)]))

    def test_land_rejects_wrong_nonce(self):
        self.write_scan([scan_row(823509)])
        self.write_receipt(nonce="other")
        with self.assertRaisesRegex(writer.SlateWriteError, "does not match"):
            writer.land(self.root, DAY, draft_for([scan_row(823509)]), run_nonce="expected")

    def test_skeleton_rejects_wrong_receipt_date(self):
        self.write_scan([scan_row(823509)])
        self.write_receipt(date="2026-09-02")
        with self.assertRaisesRegex(writer.SlateWriteError, "does not match"):
            writer.skeleton(self.root, DAY, run_nonce="direct-nonce")

    def test_land_rejects_mutated_scan_artifact(self):
        self.write_scan([scan_row(823509)])
        nonce = self.write_receipt()
        scan = writer.denominator_output_path(DAY, self.root)
        scan.write_text(scan.read_text() + "\n")
        with self.assertRaisesRegex(writer.SlateWriteError, "current scan artifact"):
            writer.land(self.root, DAY, draft_for([scan_row(823509)]), run_nonce=nonce)

    def test_land_accepts_valid_receipt(self):
        rows = [scan_row(823509)]
        self.write_scan(rows)
        nonce = self.write_receipt()
        path, _schedule = writer.land(self.root, DAY, draft_for(rows), run_nonce=nonce)
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
