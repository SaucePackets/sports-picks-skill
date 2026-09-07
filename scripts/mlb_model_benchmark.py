#!/usr/bin/env python3
"""Frozen, synthetic-only MLB benchmark mechanics. No live or historical admission."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

SPEC = {
    'schema': 'mlb-benchmark-fixture-v1',
    'side': 'away', 'initial_rating': 1500, 'elo_scale': 400, 'k': 20,
    'home_advantage': 0, 'season_policy': 'single-season-reset',
    'training': 'completed-and-observed-strictly-before-cutoff',
    'evaluation': 'frozen-ratings-no-heldout-updates-UTC-date-separated',
    'minimum_prior_games_per_team': 1, 'calibration_bins': 10,
    'pitcher_context': 'unavailable',
}
FAMILIES = ('team_strength', 'market', 'pitcher_context')
GAME_KEYS = {'game_id', 'away_id', 'home_id', 'first_pitch'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def text(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def stamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must be text')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def game(value):
    if not isinstance(value, dict) or set(value) != GAME_KEYS:
        raise ValueError('game requires exactly identity and first_pitch')
    if any(not text(value[k]) for k in ('game_id', 'away_id', 'home_id')):
        raise ValueError('invalid game identity')
    if value['away_id'] == value['home_id']:
        raise ValueError('teams must differ')
    stamp(value['first_pitch'])
    return value


def corroborate(record, scheduled):
    supplied = game(record['game'])
    if any(supplied[k] != scheduled[k] for k in GAME_KEYS - {'first_pitch'}):
        raise ValueError('game identity mismatch')
    if stamp(supplied['first_pitch']) != stamp(scheduled['first_pitch']):
        raise ValueError('first pitch mismatch')
    if not text(record.get('source_id')):
        raise ValueError('source identity required')
    return stamp(record['observed_at'])


def final(record, scheduled):
    observed = corroborate(record, scheduled)
    completed = stamp(record['completed_at'])
    if not stamp(scheduled['first_pitch']) < completed <= observed:
        raise ValueError('invalid final chronology')
    scores = (record['away_score'], record['home_score'])
    if record.get('status') != 'Final' or any(type(s) is not int or s < 0 for s in scores):
        raise ValueError('invalid final scores/status')
    if scores[0] == scores[1]:
        raise ValueError('tie is not a settled moneyline')
    return int(scores[0] > scores[1]), observed


def elo_probability(away, home):
    return 1 / (1 + 10 ** ((home + SPEC['home_advantage'] - away) / SPEC['elo_scale']))


def train(records, cutoff):
    """Market-free adapter: only whitelisted historical final fields enter training."""
    ratings, counts, seen, ordered = {}, {}, set(), []
    required = {'game', 'source_id', 'observed_at', 'completed_at', 'status', 'away_score', 'home_score'}
    for record in records:
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError('training record fields must match contract; no market/features')
        scheduled = game(record['game'])
        key = scheduled['game_id']
        if key in seen:
            raise ValueError('duplicate training game')
        seen.add(key)
        outcome, observed = final(record, scheduled)
        start = stamp(scheduled['first_pitch'])
        if observed >= cutoff or start.year != cutoff.year:
            raise ValueError('training must be same-season and strictly before cutoff')
        ordered.append((stamp(record['completed_at']), key, scheduled, outcome))
    for _, _, scheduled, outcome in sorted(ordered):
        away, home = scheduled['away_id'], scheduled['home_id']
        a, h = (ratings.get(t, SPEC['initial_rating']) for t in (away, home))
        delta = SPEC['k'] * (outcome - elo_probability(a, h))
        ratings[away], ratings[home] = a + delta, h - delta
        for team in (away, home):
            counts[team] = counts.get(team, 0) + 1
    return ratings, counts, seen


def metrics(pairs):
    if not pairs:
        return {'n': 0, 'brier': None, 'log_loss': None, 'calibration': []}
    bins = []
    for index in range(SPEC['calibration_bins']):
        selected = [(p, y) for p, y in pairs if min(int(p * 10), 9) == index]
        bins.append({'lower': index / 10, 'upper': (index + 1) / 10,
                     'n': len(selected),
                     'mean_probability': sum(p for p, _ in selected) / len(selected) if selected else None,
                     'observed_frequency': sum(y for _, y in selected) / len(selected) if selected else None})
    return {'n': len(pairs), 'brier': sum((p-y)**2 for p, y in pairs) / len(pairs),
            'log_loss': -sum(math.log(max(min(p if y else 1-p, 1-1e-15), 1e-15)) for p, y in pairs) / len(pairs),
            'calibration': bins}


def indexed(records, schedule):
    if not isinstance(records, list):
        raise ValueError('observations must be arrays')
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('observation must be an object')
        key = game(record['game'])['game_id']
        if key not in schedule or key in result:
            raise ValueError('unmatched or duplicate observation')
        result[key] = record
    return result


def evaluate(data):
    if not isinstance(data, dict) or data.get('schema') != SPEC['schema']:
        raise ValueError('unknown fixture schema')
    if data.get('evidence_kind') != 'synthetic':
        raise ValueError('historical admission unavailable; only synthetic mechanics accepted')
    cutoff = stamp(data['training_cutoff'])
    if not isinstance(data['training'], list) or not isinstance(data['schedule'], list):
        raise ValueError('training and schedule must be arrays')
    ratings, counts, training_ids = train(data['training'], cutoff)
    schedule = {}
    for item in data['schedule']:
        item = game(item)
        key = item['game_id']
        start = stamp(item['first_pitch'])
        if key in schedule or key in training_ids:
            raise ValueError('duplicate or overlapping evaluation game')
        if start.date() <= cutoff.date() or start.year != cutoff.year:
            raise ValueError('held-out games must be later UTC dates in the same season')
        schedule[key] = item
    markets = indexed(data.get('markets', []), schedule)
    finals = indexed(data.get('finals', []), schedule)
    rows = []
    for key, item in sorted(schedule.items()):
        reasons = {family: [] for family in FAMILIES}
        predictions = dict.fromkeys(FAMILIES)
        away, home = item['away_id'], item['home_id']
        if all(counts.get(t, 0) >= SPEC['minimum_prior_games_per_team'] for t in (away, home)):
            predictions['team_strength'] = elo_probability(ratings[away], ratings[home])
        else:
            reasons['team_strength'].append('missing_prior_team_games')
        reasons['pitcher_context'].append('reproducible_pitcher_bullpen_context_unavailable')
        market = markets.get(key)
        if market is None:
            reasons['market'].append('missing_market')
        else:
            try:
                observed = corroborate(market, item)
                odds = market['odds']
                a, h = odds['away_decimal'], odds['home_decimal']
                if (observed >= stamp(item['first_pitch']) or not text(odds['book'])
                        or odds['market'] != 'full_game_moneyline_including_extras'
                        or not all(number(x) and x > 1 for x in (a, h))):
                    raise ValueError('invalid pregame same-book odds')
                predictions['market'] = (1/a) / (1/a + 1/h)
            except (ValueError, KeyError, TypeError):
                reasons['market'].append('invalid_market')
        outcome, outcome_reason = None, 'missing_final'
        if key in finals:
            try:
                outcome, _ = final(finals[key], item)
                outcome_reason = None
            except (ValueError, KeyError, TypeError):
                outcome_reason = 'invalid_final'
        rows.append({'game_id': key, 'predictions': predictions, 'missing': reasons,
                     'away_won': outcome, 'outcome_reason': outcome_reason, 'eligible': False})
    reports = {}
    common = [r for r in rows if r['away_won'] is not None and all(
        r['predictions'][f] is not None for f in ('team_strength', 'market'))]
    for family in FAMILIES:
        predicted = [r for r in rows if r['predictions'][family] is not None]
        scored = [r for r in predicted if r['away_won'] is not None]
        reports[family] = {'scheduled': len(rows), 'predicted': len(predicted),
                           'coverage': len(predicted)/len(rows) if rows else None,
                           'scores': metrics([(r['predictions'][family], r['away_won']) for r in scored]),
                           'refusal_funnel': {'missing_prediction': len(rows)-len(predicted),
                                              'missing_or_invalid_final_after_prediction': len(predicted)-len(scored),
                                              'scored_synthetic': len(scored)},
                           'qualifying_pick_count': None,
                           'qualification_reason': 'unavailable_conservative_probability_and_retained_selection_evidence',
                           'candidate_count': None, 'review_approved_count': None}
    return {'evidence_kind': 'synthetic_mechanics_only', 'historical_performance': None,
            'historical_evidence_reason': 'no_admitted_retained_historical_dataset',
            'eligible': False, 'live_eligible_count': 0,
            'spec': SPEC, 'spec_sha256': digest(canonical(SPEC)),
            'input_sha256': digest(canonical(data)),
            'implementation_sha256': digest(Path(__file__).read_bytes()),
            'training_cutoff': data['training_cutoff'], 'ratings': ratings,
            'synthetic_mechanics': reports, 'rows': rows,
            'paired_team_market': {'game_ids': [r['game_id'] for r in common],
                **{f: metrics([(r['predictions'][f], r['away_won']) for r in common])
                   for f in ('team_strength', 'market')}},
            'all_family_common_game_ids': [],
            'selection_policy': 'untouched; no policy loading or qualification implementation'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, type=Path)
    args = parser.parse_args()
    try:
        raw = args.fixture.read_bytes()
        result = evaluate(json.loads(raw))
        result['input_bytes_sha256'] = digest(raw)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f'invalid benchmark fixture: {exc}\n')


if __name__ == '__main__':
    main()
