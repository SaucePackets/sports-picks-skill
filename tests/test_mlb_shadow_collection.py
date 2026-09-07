import copy
import json
import tempfile
import unittest
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from scripts import mlb_shadow_collection as s


class ShadowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = s.Store(self.tmp.name)
        self.game = dict(game_id='42', away_id='10', home_id='20', first_pitch='2026-09-08T20:00:00Z')
        self.source = dict(game=self.game, source_id='fixture-book', observed_at='2026-09-08T18:00:00Z',
                           odds=dict(book='fixture-book', market='full_game_moneyline_including_extras', away_decimal=2.0, home_decimal=1.8))
        self.private = Ed25519PrivateKey.generate()
        self.key = self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    def bundle(self, family='market', source=None, cutoff='2026-09-01T00:00:00Z', producer='11' * 32):
        return s.capture(self.store, self.game, s.canonical(source or self.source), family, producer, cutoff)

    def attempt(self, key=None, time='2026-09-08T19:00:00Z', family='market', sign=True):
        key = key or self.bundle(family)
        payload = s.receipt_payload(key, time, self.key)
        receipt = dict(payload=payload, signature=self.private.sign(s.canonical(payload)).hex()) if sign else None
        return dict(game_id='42', family=family, bundle_digest=key, receipt=receipt)

    def add(self, attempt):
        self.store.append('attempts', attempt)

    def row(self):
        return s.report(self.store, [self.game], [self.key])['rows'][0]

    def assess(self, attempt):
        return s.assess(self.store, attempt, self.game, 'market', [self.key])

    def observation(self, kind, **overrides):
        source = dict(self.source)
        if kind == 'finals':
            source.update(status='Final', away_score=3, home_score=1, observed_at='2026-09-08T23:00:00Z')
        else:
            source['observed_at'] = '2026-09-08T19:50:00Z'
        source.update(overrides)
        self.store.append(kind, {'source_digest': self.store.put(s.canonical(source))})

    def test_complete_offline_dry_run(self):
        for family in s.FAMILIES:
            self.add(self.attempt(family=family))
        self.observation('finals')
        self.observation('closings')
        report = s.report(self.store, [self.game], [self.key])
        row = report['rows'][0]
        self.assertTrue(row['fixture_contract_pass'])
        self.assertTrue(row['finals']['away_won'])
        self.assertEqual(row['closings']['status'], 'joined')
        self.assertEqual([r['state'] for r in report['rows']], ['captured', 'missing', 'missing'])
        self.assertFalse(any(r['eligible'] for r in report['rows']))
        self.assertEqual(report['shared_eligible_game_ids'], [])
        bundle = json.loads(self.store.get(row['selected_fixture_bundle']))
        self.assertEqual(self.store.get(bundle['source_digest']), s.canonical(self.source))
        self.assertAlmostEqual(bundle['prediction']['probability'], .47368421052631576)

    def test_forged_receipts(self):
        edits = [lambda a: a['receipt'].update(signature='00' * 64),
                 lambda a: a['receipt']['payload'].update(bundle_digest='0' * 64),
                 lambda a: a['receipt']['payload'].update(observed_at='2026-09-08T17:00:00Z'),
                 lambda a: a['receipt']['payload'].update(purpose='live')]
        for edit in edits:
            attempt = self.attempt()
            edit(attempt)
            self.assertEqual(self.assess(attempt)[0], 'invalid')
        self.assertEqual(self.assess(self.attempt(self.bundle(producer=self.key)))[0], 'invalid')
        self.assertEqual(s.assess(self.store, self.attempt(), self.game, 'market', [])[0], 'invalid')
        self.assertEqual(self.assess(self.attempt(time='2026-09-08T17:00:00Z'))[0], 'invalid')

    def test_changed_bytes(self):
        attempt = self.attempt()
        self.add(attempt)
        (Path(self.tmp.name) / 'objects' / attempt['bundle_digest']).write_bytes(b'{}')
        self.assertEqual(self.row()['state'], 'invalid')
        with self.assertRaises(ValueError):
            self.store.get(attempt['bundle_digest'])

    def test_bundle_mutations_even_with_new_signature(self):
        edits = [lambda b: b.update(model_version=99), lambda b: b.update(model_version=True),
                 lambda b: b.update(producer_key=''), lambda b: b.update(spec_digest='0' * 64),
                 lambda b: b.update(artifact_digest='0' * 64),
                 lambda b: b['game'].update(away_id='20', home_id='10'),
                 lambda b: b['prediction'].update(probability=.99),
                 lambda b: b.update(observed_at='2026-09-08T17:00:00Z'),
                 lambda b: b.update(training_cutoff='2026-09-09T00:00:00Z')]
        for edit in edits:
            bundle = json.loads(self.store.get(self.bundle()))
            edit(bundle)
            key = self.store.put(s.canonical(bundle))
            self.assertEqual(self.assess(self.attempt(key))[0], 'invalid')

    def test_late_and_missing(self):
        self.assertEqual(self.assess(self.attempt(time=self.game['first_pitch']))[0], 'late')
        source = copy.deepcopy(self.source)
        source['observed_at'] = self.game['first_pitch']
        self.assertEqual(self.assess(self.attempt(self.bundle(source=source)))[0], 'late')
        self.add(self.attempt(sign=False))
        self.assertEqual(self.row()['reason'], 'missing_receipt')
        source = copy.deepcopy(self.source)
        del source['odds']['home_decimal']
        self.assertEqual(self.assess(self.attempt(self.bundle(source=source)))[0], 'missing')

    def test_retries_and_duplicates(self):
        early, late = self.attempt(time='2026-09-08T18:30:00Z'), self.attempt()
        for a in (late, early, early):
            self.add(a)
        self.assertEqual(len(self.row()['attempts']), 2)
        self.assertTrue(self.row()['fixture_contract_pass'])
        with self.assertRaisesRegex(ValueError, 'duplicate_schedule_game'):
            s.report(self.store, [self.game, self.game], [self.key])

    def test_independent_adapters(self):
        for family in s.FAMILIES[1:]:
            for source in (self.source, {}):
                self.assertIsNone(s.prediction(family, source)['probability'])
                self.assertIn('estimator_unimplemented', s.prediction(family, source)['missing'])

    def test_joins_fail_closed(self):
        self.add(self.attempt())
        self.observation('finals', game=dict(self.game, away_id='20', home_id='10'))
        self.observation('closings', odds=dict(self.source['odds'], book='different'))
        self.assertEqual(self.row()['finals']['status'], 'invalid')
        self.assertEqual(self.row()['closings']['status'], 'unavailable')
        self.assertTrue(self.row()['fixture_contract_pass'])

    def test_duplicate_finals(self):
        self.add(self.attempt())
        self.observation('finals')
        self.observation('finals', away_score=4)
        self.assertEqual(self.row()['finals']['reason'], 'ambiguous_observations')

    def test_bad_odds_and_mapping(self):
        for odds in (True, float('inf'), 1, -1):
            source = copy.deepcopy(self.source)
            source['odds']['away_decimal'] = odds
            with self.assertRaises(ValueError):
                s.prediction('market', source)
        source = copy.deepcopy(self.source)
        source['game']['away_id'] = 'wrong'
        with self.assertRaises(ValueError):
            self.bundle(source=source)

    def test_source_byte_tamper_and_attempt_routing(self):
        key = self.bundle()
        bundle = json.loads(self.store.get(key))
        path = Path(self.tmp.name) / 'objects' / bundle['source_digest']
        path.write_bytes(b'{}')
        self.assertEqual(self.assess(self.attempt(key))[0], 'invalid')
        attempt = self.attempt(self.bundle(source=dict(self.source, source_id='other')))
        attempt['game_id'] = 'unknown'
        self.add(attempt)
        result = s.report(self.store, [self.game], [self.key])
        self.assertEqual(len(result['unmatched_attempts']), 1)
        self.assertEqual(result['rows'][0]['reason'], 'no_attempt')

    def test_selection_uses_receipt_time_not_insertion_or_probability(self):
        early = self.attempt(time='2026-09-08T18:30:00Z')
        other_source = copy.deepcopy(self.source)
        other_source['odds']['away_decimal'] = 3.0
        later = self.attempt(self.bundle(source=other_source))
        self.add(later)
        self.add(early)
        self.assertEqual(self.row()['selected_fixture_bundle'], early['bundle_digest'])
        self.assertEqual(len(self.row()['attempts']), 2)

    def test_closing_before_capture_and_tied_final(self):
        self.add(self.attempt())
        self.observation('closings', observed_at='2026-09-08T18:30:00Z')
        self.observation('finals', away_score=1, home_score=1)
        self.assertEqual(self.row()['closings']['status'], 'unavailable')
        self.assertEqual(self.row()['finals']['status'], 'invalid')

    def test_denominator(self):
        self.add(self.attempt())
        result = s.report(self.store, [self.game, dict(self.game, game_id='43')])
        self.assertEqual(len(result['rows']), 6)
        self.assertFalse(any(r['fixture_contract_pass'] for r in result['rows']))
        self.assertEqual(result['shared_eligible_game_ids'], [])


if __name__ == '__main__':
    unittest.main()
