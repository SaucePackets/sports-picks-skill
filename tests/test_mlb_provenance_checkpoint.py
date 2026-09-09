"""Repository pins defeat internally valid replacement without new acquisition."""
import copy
import io
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch
import test_mlb_bulk_starter_snapshots as f
import mlb_provenance_checkpoint as m


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.f = f.SnapshotTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.bundle, self.snap = self.f.bundle, self.f.snap
        self.original = self.f.acquired()
        manifest = m.decode((self.snap / 'manifest.json').read_bytes())
        output = m.encoded(self.original)
        self.metadata = dict(schema='mlb-provenance-checkpoint-v1',
            bundle_sha256=self.original['plan']['bundle_sha256'],
            manifest_sha256=self.original['manifest_sha256'], plan_sha256=manifest['plan_sha256'],
            acquisition_sha256=manifest['acquisition_sha256'],
            contract_sha256=self.original['plan']['contract_sha256'],
            adapter_sha256=self.original['adapter_sha256'],
            replay_bytes=len(output), replay_sha256=m.sha(output))
        self.pin = self.f.root / 'repository-checkpoint.json'
        self.pin.write_bytes(m.encoded(self.metadata))
        self.enterContext(patch.object(m, 'CHECKPOINT', self.pin))
        self.enterContext(patch.object(f.m.urllib.request.OpenerDirector, 'open',
                                      side_effect=AssertionError('network forbidden')))

    def verify(self):
        return m.checkpoint(self.bundle, self.snap)

    def cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            status = m.main(['--bundle', str(self.bundle), '--snapshot-dir', str(self.snap)])
        return status, m.decode(out.getvalue().encode())

    def test_positive_offline_and_rails(self):
        result = self.verify()
        self.assertTrue(result['pinned_replay_verified'])
        self.assertEqual(result['replay_sha256'], self.metadata['replay_sha256'])
        for key in ('eligible', 'fitting_enabled', 'scoring_enabled'):
            self.assertIs(result[key], False)
        self.assertIsNone(result['historical_performance'])
        self.assertEqual(result, self.verify())
        self.assertEqual(self.cli(), (0, result))

    def test_each_pin_and_full_report_are_enforced(self):
        for key in self.metadata:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.metadata)
                changed[key] = {} if key == 'adapter_sha256' else 1 if key == 'replay_bytes' else 'wrong'
                self.pin.write_bytes(m.encoded(changed))
                with self.assertRaises(ValueError):
                    self.verify()
        self.pin.write_bytes(m.encoded(self.metadata))
        altered = copy.deepcopy(self.original)
        altered['occurrences'][0]['starter'] = None
        with patch.object(m, 'replay', return_value=altered):
            with self.assertRaisesRegex(ValueError, 'provenance_replay_mismatch'):
                self.verify()

    def test_matching_pins_still_validate_bodies(self):
        digest = self.original['receipts'][0]['body_sha256']
        (self.snap / 'objects' / digest).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'snapshot_integrity'):
            self.verify()

    def test_coherent_whole_bundle_replacement_fails_closed(self):
        source = m.decode((self.bundle / 'manifest.json').read_bytes())
        # Replace EVERY source and receipt, retaining valid JSON and identity.
        for entry in source['sources']:
            body = (self.bundle / 'objects' / entry['body_sha256']).read_bytes() + b' '
            entry.update(body_sha256=m.sha(body), size=len(body), headers={'replacement': 'yes'})
            (self.bundle / 'objects' / entry['body_sha256']).write_bytes(body)
        (self.bundle / 'manifest.json').write_bytes(m.encoded(source))
        for path in self.snap.glob('*.json'):
            if path.name.startswith(('snapshot-', 'timestamps-')):
                entry = m.decode(path.read_bytes())
                body = (self.snap / 'objects' / entry['body_sha256']).read_bytes() + b' '
                entry.update(body_sha256=m.sha(body), size=len(body), headers={'replacement': 'yes'})
                (self.snap / 'objects' / entry['body_sha256']).write_bytes(body)
                path.write_bytes(m.encoded(entry))
        plan = f.m.plan(self.bundle)[0]
        (self.snap / 'plan.json').write_bytes(m.encoded(plan))
        path = self.snap / 'manifest.json'
        manifest = m.decode(path.read_bytes())
        manifest['plan_sha256'] = m.sha(m.encoded(plan))
        manifest['receipts'] = f.m.validated_snapshots(self.snap)[1]
        path.write_bytes(m.encoded(manifest))
        # Positive control proves internal consistency accepts the replacement.
        replacement = f.m.replay(self.bundle, self.snap)
        self.assertEqual(replacement['summary'], self.original['summary'])
        for key in ('manifest_sha256', 'receipts', 'plan'):
            self.assertNotEqual(replacement[key], self.original[key])
        self.assertNotEqual(m.sha(m.encoded(replacement)), self.metadata['replay_sha256'])
        attacker_pin = self.metadata | dict(bundle_sha256=plan['bundle_sha256'],
            manifest_sha256=replacement['manifest_sha256'], plan_sha256=manifest['plan_sha256'],
            replay_bytes=len(m.encoded(replacement)), replay_sha256=m.sha(m.encoded(replacement)))
        (self.snap / 'mlb-provenance-checkpoint.json').write_bytes(m.encoded(attacker_pin))
        with self.assertRaisesRegex(ValueError, 'provenance_bundle_sha256_mismatch'):
            self.verify()
        status, result = self.cli()
        self.assertEqual(status, 2)
        self.assertIs(result['pinned_replay_verified'], False)

    def test_snapshot_only_coherent_reseal_rejected(self):
        path = self.snap / 'snapshot-100.json'
        receipt = m.decode(path.read_bytes())
        receipt['headers'] = {'replacement': 'yes'}
        path.write_bytes(m.encoded(receipt))
        self.f.reseal()
        f.m.replay(self.bundle, self.snap)
        with self.assertRaisesRegex(ValueError, 'provenance_manifest_sha256_mismatch'):
            self.verify()

    def test_absent_malformed_checkpoint_fails_closed(self):
        for raw in (b'{}', b'null', b'not json'):
            self.pin.write_bytes(raw)
            self.assertEqual(self.cli()[0], 2)
        self.pin.unlink()
        self.assertEqual(self.cli()[0], 2)
