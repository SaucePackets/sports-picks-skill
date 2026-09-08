"""Adversarial schema/mechanism tests; synthetic data is not historical evidence."""
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import mlb_market_free_checkpoint as m


def raw(value):
    return json.dumps(value).encode()


def game(day='2025-04-21', gid=100, away=1, home=2):
    return {'gamePk': gid, 'officialDate': day, 'gameDate': day+'T17:00:00Z',
            'gameType': 'R', 'status': {'abstractGameState': 'Final', 'detailedState': 'Final'},
            'teams': {s: {'team': {'id': i, 'name': s}, 'score': score}
                      for s, i, score in [('away', away, 2), ('home', home, 1)]}}


def schedule(day, games):
    return raw({'totalGames': len(games), 'dates': [{'date': day, 'totalGames': len(games), 'games': games}]})


def feed(g):
    day = g['officialDate']
    return {'gamePk': g['gamePk'], 'gameData': {
        'game': {'pk': g['gamePk'], 'type': 'R', 'season': '2025'},
        'datetime': {'dateTime': g['gameDate'], 'officialDate': day, 'originalDate': day},
        'status': g['status'].copy(),
        'teams': {s: {'id': g['teams'][s]['team']['id'], 'abbreviation': s.upper()} for s in ('away','home')},
        'players': {'ID10': {'id': 10}, 'ID20': {'id': 20}}},
        'liveData': {'linescore': {'teams': {s: {'runs': g['teams'][s]['score']} for s in ('away','home')}},
                     'plays': {'allPlays': [{'about': {'atBatIndex': 0, 'isComplete': True,
                                                     'startTime': day+'T19:00:00Z', 'endTime': day+'T19:01:00Z'},
                                            'result': {'awayScore': 2, 'homeScore': 1},
                                            'matchup': {'batter': {'id': 10}, 'pitcher': {'id': 20}}}]}}}


FIELDS = 'game_pk,game_date,game_type,home_team,away_team,batter,pitcher,at_bat_number,pitch_number,plate_x,plate_z'.split(',')
PITCH = ['100', '2025-04-21', 'R', 'HOME', 'AWAY', '10', '20', '1', '1', '', '2.5']


def pitches(rows=None):
    buf = io.StringIO()
    w = csv.writer(buf); w.writerow(FIELDS); w.writerows(rows if rows is not None else [PITCH])
    return buf.getvalue().encode()


def fixture():
    sources = {}
    for day in m.SPEC['training_dates']+[m.SPEC['evaluation_date']]:
        games = [game()] if day == '2025-04-21' else [game(day, 200)] if day == m.SPEC['evaluation_date'] else []
        b = schedule(day, games)
        sources['schedule', day] = ({'body_sha256': m.sha(b)}, b)
    b = raw(feed(game()))
    sources['feed', '100'] = ({'body_sha256': m.sha(b)}, b)
    b = pitches()
    sources['savant', '2025-04-21'] = ({'body_sha256': m.sha(b)}, b)
    return sources


def run(sources):
    with patch.object(m, 'load_bundle', return_value=('a'*64, sources)):
        return m.checkpoint(Path('.'))


def replace_feed(sources, fn):
    d = json.loads(sources['feed','100'][1]); fn(d)
    b = raw(d); sources['feed','100'] = ({'body_sha256': m.sha(b)}, b)


