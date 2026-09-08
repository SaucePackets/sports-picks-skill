"""Synthetic schema/mutation tests only; no test input establishes real trust."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'mlb_real_shadow_capture.py'
spec = importlib.util.spec_from_file_location('real_shadow', MODULE)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class RealShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'objects').mkdir()
        self.day = '2026-09-07'
        self.game = {'gamePk': 123, 'officialDate': self.day, 'gameDate': self.day + 'T18:00:00Z',
                     'status': {'abstractGameState': 'Final', 'detailedState': 'Final'},
                     'teams': {s: {'team': {'id': i, 'name': n}, 'score': score}
                               for s, i, n, score in [('away', 1, 'Away', 4), ('home', 2, 'Home', 2)]}}
        self.schedule = {'totalGames': 1, 'dates': [{'date': self.day, 'totalGames': 1, 'games': [self.game]}]}
        self.market = {'events': [{'id': 'espn-123', 'competitions': [{'date': self.game['gameDate'],
                         'competitors': [{'homeAway': s, 'team': {'displayName': n}}
                                         for s, n in [('away', 'Away'), ('home', 'Home')]], 'odds': [{}]}]}]}
        self.final = {'gamePk': 123, 'gameData': {'game': {'pk': 123},
                      'teams': {'away': {'id': 1}, 'home': {'id': 2}},
                      'datetime': {'dateTime': self.game['gameDate'], 'officialDate': self.day},
                      'status': self.game['status']},
                      'liveData': {'linescore': {'teams': {'away': {'runs': 4}, 'home': {'runs': 2}}}}}
        self.manifest = {'schema': 1, 'slate_date': self.day, 'sources': [
            self.entry('schedule', self.schedule, 'mlb_statsapi', 'v1',
                       f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={self.day}&hydrate=linescore'),
            self.entry('markets', self.market, 'espn_scoreboard', 'v2',
                       'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates=20260907&limit=100'),
            self.entry('timestamp', None, 'unconfigured', 'unconfigured', 'unavailable'),
            self.entry('final', self.final, 'mlb_statsapi', 'v1.1',
                       'https://statsapi.mlb.com/api/v1.1/game/123/feed/live', '123')]}

    def entry(self, kind, value, source, version, url, game_id=None):
        return {'kind': kind, 'game_id': game_id, 'source_id': source, 'version': version, 'url': url,
                'body_sha256': self.put(value) if value is not None else None,
                'retrieved_at_local': None, 'failure': None if value is not None else 'not_acquired'}

    def put(self, value):
        raw = json.dumps(value).encode()
        key = capture.sha(raw)
        (self.root / 'objects' / key).write_bytes(raw)
        return key

    def change(self, slot, value):
        self.manifest['sources'][slot]['body_sha256'] = self.put(value)

    def run_report(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))
        return capture.report(self.root)

    def row(self):
        return self.run_report()['rows'][0]

    def test_full_source_census_never_admits(self):
        result = self.run_report()
        self.assertEqual(result['scheduled_games'], 1)
        self.assertEqual(result['refused_games'], 1)
        self.assertFalse(result['dry_run_pass'])
        self.assertFalse(result['eligible'])
        self.assertEqual(result['admitted_game_ids'], [])
        row = result['rows'][0]
        self.assertFalse(row['eligible'])
        self.assertEqual(row['final']['status'], 'structurally_corroborated_only')
        self.assertEqual(row['market']['status'], 'candidate_only')
        self.assertTrue(set(capture.BASE_REFUSALS) <= set(row['refusals']))

    def test_absent_schedule_is_unknown_not_zero(self):
        self.manifest['sources'][0].update(body_sha256=None, failure='fetch_failed')
        result = self.run_report()
        self.assertIsNone(result['scheduled_games'])
        self.assertIn('schedule_source_unavailable', result['refusals'])

    def test_zero_game_day_still_untrusted(self):
        self.change(0, {'totalGames': 0, 'dates': []})
        self.manifest['sources'].pop()
        result = self.run_report()
        self.assertEqual(result['scheduled_games'], 0)
        self.assertFalse(result['dry_run_pass'])

    def test_missing_market_and_final_preserve_denominator(self):
        self.manifest['sources'][1].update(body_sha256=None, failure='access_unavailable')
        self.manifest['sources'].pop()
        row = self.row()
        self.assertIn('market_source_unavailable', row['refusals'])
        self.assertIn('final_source_unavailable', row['refusals'])

    def test_all_bytes_rehashed_before_parsing(self):
        path = self.root / 'objects' / self.manifest['sources'][1]['body_sha256']
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'changed_source_bytes'):
            self.run_report()

    def test_missing_blob_is_integrity_error(self):
        (self.root / 'objects' / self.manifest['sources'][3]['body_sha256']).unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_report()

    def test_symlink_rejected(self):
        path = self.root / 'objects' / self.manifest['sources'][1]['body_sha256']
        raw = path.read_bytes()
        target = self.root / 'target'
        target.write_bytes(raw)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'source_symlink'):
            self.run_report()

    def test_digest_path_traversal_rejected(self):
        self.manifest['sources'][1]['body_sha256'] = '../target'
        with self.assertRaisesRegex(ValueError, 'invalid_source_digest'):
            self.run_report()

    def test_caller_cannot_supply_trust_boolean(self):
        for field in ('eligible', 'provider_authenticated', 'timestamp_verified'):
            with self.subTest(field=field):
                self.manifest['sources'][2][field] = True
                with self.assertRaisesRegex(ValueError, 'invalid_contract_shape'):
                    self.run_report()
                del self.manifest['sources'][2][field]

    def test_fixture_or_forged_receipt_is_opaque_not_trusted(self):
        self.manifest['sources'][2].update(body_sha256=self.put({
            'purpose': 'mlb-shadow-fixture-only', 'verified': True,
            'observed_at': '2020-01-01T00:00:00Z', 'signature': 'fake'}), failure=None)
        row = self.row()
        self.assertEqual(row['timestamp']['status'], 'unverified')
        self.assertIsNone(row['timestamp']['trusted_time'])
        self.assertFalse(row['eligible'])

    def test_local_time_cannot_establish_pregame_or_lateness(self):
        for time in ('2020-01-01T00:00:00Z', '2030-01-01T00:00:00Z'):
            self.manifest['sources'][0]['retrieved_at_local'] = time
            row = self.row()
            self.assertIsNone(row['timing']['capture_late'])
            self.assertIn('pregame_asof_unestablished', row['refusals'])

    def test_schedule_mutations_cannot_become_complete(self):
        variants = []
        bad = copy.deepcopy(self.schedule); bad['totalGames'] = 2; variants.append(bad)
        bad = copy.deepcopy(self.schedule); bad['dates'][0]['date'] = '2026-09-06'; variants.append(bad)
        bad = copy.deepcopy(self.schedule); bad['dates'][0]['games'][0]['gamePk'] = True; variants.append(bad)
        bad = copy.deepcopy(self.schedule); bad['dates'][0]['games'][0]['teams']['home']['team']['id'] = 1; variants.append(bad)
        bad = copy.deepcopy(self.schedule); bad['dates'][0]['games'] *= 2; bad['totalGames'] = bad['dates'][0]['totalGames'] = 2; variants.append(bad)
        for bad in variants:
            with self.subTest(bad=bad):
                self.change(0, bad)
                result = self.run_report()
                self.assertIsNone(result['scheduled_games'])
                self.assertEqual(result['rows'], [])

    def test_final_same_id_does_not_override_sides_or_scores(self):
        variants = []
        bad = copy.deepcopy(self.final); bad['gameData']['teams']['away']['id'] = 2; variants.append((bad, 'final_identity_mismatch'))
        bad = copy.deepcopy(self.final); bad['liveData']['linescore']['teams']['away']['runs'] = 5; variants.append((bad, 'conflicting_final_scores'))
        bad = copy.deepcopy(self.final); bad['gameData']['datetime']['dateTime'] = '2026-09-07T19:00Z'; variants.append((bad, 'final_schedule_mismatch'))
        bad = copy.deepcopy(self.final); bad['liveData']['linescore']['teams']['away']['runs'] = True; variants.append((bad, 'invalid_final_scores'))
        bad = copy.deepcopy(self.final); bad['gameData']['status']['detailedState'] = 'Suspended'; variants.append((bad, 'final_not_complete_in_both_sources'))
        for bad, reason in variants:
            with self.subTest(reason=reason):
                self.change(3, bad)
                self.assertEqual(self.row()['final']['reason'], reason)

    def test_duplicate_market_candidates_are_ambiguous(self):
        self.market['events'] *= 2
        self.change(1, self.market)
        self.assertEqual(self.row()['market']['reason'], 'market_identity_missing_or_ambiguous')

    def test_market_time_mismatch_not_joined_by_name(self):
        self.market['events'][0]['competitions'][0]['date'] = '2026-09-07T23:00Z'
        self.change(1, self.market)
        self.assertEqual(self.row()['market']['reason'], 'market_identity_missing_or_ambiguous')

    def test_invalid_source_shapes_preserve_game(self):
        for value in ([], None, {'events': None}):
            self.change(1, value)
            self.assertEqual(self.row()['market']['reason'], 'invalid_market_source')
        self.change(3, [])
        self.assertEqual(self.row()['final']['reason'], 'invalid_final_source')

    def test_duplicate_final_not_silently_selected(self):
        self.manifest['sources'].append(copy.deepcopy(self.manifest['sources'][3]))
        with self.assertRaisesRegex(ValueError, 'duplicate_final_attempt'):
            self.run_report()

    def test_outside_slate_final_rejected(self):
        self.manifest['sources'][3]['game_id'] = '999'
        with self.assertRaisesRegex(ValueError, 'final_outside_declared_slate'):
            self.run_report()

    def test_url_label_not_provider_authentication(self):
        self.manifest['sources'][0]['url'] = 'https://example.com/schedule'
        self.assertIn('unsupported_schedule_source', self.run_report()['refusals'])

    def test_json_ambiguity_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'):
            with self.assertRaises(ValueError):
                capture.decode(raw)

    def test_replay_is_identical_and_read_only(self):
        first = self.run_report()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        second = capture.report(self.root)
        after = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_cli_refusal_and_integrity_exit_codes(self):
        self.run_report()
        command = [sys.executable, str(MODULE), '--bundle', str(self.root)]
        run = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertFalse(json.loads(run.stdout)['dry_run_pass'])
        (self.root / 'objects' / self.manifest['sources'][0]['body_sha256']).write_text('{}')
        run = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertEqual(json.loads(run.stdout)['status'], 'invalid_bundle')


if __name__ == '__main__':
    unittest.main()
