"""Synthetic verifier boundary tests; the real retained replay is a separate check."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mlb_chronological_checkpoint as m


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'docs').mkdir()
        (self.root / 'scripts').mkdir()
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        (self.bundle / 'manifest.json').write_bytes(b'retained manifest')
        self.contract = json.loads((m.ROOT / 'docs/mlb-chronological-contract.json').read_bytes())
        (self.root / 'docs/mlb-chronological-contract.json').write_bytes(m.encoded(self.contract))
        for name in ('mlb_market_free_checkpoint.py', 'mlb_real_shadow_capture.py'):
            (self.root / 'scripts' / name).write_bytes(name.encode())
        self.report = {'checkpoint_complete': True, 'training': [1, 2],
                       'training_refusals': [3], 'predictions': [4],
                       'schedule_counts': {'2025-04-21': 8, '2025-05-01': 11}}
        body = m.encoded(self.report)
        self.meta = {'manifest_sha256': m.replay.sha(b'retained manifest'),
                     'implementation_sha256': m.replay.sha(b'mlb_market_free_checkpoint.py'),
                     'source_adapter_sha256': m.replay.sha(b'mlb_real_shadow_capture.py'),
                     'report_bytes': len(body), 'report_sha256': m.replay.sha(body)}
        self.write_meta()
        self.root_patch = patch.object(m, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def write_meta(self):
        (self.root / 'docs/mlb-market-free-checkpoint.json').write_bytes(m.encoded(self.meta))

    def test_verified_replay_still_blocks_longer_evaluation(self):
        with patch.object(m.replay, 'checkpoint', return_value=self.report) as run:
            result = m.checkpoint(self.bundle)
        run.assert_called_once_with(self.bundle)
        self.assertTrue(result['retained_replay_verified'])
        for field in ('longer_evaluation_admitted', 'fitting_enabled', 'scoring_enabled', 'eligible'):
            self.assertIs(result[field], False)
        self.assertIsNone(result['historical_performance'])
        self.assertEqual(len(result['missing_schedule_dates']['test']), 30)
        self.assertNotIn('2025-05-01', result['missing_schedule_dates']['validation'])
        self.assertIn('2025-05-31', result['missing_schedule_dates']['validation'])

    def test_wrong_manifest_stops_before_reconstruction(self):
        (self.bundle / 'manifest.json').write_bytes(b'new manifest')
        with patch.object(m.replay, 'checkpoint') as run:
            with self.assertRaisesRegex(ValueError, 'retained_manifest_mismatch'):
                m.checkpoint(self.bundle)
        run.assert_not_called()

    def test_each_changed_adapter_stops_before_reconstruction(self):
        for filename in ('mlb_market_free_checkpoint.py', 'mlb_real_shadow_capture.py'):
            with self.subTest(filename=filename):
                path = self.root / 'scripts' / filename
                original = path.read_bytes()
                path.write_bytes(b'changed implementation')
                with patch.object(m.replay, 'checkpoint') as run:
                    with self.assertRaisesRegex(ValueError, 'replay_implementation_mismatch'):
                        m.checkpoint(self.bundle)
                run.assert_not_called()
                path.write_bytes(original)

    def test_output_mismatch_is_not_accepted_from_matching_manifest(self):
        altered = copy.deepcopy(self.report)
        altered['training'] = [9, 8]  # Same serialized length, different contents.
        with patch.object(m.replay, 'checkpoint', return_value=altered):
            with self.assertRaisesRegex(ValueError, 'retained_report_mismatch'):
                m.checkpoint(self.bundle)

    def test_pinned_size_is_checked_separately(self):
        self.meta['report_bytes'] += 1
        self.write_meta()
        with patch.object(m.replay, 'checkpoint', return_value=self.report):
            with self.assertRaisesRegex(ValueError, 'retained_report_mismatch'):
                m.checkpoint(self.bundle)

    def test_raw_integrity_error_propagates(self):
        with patch.object(m.replay, 'checkpoint', side_effect=ValueError('source_integrity')):
            with self.assertRaisesRegex(ValueError, 'source_integrity'):
                m.checkpoint(self.bundle)

    def test_supplied_dates_cannot_turn_gate_on(self):
        for bounds in self.contract['splits'].values():
            self.report['schedule_counts'].update(dict.fromkeys(m.dates(bounds), 0))
        body = m.encoded(self.report)
        self.meta.update(report_bytes=len(body), report_sha256=m.replay.sha(body))
        self.write_meta()
        with patch.object(m.replay, 'checkpoint', return_value=self.report):
            result = m.checkpoint(self.bundle)
        self.assertTrue(all(not v for v in result['missing_schedule_dates'].values()))
        self.assertFalse(result['longer_evaluation_admitted'])
        self.assertFalse(result['scoring_enabled'])

    def test_cli_missing_bundle_is_integrity_failure(self):
        with patch.object(sys, 'argv', ['checkpoint', '--bundle', str(self.root / 'absent')]), \
                patch('builtins.print') as output:
            self.assertEqual(m.main(), 2)
        result = json.loads(output.call_args.args[0])
        self.assertFalse(result['retained_replay_verified'])
        self.assertFalse(result['fitting_enabled'])

    def test_cli_verified_checkpoint_exits_blocked(self):
        with patch.object(sys, 'argv', ['checkpoint', '--bundle', str(self.bundle)]), \
                patch.object(m.replay, 'checkpoint', return_value=self.report), \
                patch('builtins.print') as output:
            self.assertEqual(m.main(), 1)
        self.assertTrue(json.loads(output.call_args.args[0])['retained_replay_verified'])


if __name__ == '__main__':
    unittest.main()