class ReconstructionTests(unittest.TestCase):
    def test_baselines_and_lineage(self):
        r = run(fixture())
        self.assertTrue(r['checkpoint_complete'])
        self.assertEqual(r['team_ratings'], {'1': 1510.0, '2': 1490.0})
        self.assertAlmostEqual(r['predictions'][0]['empirical_home_probability'], 1/3)
        self.assertGreater(r['predictions'][0]['elo_away_probability'], .5)
        self.assertEqual(r['savant']['joined_count'], 1)
        self.assertIsNone(r['savant']['rows'][0]['coordinates']['plate_x'])
        self.assertFalse(r['eligible']); self.assertFalse(r['asof_snapshot_verified'])
        self.assertIsNone(r['historical_performance'])

    def test_evaluation_scores_do_not_enter_predictions(self):
        s = fixture(); before = run(s)
        g = game(m.SPEC['evaluation_date'], 200)
        g['teams']['away']['score'] = 999
        b = schedule(m.SPEC['evaluation_date'], [g]); s['schedule', m.SPEC['evaluation_date']] = ({'body_sha256': m.sha(b)}, b)
        after = run(s)
        for k in ('elo_away_probability','empirical_home_probability'):
            self.assertEqual(before['predictions'][0][k], after['predictions'][0][k])
        self.assertEqual(before['team_ratings'], after['team_ratings'])

    def test_savant_coordinates_do_not_enter_predictions(self):
        s = fixture(); before = run(s)
        p = PITCH.copy(); p[-2:] = ['100.25', '-5']
        b = pitches([p]); s['savant','2025-04-21'] = ({'body_sha256':m.sha(b)},b)
        self.assertEqual(before['predictions'], run(s)['predictions'])

    def test_feed_identity_score_and_completion_refusals(self):
        mutations = [lambda d:d.update(gamePk=101),
                     lambda d:d['gameData']['teams']['away'].update(id=99),
                     lambda d:d['liveData']['linescore']['teams']['away'].update(runs=9),
                     lambda d:d['liveData']['plays']['allPlays'][-1]['result'].update(awayScore=9),
                     lambda d:d['liveData']['plays']['allPlays'][-1]['about'].update(endTime=m.SPEC['cutoff']),
                     lambda d:d['liveData']['plays']['allPlays'][-1]['about'].update(isComplete=False),
                     lambda d:d['gameData']['datetime'].update(resumeDate='2025-04-22')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                s = fixture(); replace_feed(s,mutation); r=run(s)
                self.assertEqual(len(r['training']),0); self.assertEqual(len(r['training_refusals']),1)
                self.assertEqual(len(r['predictions']),1)
                self.assertEqual(r['predictions'][0]['status'],'refused')

    def test_repeated_training_game_refuses_all_occurrences(self):
        s=fixture(); day='2025-04-22'; b=schedule(day,[game(day)])
        s['schedule',day]=({'body_sha256':m.sha(b)},b); r=run(s)
        self.assertEqual(len(r['training']),0); self.assertEqual(len(r['training_refusals']),2)
        self.assertEqual({x['reason'] for x in r['training_refusals']},{'repeated_game_across_source_dates'})

    def test_missing_training_feed_and_savant_are_preserved(self):
        s=fixture(); del s['feed','100']; s['savant','2025-04-21']=({'body_sha256':None},None)
        r=run(s); self.assertFalse(r['checkpoint_complete']); self.assertEqual(len(r['training_refusals']),1)
        self.assertEqual(len(r['predictions']),1); self.assertIn('savant_lineage_incomplete',r['blockers'])

    def test_missing_schedule_is_unknown_not_zero(self):
        s=fixture(); s['schedule','2025-04-22']=({'body_sha256':None},None)
        r=run(s); self.assertIsNone(r['schedule_counts']['2025-04-22'])
        self.assertFalse(r['checkpoint_complete'])
        self.assertIsNone(r['predictions'][0]['empirical_home_probability'])

    def test_untrained_team_keeps_empirical_prediction(self):
        s=fixture(); day=m.SPEC['evaluation_date']; b=schedule(day,[game(day,200,3,4)])
        s['schedule',day]=({'body_sha256':m.sha(b)},b); r=run(s)['predictions'][0]
        self.assertIsNone(r['elo_away_probability']); self.assertAlmostEqual(r['empirical_home_probability'],1/3)

    def test_evaluation_overlap_cannot_predict(self):
        s=fixture(); day=m.SPEC['evaluation_date']; b=schedule(day,[game(day,100)])
        s['schedule',day]=({'body_sha256':m.sha(b)},b)
        self.assertIn('evaluation_overlaps_training',run(s)['predictions'][0]['elo_refusals'])

    def test_savant_id_date_team_player_matchup_mutations(self):
        for field,value in [('game_pk','999'),('game_date','2025-04-22'),('home_team','XXX'),
                            ('batter','20'),('pitcher','99'),('at_bat_number','2'),
                            ('plate_x','nan'),('game_pk','100.0')]:
            with self.subTest(field=field):
                s=fixture(); p=PITCH.copy();p[FIELDS.index(field)]=value
                b=pitches([p]);s['savant','2025-04-21']=({'body_sha256':m.sha(b)},b)
                r=run(s);self.assertFalse(r['checkpoint_complete']); self.assertEqual(r['savant']['rows'][0]['status'],'refused')

    def test_duplicate_pitch_retained_and_blocked(self):
        s=fixture();b=pitches([PITCH,PITCH]);s['savant','2025-04-21']=({'body_sha256':m.sha(b)},b)
        r=run(s); self.assertEqual(r['savant']['row_count'],2)
        self.assertEqual(r['savant']['rows'][1]['reason'],'duplicate_pitch_key')
        self.assertFalse(r['checkpoint_complete'])

    def test_bad_census_aborts(self):
        for data in [dict(totalGames=2, dates=[]),dict(totalGames=2,dates=[dict(date='2025-04-21',totalGames=2,games=[game(),game()])])]:
            with self.assertRaises(ValueError):m.schedule_census(raw(data),'2025-04-21')

    def test_frozen_completion_order_numeric_tie_break(self):
        rows=[dict(game_id=str(i),away_id='1',home_id='2',completed_at='2025-04-21T20:00:00Z',away_won=y)
              for i,y in [(10,0),(2,1)]]
        a=m.train(rows);b=m.train(list(reversed(rows)))
        self.assertEqual(a,b);self.assertEqual([r['game_id'] for r in a[2]],['2','10'])
        rows[0]['completed_at']='2025-04-21T19:00:00Z'
        self.assertNotEqual(a[0],m.train(rows)[0])


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'objects').mkdir()
        self.entries=[]
        for (kind,key),(entry,body) in fixture().items():
            digest=m.sha(body);(self.root/'objects'/digest).write_bytes(body)
            if kind=='schedule':url=f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={key}&hydrate=linescore'
            elif kind=='feed':url=f'https://statsapi.mlb.com/api/v1.1/game/{key}/feed/live'
            else:url='https://baseballsavant.mlb.com/statcast_search/csv?all=true&type=details&player_type=pitcher&hfGT=R%7C&game_date_gt=2025-04-21&game_date_lt=2025-04-21&group_by=name&min_pitches=0&min_results=0'
            self.entries.append(dict(kind=kind,key=key,url=url,body_sha256=digest,size=len(body),http_status=200,failure=None,retrieved_at_local='2026-09-08T00:00:00Z'))
        self.save()

    def save(self):
        (self.root/'manifest.json').write_bytes(raw(dict(schema=1,sources=self.entries)))

    def test_valid_and_deterministic(self):
        self.assertEqual(m.checkpoint(self.root),m.checkpoint(self.root))

    def test_changed_source_aborts(self):
        (self.root/'objects'/self.entries[0]['body_sha256']).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'source_integrity'):m.checkpoint(self.root)

    def test_duplicate_slot_aborts(self):
        self.entries.append(self.entries[0]);self.save()
        with self.assertRaisesRegex(ValueError,'duplicate_source'):m.checkpoint(self.root)

    def test_wrong_url_aborts(self):
        self.entries[0]['url']='https://example.com/fake';self.save()
        with self.assertRaisesRegex(ValueError,'schedule_url'):m.checkpoint(self.root)

    def test_missing_slot_aborts(self):
        self.entries.pop(0);self.save()
        with self.assertRaisesRegex(ValueError,'missing_schedule_slot'):m.checkpoint(self.root)

    def test_symlink_aborts(self):
        p=self.root/'objects'/self.entries[0]['body_sha256'];b=p.read_bytes();p.unlink()
        other=self.root/'other';other.write_bytes(b);p.symlink_to(other)
        with self.assertRaisesRegex(ValueError,'object_symlink'):m.checkpoint(self.root)

    def test_digest_traversal_aborts(self):
        self.entries[0]['body_sha256']='../manifest.json';self.save()
        with self.assertRaisesRegex(ValueError,'invalid_digest'):m.checkpoint(self.root)

    def test_duplicate_json_key_aborts(self):
        (self.root/'manifest.json').write_bytes(b'{"schema":1,"schema":1,"sources":[]}')
        with self.assertRaisesRegex(ValueError,'duplicate_json_key'):m.checkpoint(self.root)


if __name__ == '__main__':
    unittest.main()
