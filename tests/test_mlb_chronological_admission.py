"""Synthetic boundary tests; fixtures are not historical evidence."""
import copy
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mlb_chronological_admission as m
import mlb_chronological_starter_probe as probe
from test_mlb_market_free_checkpoint import game, feed, schedule, raw


class Bundle:
    def __init__(self, root, games):
        self.root = Path(root)
        (self.root / 'objects').mkdir()
        self.sources = []
        for bounds in m.contract()['splits'].values():
            for day in m.dates(bounds):
                self.add('schedule', day, schedule(day, [g for g in games if g['officialDate'] == day]))
        for g in games:
            self.add('feed', str(g['gamePk']), raw(feed(g)))
        self.save()

    def add(self, kind, key, body):
        digest = m.sha(body)
        (self.root / 'objects' / digest).write_bytes(body)
        url = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={key}&hydrate=linescore'
               if kind == 'schedule' else f'https://statsapi.mlb.com/api/v1.1/game/{key}/feed/live')
        self.sources = [s for s in self.sources if (s['kind'], s['key']) != (kind, key)]
        self.sources.append(dict(kind=kind, key=key, body_sha256=digest, size=len(body),
            url=url, retrieved_at_local='2026-09-08T00:00:00Z', headers={}, http_status=200, failure=None))

    def save(self):
        (self.root / 'manifest.json').write_bytes(m.encoded({'schema':'mlb-chronological-source-v1',
            'contract_sha256': m.sha(m.CONTRACT_PATH.read_bytes()), 'sources': self.sources}))


class AdmissionTests(unittest.TestCase):
    def test_full_census_outcome_and_training_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            g = game('2025-04-30')
            b = Bundle(root, [g])
            for end, expected in [('2025-04-30T23:59:59Z', True), ('2025-05-01T00:00:00Z', False),
                                  ('2025-05-01T00:00:01Z', False)]:
                f = feed(g)
                f['liveData']['plays']['allPlays'][0]['about']['endTime'] = end
                b.add('feed', '100', raw(f)); b.save()
                report = m.admission(root)
                self.assertTrue(report['census_complete'])
                row = report['occurrences'][0]
                self.assertEqual(row['outcome_status'], 'admitted_reconstruction')
                self.assertEqual(row['training_label_admitted'], expected)
                self.assertFalse(row['challenger_row_admitted'])
                self.assertIsNone(report['historical_performance'])
                self.assertFalse(report['fitting_enabled'])

    def test_missing_day_is_unknown_and_blocks_duplicate_admission(self):
        with tempfile.TemporaryDirectory() as root:
            b = Bundle(root, [game()])
            b.sources = [s for s in b.sources if s['key'] != '2025-03-01']; b.save()
            r = m.admission(root)
            self.assertFalse(r['census_complete'])
            self.assertIsNone(r['summary']['warmup']['occurrences'])
            self.assertEqual(r['occurrences'][0]['outcome_refusal'], 'full_census_unknown_duplicate_check_blocked')

    def test_every_raw_object_verified_even_excluded_schedule(self):
        with tempfile.TemporaryDirectory() as root:
            b = Bundle(root, [game()])
            s = next(s for s in b.sources if s['key'] == '2025-03-01')
            (Path(root) / 'objects' / s['body_sha256']).write_bytes(b'{}')
            with self.assertRaisesRegex(ValueError, 'source_integrity'):
                m.admission(root)

    def test_repeated_id_preserves_both_occurrences_across_splits(self):
        with tempfile.TemporaryDirectory() as root:
            g = game('2025-04-30')
            b = Bundle(root, [g])
            moved = game('2025-05-01')
            b.add('schedule', '2025-05-01', schedule('2025-05-01', [moved])); b.save()
            r = m.admission(root)
            self.assertEqual(len(r['occurrences']), 2)
            self.assertTrue(all(x['outcome_refusal'] == 'repeated_game_across_source_dates' for x in r['occurrences']))
            self.assertEqual(sum(x['outcomes_admitted'] for x in r['summary'].values()), 0)

    def test_identity_score_status_and_completion_corruption_refused(self):
        mutations = [lambda f: f['gameData']['teams']['home'].update(id=3),
                     lambda f: f['liveData']['linescore']['teams']['home'].update(runs=9),
                     lambda f: f['gameData']['status'].update(reason='Resumed'),
                     lambda f: f['liveData']['plays']['allPlays'][0]['about'].update(isComplete=False)]
        g = game()
        parsed = m.schedule_census(schedule(g['officialDate'], [g]), g['officialDate'])[0]
        for mutate in mutations:
            f = feed(g); mutate(f)
            with self.assertRaises(ValueError):
                m.outcome(parsed, raw(f))

    def test_lagged_history_excludes_same_day_and_cutoff_equality(self):
        g = {'source_date':'2025-05-02', 'away_id':'1', 'home_id':'2'}
        cutoff = m.instant('2025-05-02T16:00:00Z')
        history = [dict(game_id=str(i), source_date='2025-04-30', away_id='1', home_id='2', home_won=i % 2,
                        completed_at=f'2025-05-01T12:{i:02}:00Z') for i in range(1, 12)]
        excluded = [dict(history[0], game_id='99', source_date='2025-05-02'),
                    dict(history[0], game_id='98', completed_at=cutoff.isoformat()),
                    dict(history[0], game_id='97', completed_at=(cutoff + timedelta(seconds=1)).isoformat())]
        r = m.team_history(g, history + excluded, cutoff, True)
        self.assertEqual([x['game_id'] for x in r['away']['games']], [str(i) for i in range(2,12)])
        self.assertEqual(r['home']['denominator'], 10)
        self.assertEqual(r['home']['wins'], 5)
        self.assertEqual(r['home_minus_away'], 0)
        self.assertIsNone(m.team_history(g, history[:9], cutoff, True)['home_minus_away'])
        self.assertIsNone(m.team_history(g, history, cutoff, False)['home_minus_away'])

    def test_target_final_does_not_own_team_feature(self):
        games = [game(f'2025-04-{i:02}', gid=i) for i in range(1,11)] + [game('2025-05-01', gid=100)]
        with tempfile.TemporaryDirectory() as root:
            b = Bundle(root, games)
            before = m.admission(root)['occurrences'][-1]['team_history']
            self.assertIsNotNone(before['home_minus_away'])
            b.add('feed','100', b'{}'); b.save()
            after = m.admission(root)['occurrences'][-1]
            self.assertEqual(after['outcome_status'], 'refused')
            self.assertEqual(after['team_history'], before)

    def test_manifest_contract_and_duplicate_sources(self):
        with tempfile.TemporaryDirectory() as root:
            b = Bundle(root, [])
            b.sources.append(copy.deepcopy(b.sources[0])); b.save()
            with self.assertRaisesRegex(ValueError, 'duplicate_or_invalid_source'):
                m.load_bundle(root)


