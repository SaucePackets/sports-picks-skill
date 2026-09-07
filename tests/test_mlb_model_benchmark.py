"""Negative-path and known-answer tests for synthetic benchmark mechanics."""
import copy
import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

from scripts import mlb_model_benchmark as benchmark

FIXTURE = Path(__file__).parent / 'fixtures/benchmark/synthetic.json'


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text())

    def test_known_elo_and_score(self):
        report = benchmark.evaluate(self.data)
        self.assertEqual(report['ratings'], {'A': 1510, 'B': 1490})
        p = 1/(1+10**(-20/400))
        team = report['synthetic_mechanics']['team_strength']
        self.assertAlmostEqual(report['rows'][0]['predictions']['team_strength'], p)
        self.assertAlmostEqual(team['scores']['brier'], (p-1)**2)
        self.assertAlmostEqual(team['scores']['log_loss'], -math.log(p))
        self.assertEqual(sum(b['n'] for b in team['scores']['calibration']), 1)
        self.assertEqual(team['predicted'], 2)
        self.assertEqual(team['scheduled'], 3)
        self.assertEqual(team['refusal_funnel'], {'missing_prediction': 1,
                         'missing_or_invalid_final_after_prediction': 1, 'scored_synthetic': 1})
        self.assertIsNone(team['qualifying_pick_count'])
        self.assertIsNone(report['historical_performance'])
        self.assertFalse(report['eligible'])
        self.assertTrue(all(not row['eligible'] for row in report['rows']))
        self.assertEqual(report['paired_team_market']['game_ids'], ['eval-1'])
        self.assertEqual(report['all_family_common_game_ids'], [])

    def test_market_and_outcome_cannot_train_team(self):
        before = benchmark.evaluate(self.data)
        self.data['markets'][0]['odds']['away_decimal'] = 10
        self.data['finals'][0]['away_score'] = 0
        after = benchmark.evaluate(self.data)
        self.assertEqual(before['ratings'], after['ratings'])
        self.assertEqual(before['rows'][0]['predictions']['team_strength'], after['rows'][0]['predictions']['team_strength'])
        self.assertNotEqual(before['rows'][0]['predictions']['market'], after['rows'][0]['predictions']['market'])
        self.assertNotEqual(before['synthetic_mechanics']['team_strength']['scores'], after['synthetic_mechanics']['team_strength']['scores'])
        self.assertNotEqual(before['input_sha256'], after['input_sha256'])

    def test_historical_cannot_be_enabled(self):
        self.data['evidence_kind'] = 'historical'
        with self.assertRaises(ValueError):
            benchmark.evaluate(self.data)

    def test_cutoff_and_season_negative_paths(self):
        for value in ('2026-04-02T12:00:00Z', '2026-04-03T00:00:00Z', '2026-04-01T23:00:00-13:00'):
            with self.subTest(value=value):
                data = copy.deepcopy(self.data)
                data['training'][0]['observed_at'] = value
                with self.assertRaises(ValueError):
                    benchmark.evaluate(data)
        for value in ('2026-04-02T23:00:00Z', '2027-04-03T18:00:00Z'):
            data = copy.deepcopy(self.data)
            data['schedule'][0]['first_pitch'] = value
            with self.assertRaises(ValueError):
                benchmark.evaluate(data)

    def test_no_market_fields_in_training(self):
        self.data['training'][0]['market_probability'] = .9
        with self.assertRaises(ValueError):
            benchmark.evaluate(self.data)

    def test_duplicates_overlap_unmatched_fail_closed(self):
        for collection in ('training', 'schedule', 'markets', 'finals'):
            data = copy.deepcopy(self.data)
            data[collection].append(copy.deepcopy(data[collection][0]))
            with self.subTest(collection=collection), self.assertRaises(ValueError):
                benchmark.evaluate(data)
        self.data['schedule'][0]['game_id'] = 'train-1'
        with self.assertRaises(ValueError):
            benchmark.evaluate(self.data)

    def test_invalid_market_retains_coverage_and_team_prediction(self):
        for value in (True, float('nan'), float('inf'), 10**400, 1, '2'):
            data = copy.deepcopy(self.data)
            data['markets'][0]['odds']['away_decimal'] = value
            # NaN cannot be hashed: malformed JSON numbers must fail the whole contract.
            if isinstance(value, float) and not math.isfinite(value):
                with self.assertRaises(ValueError):
                    benchmark.evaluate(data)
                continue
            row = benchmark.evaluate(data)['rows'][0]
            self.assertEqual(row['missing']['market'], ['invalid_market'])
            self.assertIsNotNone(row['predictions']['team_strength'])
        for field, value in (('observed_at', '2026-04-03T18:00:00Z'), ('source_id', '')):
            data = copy.deepcopy(self.data)
            data['markets'][0][field] = value
            self.assertEqual(benchmark.evaluate(data)['rows'][0]['missing']['market'], ['invalid_market'])

    def test_final_identity_and_time_are_corroborated(self):
        for mutate in ('swap', 'time', 'bool'):
            data = copy.deepcopy(self.data)
            record = data['finals'][0]
            if mutate == 'swap':
                record['game']['away_id'], record['game']['home_id'] = 'B', 'A'
            elif mutate == 'time':
                record['completed_at'] = '2026-04-03T17:00:00Z'
            else:
                record['away_score'] = True
            report = benchmark.evaluate(data)
            self.assertEqual(report['rows'][0]['outcome_reason'], 'invalid_final')
            self.assertEqual(report['synthetic_mechanics']['team_strength']['scores']['n'], 0)

    def test_training_order_is_canonical_and_unseen_team_stays_missing(self):
        second = copy.deepcopy(self.data['training'][0])
        second['game']['game_id'] = 'train-2'
        second['away_score'], second['home_score'] = 1, 3
        self.data['training'].append(second)
        before = benchmark.evaluate(self.data)
        self.data['training'].reverse()
        after = benchmark.evaluate(self.data)
        self.assertEqual(before['ratings'], after['ratings'])
        self.assertEqual(before['rows'], after['rows'])
        self.assertIsNone(after['rows'][2]['predictions']['team_strength'])
        self.assertEqual(after['synthetic_mechanics']['pitcher_context']['predicted'], 0)

    def test_empty_input_has_no_scores(self):
        for key in ('training', 'schedule', 'markets', 'finals'):
            self.data[key] = []
        report = benchmark.evaluate(self.data)
        self.assertIsNone(report['synthetic_mechanics']['team_strength']['coverage'])
        self.assertEqual(report['paired_team_market']['team_strength']['n'], 0)
        self.assertIsNone(report['paired_team_market']['team_strength']['brier'])

    def test_cli_fixture_has_explicit_evidence_label(self):
        result = subprocess.run([sys.executable, str(Path(benchmark.__file__)), '--fixture', str(FIXTURE)],
                                capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report['evidence_kind'], 'synthetic_mechanics_only')
        self.assertEqual(report['input_bytes_sha256'], benchmark.digest(FIXTURE.read_bytes()))


if __name__ == '__main__':
    unittest.main()
