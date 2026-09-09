"""Acquisition boundaries and sealed, network-free replay."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_mlb_chronological_admission import Bundle
from test_mlb_market_free_checkpoint import game, raw, schedule
from test_mlb_bulk_starter_admission import pitching_feed, retain as legacy_retain, target_snapshot as legacy_target_snapshot
import mlb_bulk_starter_snapshots as m


def complete_receipts(root):
    for path in root.glob('*.json'):
        if path.name.startswith(('timestamps-', 'snapshot-')):
            r = m.decode(path.read_bytes())
            r['completed_at_local'] = r['retrieved_at_local']
            path.write_bytes(m.encoded(r))


def retain(root, *args, **kwargs):
    result = legacy_retain(root, *args, **kwargs)
    complete_receipts(root)
    return result


def target_snapshot(root, *args, **kwargs):
    result = legacy_target_snapshot(root, *args, **kwargs)
    complete_receipts(root)
    return result


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / 'bundle'
        self.bundle.mkdir()
        self.snap = self.root / 'snap'
        self.g = game('2025-05-01')
        self.b = Bundle(self.bundle, [self.g])
        self.b.add('feed', '100', raw(pitching_feed(self.g)))
        self.b.save()
        self.calls = []

    def fake(self, root, kind, gid, url):
        self.calls.append((kind, gid, url))
        code = '20250501_155959'
        if kind == 'timestamps':
            retain(root, kind, gid, [code, '20250501_160000', '20250501_160001'])
        else:
            target_snapshot(root, self.g, code)
            # target_snapshot also writes index; restore the requested index contents
            retain(root, 'timestamps', gid, [code, '20250501_160000', '20250501_160001'])
        return m.decode((root / f'{kind}-{gid}.json').read_bytes())

    def acquired(self):
        with patch.object(m, 'fetch', side_effect=self.fake):
            return m.acquire(self.bundle, self.snap)

    def reseal(self):
        path = self.snap / 'manifest.json'
        manifest = m.decode(path.read_bytes())
        manifest['receipts'] = m.snapshots(self.snap)[1]
        path.write_bytes(m.encoded(manifest))

    def test_positive_bounded_selection_and_offline_determinism(self):
        result = self.acquired()
        self.assertEqual([c[0] for c in self.calls], ['timestamps', 'snapshot'])
        self.assertTrue(self.calls[1][2].endswith('?timecode=20250501_155959'))
        self.assertEqual(result['summary']['validation']['starter_candidates'], 1)
        self.assertEqual(result['feature_rows_created'], 0)
        self.assertEqual(result['prior_appearance_census_unresolved'], 0)
        self.assertFalse(result['eligible'])
        self.assertEqual(len(result['plan']['census']), 122)
        with patch.object(m.urllib.request.OpenerDirector, 'open', side_effect=AssertionError('network')):
            self.assertEqual(m.encoded(result), m.encoded(m.replay(self.bundle, self.snap)))
            self.assertEqual(m.encoded(result), m.encoded(m.acquire(self.bundle, self.snap)))

    def test_missing_day_disables_acquisition_and_nulls_denominator(self):
        self.b.sources = [r for r in self.b.sources if r['key'] != '2025-03-01']
        self.b.save()
        result = self.acquired()
        self.assertEqual(self.calls, [])
        self.assertIsNone(result['summary']['warmup']['occurrences'])
        self.assertEqual(result['occurrences'][0]['refusal'], 'schedule_census_unknown')

    def test_duplicates_nonregular_and_moved_games_never_requested(self):
        moved = copy.deepcopy(self.g)
        moved['officialDate'] = '2025-05-02'
        self.b.add('schedule', '2025-05-02', schedule('2025-05-02', [moved]))
        nonregular = game('2025-05-03', gid=101); nonregular['gameType'] = 'S'
        wrong_date = game('2025-05-04', gid=102); wrong_date['officialDate'] = '2025-05-05'
        self.b.add('schedule', '2025-05-03', schedule('2025-05-03', [nonregular]))
        self.b.add('schedule', '2025-05-04', schedule('2025-05-04', [wrong_date]))
        self.b.save()
        result = self.acquired()
        self.assertEqual(self.calls, [])
        self.assertEqual(len(result['occurrences']), 4)
        self.assertEqual(result['summary']['validation']['occurrences'], 4)

    def test_training_boundary_and_invalid_or_future_index(self):
        self.g = game('2025-04-30'); self.g['gameDate'] = '2025-05-01T02:00:00Z'
        self.b.add('schedule', '2025-05-01', schedule('2025-05-01', []))
        self.b.add('schedule', '2025-04-30', schedule('2025-04-30', [self.g]))
        self.b.save()
        p = m.plan(self.bundle)[0]
        self.assertEqual(p['targets'][0]['cutoff'], '2025-05-01T00:00:00+00:00')
        for codes in (['20250501_000000'], ['20250501_000001'], ['bad'], ['20250430_235959'] * 2):
            with self.subTest(codes=codes):
                with self.assertRaises(ValueError):
                    m.selected_code(raw(codes), p['targets'][0]['cutoff'])
        self.assertEqual(m.selected_code(raw(['20250430_235959', '20250501_000000']), p['targets'][0]['cutoff']), '20250430_235959')

    def test_http_failure_bytes_remain_refusal_and_sealed_no_retry(self):
        def failure(root, kind, gid, url):
            retain(root, kind, gid, {'error': 'unavailable'})
            path = root / f'{kind}-{gid}.json'
            r = m.decode(path.read_bytes()); r.update(http_status=503, failure='http_error')
            path.write_bytes(m.encoded(r))
            return r
        with patch.object(m, 'fetch', side_effect=failure) as fetch:
            result = m.acquire(self.bundle, self.snap)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result['occurrences'][0]['acquisition_refusal'], 'timestamp_source_unavailable')
        self.assertEqual(result['acquired_responses'], 0)
        with patch.object(m, 'fetch', side_effect=AssertionError('retry')):
            m.acquire(self.bundle, self.snap)
        digest = result['receipts'][0]['body_sha256']
        (self.snap / 'objects' / digest).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'snapshot_integrity'):
            m.replay(self.bundle, self.snap)

    def test_receipt_removal_replacement_and_outside_plan(self):
        self.acquired()
        p = self.snap / 'snapshot-100.json'; saved = p.read_bytes(); p.unlink()
        with self.assertRaisesRegex(ValueError, 'snapshot_manifest_receipts_changed'):
            m.replay(self.bundle, self.snap)
        self.reseal()
        with self.assertRaisesRegex(ValueError, 'planned_snapshot_receipt_missing'):
            m.replay(self.bundle, self.snap)
        p.write_bytes(saved); self.reseal()
        target_snapshot(self.snap, game('2025-05-01', gid=999)); self.reseal()
        with self.assertRaisesRegex(ValueError, 'receipt_outside_selected_plan'):
            m.replay(self.bundle, self.snap)

    def test_identity_metadata_state_are_rechecked_after_retention(self):
        self.acquired()
        for change, reason in [
            (lambda f: f['gameData']['teams']['home'].update(id=999), 'snapshot_team_mismatch'),
            (lambda f: f.update(gamePk=999), 'snapshot_game_mismatch'),
            (lambda f: f['gameData']['datetime'].update(originalDate='2025-04-30'), 'snapshot_schedule_mismatch'),
            (lambda f: f['metaData'].update(timeStamp='20250501_160000'), 'returned_snapshot_time_mismatch'),
            (lambda f: f['gameData']['status'].update(abstractGameState='Final'), 'snapshot_not_pregame')]:
            target_snapshot(self.snap, self.g, mutate=change); self.reseal()
            result = m.replay(self.bundle, self.snap)
            self.assertEqual(result['occurrences'][0]['acquisition_refusal'], reason)
            self.assertIsNone(result['occurrences'][0]['starter'])

    def test_unresolved_appearance_census_is_independent_of_starter(self):
        self.b.add('feed', '100', b'{}'); self.b.save()
        result = self.acquired()
        self.assertEqual(result['prior_appearance_census_unresolved'], 1)
        self.assertIsNotNone(result['occurrences'][0]['starter'])
        self.assertEqual(result['feature_rows_created'], 0)
        self.assertEqual(result['inherited_missing_source_refusals'], 1243)

    def test_plan_mutation_and_symlink_fail_closed(self):
        self.acquired()
        p = self.snap / 'plan.json'; original = p.read_bytes(); p.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'acquisition_plan_changed'):
            m.replay(self.bundle, self.snap)
        p.unlink(); other = self.root / 'other'; other.write_bytes(original); p.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'snapshot_manifest_symlink'):
            m.replay(self.bundle, self.snap)

    def test_transport_size_limit_retains_prefix_and_no_redirect(self):
        class Response:
            code = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'x' * limit
        self.snap.mkdir(); (self.snap / 'objects').mkdir()
        with patch.object(m.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = Response()
            with patch.dict(m.LIMITS, response_bytes=10):
                r = m.fetch(self.snap, 'timestamps', '100', 'https://statsapi.mlb.com/test')
            self.assertIsInstance(opener.call_args.args[0], m.NoRedirect)
        self.assertEqual(r['failure'], 'response_size_limit_exceeded')
        self.assertEqual(r['size'], 11)
        self.assertEqual((self.snap / 'objects' / r['body_sha256']).read_bytes(), b'x' * 11)
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 302, None, None, 'https://other'))

    def test_interrupted_acquisition_reuses_retained_index(self):
        original_fetch = m.fetch
        def interrupted(root, kind, gid, url):
            if kind == 'snapshot':
                raise OSError('interrupted')
            return self.fake(root, kind, gid, url)
        with patch.object(m, 'fetch', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                m.acquire(self.bundle, self.snap)
        self.assertFalse((self.snap / 'manifest.json').exists())
        before = (self.snap / 'timestamps-100.json').read_bytes()
        def resume(root, kind, gid, url):
            if kind == 'timestamps':
                return original_fetch(root, kind, gid, url)
            return self.fake(root, kind, gid, url)
        with patch.object(m, 'fetch', side_effect=resume), patch.object(m.urllib.request, 'build_opener', side_effect=AssertionError('index re-fetch')):
            result = m.acquire(self.bundle, self.snap)
        self.assertEqual(before, (self.snap / 'timestamps-100.json').read_bytes())
        self.assertEqual(result['summary']['validation']['starter_candidates'], 1)

    def test_cached_snapshot_selection_checked_before_any_network(self):
        self.acquired()
        (self.snap / 'manifest.json').unlink()
        path = self.snap / 'snapshot-100.json'
        r = m.decode(path.read_bytes()); r['url'] = r['url'].replace('155959', '155958')
        path.write_bytes(m.encoded(r))
        with patch.object(m, 'fetch', side_effect=AssertionError('network')):
            with self.assertRaisesRegex(ValueError, 'cached_snapshot_selection'):
                m.acquire(self.bundle, self.snap)


    def test_resume_receipt_boundaries_reject_before_workers_or_sealing(self):
        self.acquired()
        (self.snap / 'manifest.json').unlink()
        path = self.snap / 'timestamps-100.json'
        original = path.read_bytes()
        cases = [
            ('missing', lambda r: r.pop('completed_at_local')),
            ('malformed', lambda r: r.update(completed_at_local='bad')),
            ('naive', lambda r: r.update(completed_at_local='2026-09-08T00:00:00')),
            ('backwards', lambda r: r.update(completed_at_local='2026-09-07T23:59:59Z')),
            ('null', lambda r: r.update(completed_at_local=None)),
            ('bool_size', lambda r: r.update(size=True)),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                r = m.decode(original); mutate(r); path.write_bytes(m.encoded(r))
                with patch.object(m, 'fetch', side_effect=AssertionError('network')):
                    with self.assertRaises((ValueError, KeyError)):
                        m.acquire(self.bundle, self.snap)
                self.assertFalse((self.snap / 'manifest.json').exists())
        path.write_bytes(original)
        # Reproduce the reviewer's ceiling bypass with an otherwise valid plan/cache.
        snapshot_size = m.decode((self.snap / 'snapshot-100.json').read_bytes())['size']
        for ceiling in (10, snapshot_size - 1):
            with self.subTest(ceiling=ceiling), patch.dict(m.LIMITS, response_bytes=ceiling):
                (self.snap / 'plan.json').write_bytes(m.encoded(m.plan(self.bundle)[0]))
                with patch.object(m, 'fetch', side_effect=AssertionError('network')):
                    with self.assertRaisesRegex(ValueError, 'acquisition_response_size_limit'):
                        m.acquire(self.bundle, self.snap)
                self.assertFalse((self.snap / 'manifest.json').exists())

    def test_replay_checks_completion_even_with_rebound_manifest(self):
        self.acquired()
        path = self.snap / 'snapshot-100.json'
        r = m.decode(path.read_bytes()); r.pop('completed_at_local')
        path.write_bytes(m.encoded(r)); self.reseal()
        with self.assertRaises(KeyError):
            m.replay(self.bundle, self.snap)

    def test_refused_oversize_prefix_is_bounded_and_never_evidence(self):
        self.snap.mkdir(); (self.snap / 'objects').mkdir()
        with patch.dict(m.LIMITS, response_bytes=10):
            retain(self.snap, 'timestamps', '100', 'x' * 20)
            path = self.snap / 'timestamps-100.json'
            r = m.decode(path.read_bytes()); r.update(failure='response_size_limit_exceeded')
            path.write_bytes(m.encoded(r))
            with self.assertRaisesRegex(ValueError, 'acquisition_oversize_prefix'):
                m.validated_snapshots(self.snap)
            body = b'x' * 11; digest = m.sha(body)
            (self.snap / 'objects' / digest).write_bytes(body)
            r.update(size=11, body_sha256=digest); path.write_bytes(m.encoded(r))
            sources, _ = m.validated_snapshots(self.snap)
            self.assertIsNone(sources['timestamps', '100'][1])
