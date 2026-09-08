"""Adversarial offline reconstruction controls; synthetic bytes are not evidence."""
import copy
from pathlib import Path
import tempfile
import unittest

from test_mlb_chronological_admission import Bundle
from test_mlb_market_free_checkpoint import game, feed, raw, schedule
import mlb_bulk_starter_admission as m


def pitching_feed(g):
    f = feed(g)
    plays = []
    boxes = {}
    for side, pid, top, event in [('away', 10, False, 'walk'), ('home', 20, True, 'strikeout')]:
        p = copy.deepcopy(f['liveData']['plays']['allPlays'][0])
        p['about'].update(atBatIndex=len(plays), isTopInning=top)
        p['result'].update(type='atBat', eventType=event)
        p['matchup']['pitcher']['id'] = pid
        plays.append(p)
        boxes[side] = {'team': {'id': g['teams'][side]['team']['id']}, 'pitchers': [pid],
            'players': {'ID'+str(pid): {'person': {'id': pid}, 'stats': {'pitching': {
                'battersFaced': 1, 'strikeOuts': int(event == 'strikeout'), 'baseOnBalls': int(event == 'walk')}}}}}
    f['liveData']['plays']['allPlays'] = plays
    f['liveData']['boxscore'] = {'teams': boxes}
    return f


def retain(root, kind, gid, value, code='20250501_155959'):
    body = raw(value)
    digest = m.sha(body)
    (root/'objects').mkdir(exist_ok=True)
    (root/'objects'/digest).write_bytes(body)
    url = f'https://statsapi.mlb.com/api/v1.1/game/{gid}/feed/live'
    url += '/timestamps' if kind == 'timestamps' else '?timecode='+code
    receipt = {'kind': kind, 'key': str(gid), 'url': url, 'body_sha256': digest, 'size': len(body),
               'retrieved_at_local': '2026-09-08T00:00:00Z', 'headers': {}, 'http_status': 200, 'failure': None}
    (root/f'{kind}-{gid}.json').write_bytes(raw(receipt))
    return root/'objects'/digest


def target_snapshot(root, g, code='20250501_155959', mutate=None):
    f = feed(g)
    f['metaData'] = {'timeStamp': code}
    f['gameData']['status'] = {'abstractGameState': 'Preview'}
    f['gameData']['probablePitchers'] = {'away': {'id': 10}, 'home': {'id': 20}}
    if mutate:
        mutate(f)
    retain(root, 'timestamps', g['gamePk'], [code], code)
    return retain(root, 'snapshot', g['gamePk'], f, code)


def setup(root, snap):
    games = [game(f'2025-04-{d:02}', gid=d) for d in (21, 23, 25)] + [game('2025-05-01')]
    b = Bundle(root, games)
    for g in games:
        b.add('feed', str(g['gamePk']), raw(pitching_feed(g)))
    b.save()
    target_snapshot(snap, games[-1])
    return b, games