class StarterProbeTests(unittest.TestCase):
    def run_probe(self, codes, mutate=None):
        g = game('2025-03-18')
        # One selected training occurrence; other splits have no regular games.
        with tempfile.TemporaryDirectory() as root:
            Bundle(root, [g])
            f = feed(g)
            f['metaData'] = {'timeStamp':'20250318_155959'}
            f['gameData']['status'] = {'abstractGameState':'Preview'}
            f['gameData']['probablePitchers'] = {'away':{'id':10},'home':{'id':20}}
            if mutate: mutate(f)
            def retained(_root, kind, key, url, acquire):
                return {'url':url}, codes if kind == 'timestamps' else f
            with patch.object(probe, 'retained', side_effect=retained):
                return next(r for r in probe.probe(root, root)['samples'] if r['split'] == 'train')

    def test_provider_snapshot_candidate_requires_strict_cutoff(self):
        self.assertEqual(self.run_probe(['20250318_155959'])['status'], 'provider_historical_candidate')
        self.assertEqual(self.run_probe(['20250318_160000'])['reason'], 'no_provider_snapshot_before_cutoff')

    def test_final_feed_and_ignored_timecode_and_wrong_teams_refused(self):
        for mutate, reason in [
            (lambda f: f['gameData']['status'].update(abstractGameState='Final'), 'snapshot_not_pregame'),
            (lambda f: f['metaData'].update(timeStamp='20250318_190000'), 'returned_snapshot_time_mismatch'),
            (lambda f: f['gameData']['teams']['away'].update(id=9), 'snapshot_team_mismatch'),
            (lambda f: f['gameData'].pop('probablePitchers'), "'probablePitchers'")]:
            self.assertEqual(self.run_probe(['20250318_155959'], mutate)['reason'], reason)
