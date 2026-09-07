"""Synthetic integrity tests; no fixture is admitted as historical evidence."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import mlb_historical_admission as admission


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'blobs').mkdir()
        self.raw = b'{"synthetic":true}'
        self.digest = admission.sha(self.raw)
        (self.root / 'blobs' / self.digest).write_bytes(self.raw)
        self.manifest = {'errors': [], 'skipped': [], 'unique_digests': 1,
                         'roots': {'fixture': {'files': 2, 'bytes': len(self.raw)*2}},
                         'files': [{'path': f'fixture/{n}', 'sha256': self.digest,
                                    'bytes': len(self.raw), 'root': 'fixture'} for n in ('a', 'b')]}

    def verify(self):
        raw = admission.canonical(self.manifest)
        (self.root / 'manifest.json').write_bytes(raw)
        return admission.verify_bundle(self.root, admission.sha(raw))

    def test_duplicate_content_is_not_independent_evidence(self):
        _, paths, blobs = self.verify()
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(blobs), 1)

    def test_tampering_and_missing_blob_fail(self):
        blob = self.root / 'blobs' / self.digest
        blob.write_bytes(b'x' * len(self.raw))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.verify()
        blob.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.verify()

    def test_symlink_refused(self):
        target = self.root / 'target'
        target.write_bytes(self.raw)
        blob = self.root / 'blobs' / self.digest
        blob.unlink()
        blob.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.verify()

    def test_manifest_pin(self):
        self.verify()
        with self.assertRaisesRegex(ValueError, 'manifest digest'):
            admission.verify_bundle(self.root, '0' * 64)

    def test_inventory_defects(self):
        original = copy.deepcopy(self.manifest)
        for mutate in (
            lambda m: m['files'][0].update(path='../outside'),
            lambda m: m['files'][0].update(path='fixture/b'),
            lambda m: m['files'][0].update(sha256='../outside'),
            lambda m: m['files'][0].update(bytes=True),
            lambda m: m.update(unique_digests=2),
            lambda m: m['errors'].append('read failure'),
            lambda m: m['skipped'].append('symlink'),
            lambda m: m['roots']['fixture'].update(files=1),
        ):
            self.manifest = copy.deepcopy(original)
            mutate(self.manifest)
            with self.subTest(manifest=self.manifest), self.assertRaises(ValueError):
                self.verify()

    def test_nonjson_and_duplicate_keys_refused(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                admission.decode(raw)

    def test_pinned_checkpoint_cannot_use_synthetic_bundle(self):
        self.verify()
        rules = self.root / 'rules'
        rules.write_text('synthetic rules')
        result = subprocess.run([sys.executable, str(Path(admission.__file__)), '--bundle', str(self.root),
                                 '--recovery-rules', str(rules)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)['feasible'])
        self.assertFalse(json.loads(result.stdout)['scoring_enabled'])

    def test_scoring_switch_is_refused(self):
        contract = json.loads(admission.CONTRACT_PATH.read_text())
        contract['scoring_enabled'] = True
        with patch.object(admission, 'read_regular', return_value=admission.canonical(contract)):
            with self.assertRaisesRegex(ValueError, 'scoring cannot'):
                admission.checkpoint(self.root, self.root / 'rules')

    def test_source_id_does_not_conflate_strings_and_numbers(self):
        for value in (True, '123', ' 123 ', 0, -1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                admission.source_id(value)
        self.assertEqual(admission.source_id(123), '123')

    def test_archive_prices_and_missingness(self):
        for value in ('+100', '-187', 100, -187):
            self.assertTrue(admission.raw_american_price(value))
        for value in (None, True, '+099', '100', ' +100', 'NaN', float('inf'), 99):
            self.assertFalse(admission.raw_american_price(value))

    def test_split_leakage_and_ambiguous_timestamps(self):
        split = json.loads(admission.CONTRACT_PATH.read_text())['split']
        admission.validate_split(split)
        for key, value in [('training_end', '2026-09-02'), ('heldout_start', '2026-08-30'),
                           ('training_cutoff', '2026-08-31T00:00:00'), ('heldout_end', '2027-01-01'),
                           ('training_start', '2026-8-1'), ('training_cutoff', '2026-08-31T00:00:00+13:00')]:
            altered = dict(split, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                admission.validate_split(altered)
        with self.assertRaises(ValueError):
            admission.stamp('2026-09-03T03:2x')

    def test_recursive_receipt_census(self):
        self.assertEqual(admission.field_census({'nested': [{'win_probability': .6}, {'model_version': 'x'}]}),
                         {'win_probability': 1, 'model_version': 1})


if __name__ == '__main__':
    unittest.main()
