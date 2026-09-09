"""Synthetic adversarial controls; retained replay supplies the real denominator."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mlb_appearance_census as m
from test_mlb_bulk_starter_admission import pitching_feed
from test_mlb_market_free_checkpoint import game, raw, schedule
from mlb_bulk_starter_admission import schedule_census


def example():
    g = game()
    parsed = schedule_census(schedule(g['officialDate'], [g]), g['officialDate'])[0]
    parsed.update(schedule_sha256='a'*64, split='train')
    f = pitching_feed(g)
    p = copy.deepcopy(f['liveData']['plays']['allPlays'][-1])
    p['about'].update(atBatIndex=2, isScoringPlay=False)
    p['result'].update(eventType='caught_stealing_2b', isOut=True)
    p['count'] = dict(balls=1, strikes=2, outs=3)
    p['playEvents'] = [{'isPitch': True, 'index': 0, 'details': {}}]
    p['runners'] = [{'movement': dict(start='1B', end=None, outBase='2B', isOut=True, outNumber=3),
        'details': dict(eventType='caught_stealing_2b', runner={'id': 777}, isScoringEvent=False, playIndex=0)}]
    f['liveData']['plays']['allPlays'].append(p)
    return parsed, f, p


class CensusTests(unittest.TestCase):
    def test_runner_out_does_not_add_batter_faced(self):
        g, f, p = example()
        with self.assertRaisesRegex(ValueError, 'unsupported_plate_appearance_event'):
            m.appearances(g, raw(f))
        result = m.appearances(g, raw(f), non_pa_classifier=m.runner_out)
        home = next(r for r in result if r['side'] == 'home')
        self.assertEqual((home['plate_appearances'], home['strikeouts'], home['walks']), (1, 1, 0))
        self.assertEqual(home['non_pa_play_pointers'], ['/liveData/plays/allPlays/2'])
        self.assertEqual(home['play_pointers'], ['/liveData/plays/allPlays/1'])
        self.assertEqual(len(m.play_inventory(raw(f))), 3)

    def test_retained_runner_projection_in_synthetic_envelope(self):
        fixture = m.decode((Path(__file__).parent/'fixtures/mlb_778557_runner_out.json').read_bytes())
        self.assertTrue(m.runner_out(fixture['play']))
        g, f, p = example()
        retained = copy.deepcopy(fixture['play'])
        retained['about'].update(atBatIndex=2, startTime=p['about']['startTime'],
                                 endTime=p['about']['endTime'], isTopInning=True)
        retained['matchup']['pitcher']['id'] = 20
        retained['result'].update(awayScore=p['result']['awayScore'], homeScore=p['result']['homeScore'])
        f['liveData']['plays']['allPlays'][-1] = retained
        rows = m.appearances(g, raw(f), non_pa_classifier=m.runner_out)
        self.assertEqual(sum(r['plate_appearances'] for r in rows), 2)
        retained['runners'][0]['details']['runner']['id'] = retained['matchup']['batter']['id']
        with self.assertRaisesRegex(ValueError, 'non_pa_batter_or_origin_conflict'):
            m.appearances(g, raw(f), non_pa_classifier=m.runner_out)

    def test_explicit_runner_event_semantics(self):
        for event in m.RUNNER_OUT_EVENTS:
            with self.subTest(event=event):
                _, _, p = example()
                p['result']['eventType'] = event
                p['runners'][0]['details']['eventType'] = event
                p['runners'][0]['movement']['outBase'] = ('4B' if event.endswith('home') else event[-2:].upper())
                self.assertTrue(m.runner_out(p))
        for event in ('other_out', 'wild_pitch', 'stolen_base_2b', 'pickoff_error_1b', 'unknown'):
            _, _, p = example(); p['result']['eventType'] = event
            self.assertFalse(m.runner_out(p))

    def test_insufficient_runner_evidence_refuses(self):
        mutations = [lambda p: p['count'].update(strikes=3), lambda p: p['count'].update(balls=4),
            lambda p: p['count'].update(outs=2), lambda p: p['count'].update(balls=True),
            lambda p: p['result'].update(isOut=False), lambda p: p['about'].update(isScoringPlay=True),
            lambda p: p.update(runners=[]),
            lambda p: p['runners'][0]['movement'].update(start=None),
            lambda p: p['runners'][0]['movement'].update(outNumber=2),
            lambda p: p['runners'][0]['movement'].update(isOut=False),
            lambda p: p['runners'][0]['details']['runner'].update(id=p['matchup']['batter']['id']),
            lambda p: p['runners'][0]['details'].update(eventType='other_out'),
            lambda p: p['runners'][0]['details'].update(playIndex=20),
            lambda p: p['runners'][0]['details'].update(isScoringEvent=True),
            lambda p: p['playEvents'][0].update(index=1),
            lambda p: p['runners'][0]['movement'].update(outBase='3B'),
            lambda p: p['playEvents'][0]['details'].update(eventType='strikeout'),
            lambda p: p['playEvents'][0]['details'].update(isInPlay=True)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                g, f, p = example(); mutate(p)
                with self.assertRaises((ValueError, KeyError)):
                    m.appearances(g, raw(f), non_pa_classifier=m.runner_out)
                self.assertEqual(m.play_inventory(raw(f))[-1]['classification'], 'insufficient_evidence')

    def test_complete_pitcher_accounting_and_unchanged_gates(self):
        mutations = [
            lambda f,p: f['liveData']['boxscore']['teams']['home']['players']['ID20']['stats']['pitching'].update(battersFaced=2),
            lambda f,p: p['matchup']['pitcher'].update(id=99),
            lambda f,p: p['about'].update(isTopInning=False),
            lambda f,p: p['about'].update(startTime='2025-04-21T00:00:00Z'),
            lambda f,p: f['gameData']['datetime'].update(officialDate='2025-04-22'),
            lambda f,p: p['about'].update(isComplete=False),
            lambda f,p: p['about'].update(atBatIndex=99),
            lambda f,p: p['playEvents'].append({'isPitch': False, 'details': {'eventType':'pitching_substitution'}, 'player': {'id':20}}),
            lambda f,p: p['playEvents'].insert(0, {'isPitch': False, 'details': {'eventType':'pitching_substitution'}, 'player': {'id':99}}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                g,f,p = example(); mutate(f,p)
                with self.assertRaises((ValueError, KeyError)):
                    m.appearances(g, raw(f), non_pa_classifier=m.runner_out)

    def test_zero_pa_pitcher_is_not_silently_dropped(self):
        g,f,p = example()
        box = f['liveData']['boxscore']['teams']['home']
        box['pitchers'].append(30)
        box['players']['ID30'] = {'person': {'id':30}, 'stats': {'pitching': dict(battersFaced=0,strikeOuts=0,baseOnBalls=0)}}
        p['matchup']['pitcher']['id']=30
        with self.assertRaisesRegex(ValueError, 'appearance_without_completed_pa'):
            m.appearances(g, raw(f), non_pa_classifier=m.runner_out)

    def test_hidden_later_failure_blocks_whole_game_and_has_pointer(self):
        g,f,p = example()
        later=copy.deepcopy(p); later['about']['atBatIndex']=3; later['result']['eventType']='unknown'
        f['liveData']['plays']['allPlays'].append(later)
        pair=({'body_sha256':m.sha(raw(f))},raw(f))
        records, reason, trace=m.inspect_appearance(g,pair,True,{g['game_id']:1},m.runner_out)
        self.assertIsNone(records)
        self.assertEqual(reason,'unsupported_plate_appearance_event')
        self.assertEqual(trace['feed_pointer'],'/liveData/plays/allPlays/3')
        self.assertEqual(len(m.play_inventory(raw(f))),4)

    def test_baseline_and_denominator_cannot_be_replaced(self):
        g,f,p=example(); pair=({'body_sha256':m.sha(raw(f))},raw(f)); src={('feed',g['game_id']):pair}
        with self.assertRaisesRegex(ValueError,'baseline_occurrence_replay_mismatch'):
            m.classify([g],src,True,{'appearance_games':[]})
        baseline={'appearance_games':[dict(game_id=g['game_id'],source_date=g['source_date'],split='train',
            refusal='unsupported_plate_appearance_event',corroborated_appearances=None)]}
        with self.assertRaisesRegex(ValueError,'baseline_denominator_mismatch'):
            m.classify([g],src,True,baseline)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'baseline.json';path.write_bytes(raw(baseline))
            with patch.object(m,'plan',side_effect=AssertionError('must not load bundle')):
                with self.assertRaisesRegex(ValueError,'baseline_replay_integrity'):
                    m.census(root,path)

    def test_duplicate_occurrences_remain_separate_and_refused(self):
        g,f,p=example(); pair=({'body_sha256':m.sha(raw(f))},raw(f))
        for classifier in (None,m.runner_out):
            records,reason,trace=m.inspect_appearance(g,pair,True,{g['game_id']:2},classifier)
            self.assertIsNone(records);self.assertEqual(reason,'repeated_game_across_source_dates')
            self.assertEqual(trace,{'stage':'schedule','feed_pointer':None})