class BulkTests(unittest.TestCase):
    def test_positive_full_replay_and_weighted_counts(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            setup(root, Path(snap))
            r = m.admission(root, snap)
            self.assertTrue(r['census_complete'])
            self.assertEqual(len(r['census']), 122)
            self.assertEqual(len(r['occurrences']), 4)
            row = r['occurrences'][-1]
            self.assertTrue(row['features_admitted'])
            self.assertEqual(row['strikeout_fraction_difference'], 1)
            self.assertEqual(row['walk_fraction_difference'], -1)
            self.assertEqual(row['histories']['away']['plate_appearances'], 3)
            self.assertEqual(len(row['histories']['home']['appearances']), 3)
            self.assertTrue(all(x['feed_sha256'] and x['play_pointers'] for x in row['histories']['away']['appearances']))
            self.assertFalse(r['eligible']); self.assertFalse(r['scoring_enabled']); self.assertFalse(r['fitting_enabled'])
            self.assertIsNone(r['historical_performance'])
            self.assertEqual(m.encoded(r), m.encoded(m.admission(root, snap)))

    def test_target_final_never_selects_starter_or_changes_features(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            b, _ = setup(root, Path(snap))
            before = m.admission(root, snap)['occurrences'][-1]
            b.add('feed', '100', b'{}'); b.save()
            self.assertEqual(before, m.admission(root, snap)['occurrences'][-1])

    def test_missing_receipts_stay_visible_no_imputation(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            setup(root, Path(snap))
            for p in Path(snap).glob('*.json'): p.unlink()
            r = m.admission(root, snap)
            self.assertEqual(len(r['occurrences']), 4)
            self.assertTrue(all(x['refusal'] == 'retained_starter_source_unavailable' for x in r['occurrences']))
            self.assertTrue(all(x['strikeout_fraction_difference'] is None for x in r['occurrences']))

    def test_missing_schedule_and_duplicate_occurrences(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            b, games = setup(root, Path(snap))
            moved = copy.deepcopy(games[0]); moved['officialDate'] = '2025-04-22'
            b.add('schedule', '2025-04-22', schedule('2025-04-22', [moved])); b.save()
            r = m.admission(root, snap)
            self.assertEqual(len(r['occurrences']), 5)
            self.assertEqual(r['occurrences'][0]['refusal'], 'repeated_game_across_source_dates')
            b.sources = [x for x in b.sources if x['key'] != '2025-03-01']; b.save()
            r = m.admission(root, snap)
            self.assertIsNone(r['summary']['warmup']['occurrences'])
            self.assertTrue(all(not x['features_admitted'] for x in r['occurrences']))

    def test_unresolved_prior_game_cannot_be_skipped(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            b, _ = setup(root, Path(snap))
            b.add('feed', '23', b'{}'); b.save()
            row = m.admission(root, snap)['occurrences'][-1]
            self.assertEqual(row['histories']['home']['refusal'], 'prior_appearance_census_unresolved')
            self.assertIsNone(row['walk_fraction_difference'])

    def test_snapshot_cutoff_identity_and_state(self):
        cases = [(lambda f: f['gameData']['teams']['home'].update(id=3), 'snapshot_team_mismatch'),
                 (lambda f: f.update(gamePk=101), 'snapshot_game_mismatch'),
                 (lambda f: f['gameData']['datetime'].update(officialDate='2025-05-02'), 'snapshot_schedule_mismatch'),
                 (lambda f: f['gameData']['datetime'].update(dateTime='2025-05-01T18:00:00Z'), 'snapshot_schedule_mismatch'),
                 (lambda f: f['gameData']['status'].update(abstractGameState='Final'), 'snapshot_not_pregame'),
                 (lambda f: f['metaData'].update(timeStamp='20250501_160000'), 'returned_snapshot_time_mismatch'),
                 (lambda f: f['gameData']['probablePitchers']['away'].update(id=True), 'invalid_numeric_id'),
                 (lambda f: f['gameData'].pop('probablePitchers'), 'probablePitchers')]
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            _, games = setup(root, Path(snap))
            for mutate, reason in cases:
                target_snapshot(Path(snap), games[-1], mutate=mutate)
                row = m.admission(root, snap)['occurrences'][-1]
                self.assertIn(reason, row['refusal'])
                self.assertIsNone(row['strikeout_fraction_difference'])
            for code in ('20250501_160000', '20250501_160001'):
                target_snapshot(Path(snap), games[-1], code)
                self.assertEqual(m.admission(root, snap)['occurrences'][-1]['refusal'], 'no_provider_snapshot_before_cutoff')

    def test_training_features_use_global_cutoff_too(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            g = game('2025-04-30')
            g['gameDate'] = '2025-05-01T02:00:00Z'
            Bundle(root, [g])
            target_snapshot(Path(snap), g, '20250501_003000')
            row = m.admission(root, snap)['occurrences'][0]
            self.assertEqual(row['observation_cutoff'], '2025-05-01T00:00:00+00:00')
            self.assertEqual(row['refusal'], 'no_provider_snapshot_before_cutoff')

    def test_integrity_abort_even_unused_or_failed_snapshot(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            setup(root, Path(snap))
            obj = target_snapshot(Path(snap), game('2025-05-01', gid=999))
            for body in (obj.read_bytes().replace(b'Preview', b'preview'), obj.read_bytes()+b' '):
                obj.write_bytes(body)
                with self.assertRaisesRegex(ValueError, 'snapshot_integrity'): m.admission(root, snap)
            target_snapshot(Path(snap), game('2025-05-01', gid=999))
            p = Path(snap)/'snapshot-999.json'; r = m.decode(p.read_bytes()); r.update(failure='http_error', http_status=500); p.write_bytes(raw(r))
            obj.write_bytes(b'{}')
            with self.assertRaisesRegex(ValueError, 'snapshot_integrity'): m.admission(root, snap)

    def test_receipt_symlink_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as snap:
            setup(root, Path(snap))
            p = Path(snap)/'snapshot-100.json'; b = p.read_bytes(); p.unlink(); p.symlink_to(Path(snap)/'timestamps-100.json')
            with self.assertRaisesRegex(ValueError, 'snapshot_receipt_symlink'): m.admission(root, snap)
            p.unlink(); r = m.decode(b); r['body_sha256'] = '../escape'; p.write_bytes(raw(r))
            with self.assertRaisesRegex(ValueError, 'snapshot_digest'): m.admission(root, snap)

    def test_appearance_corruption_is_refused(self):
        g = game(); parsed = m.schedule_census(schedule(g['officialDate'], [g]), g['officialDate'])[0]
        parsed['schedule_sha256'] = 'a'*64
        mutations = [lambda f: f['liveData']['boxscore']['teams']['away']['team'].update(id=3),
                     lambda f: f['liveData']['boxscore']['teams']['away']['players']['ID10']['person'].update(id=11),
                     lambda f: f['liveData']['boxscore']['teams']['away']['players']['ID10']['stats']['pitching'].update(battersFaced=2),
                     lambda f: f['liveData']['plays']['allPlays'][0]['about'].update(isTopInning=True),
                     lambda f: f['liveData']['plays']['allPlays'][0]['about'].update(atBatIndex=1),
                     lambda f: f['liveData']['plays']['allPlays'][0]['about'].update(isComplete=False),
                     lambda f: f['liveData']['plays']['allPlays'][0]['result'].update(eventType='unknown'),
                     lambda f: f['gameData']['datetime'].update(officialDate='2025-04-22')]
        self.assertEqual(len(m.appearances(parsed, raw(pitching_feed(g)))), 2)
        for mutate in mutations:
            f = pitching_feed(g); mutate(f)
            with self.assertRaises((ValueError, KeyError)): m.appearances(parsed, raw(f))

    def test_true_last_three_cutoff_date_and_pooling(self):
        cutoff = m.instant('2025-05-01T16:00:00Z')
        g = {'source_date': '2025-05-01'}
        records = [dict(pitcher_id='10', game_id=str(i), source_date=f'2025-04-{20+i}',
            scheduled_start=f'2025-04-{20+i}T17:00:00Z', completed_at=f'2025-04-{20+i}T19:00:00Z',
            plate_appearances=i, strikeouts=1, walks=0) for i in range(1,5)]
        result = m.history(g, '10', records, [], cutoff, True)
        self.assertEqual([r['game_id'] for r in result['appearances']], ['2','3','4'])
        self.assertEqual(result['strikeout_fraction'], 3/9)
        same_day = dict(records[-1], game_id='5', source_date='2025-05-01', scheduled_start='2025-05-01T01:00:00Z', completed_at='2025-05-01T03:00:00Z')
        future = dict(same_day, game_id='6', source_date='2025-05-02')
        self.assertEqual(m.history(g,'10',records+[same_day,future],[],cutoff,True), result)
        for end in ('2025-05-01T15:59:59Z', '2025-05-01T16:00:00Z', '2025-05-01T16:00:01Z'):
            changed = copy.deepcopy(records); changed[-1]['completed_at'] = end
            result = m.history(g,'10',changed,[],cutoff,True)
            self.assertEqual(result['refusal'], None if end.endswith('15:59:59Z') else 'prior_completion_at_or_after_cutoff')
        self.assertEqual(m.history(g,'10',records[:2],[],cutoff,True)['refusal'], 'fewer_than_three_prior_appearances')
