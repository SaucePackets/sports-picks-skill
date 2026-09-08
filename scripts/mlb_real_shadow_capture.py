#!/usr/bin/env python3
"""Offline real-source ingestion and refusal census. Never admits or scores rows."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path

VERSION = 1
BASE_REFUSALS = (
    'provider_authentication_unestablished', 'complete_slate_provenance_unestablished',
    'pregame_asof_unestablished', 'independent_timestamp_trust_unestablished',
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('nonfinite_json')

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('nonfinite_json')
        return result

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def instant(value):
    if not isinstance(value, str):
        raise ValueError('invalid_time')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone_required')
    return result


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('invalid_numeric_id')
    return str(value)


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('invalid_contract_shape')


def source_bytes(root, entry):
    exact_keys(entry, ('kind', 'game_id', 'source_id', 'version', 'url',
                       'body_sha256', 'retrieved_at_local', 'failure'))
    if entry['kind'] not in ('schedule', 'markets', 'final', 'timestamp'):
        raise ValueError('unknown_source_kind')
    if entry['kind'] == 'final':
        if not isinstance(entry['game_id'], str) or not re.fullmatch(r'[1-9][0-9]*', entry['game_id']):
            raise ValueError('invalid_final_game_id')
    elif entry['game_id'] is not None:
        raise ValueError('unexpected_game_id')
    for key in ('source_id', 'version', 'url'):
        if not isinstance(entry[key], str) or not entry[key].strip() or entry[key] != entry[key].strip():
            raise ValueError('missing_source_metadata')
    if entry['retrieved_at_local'] is not None:
        instant(entry['retrieved_at_local'])  # Diagnostic only; never a trust input.
    key = entry['body_sha256']
    if key is None:
        if entry['failure'] not in ('not_acquired', 'access_unavailable', 'fetch_failed'):
            raise ValueError('missing_failure_reason')
        return None
    if entry['failure'] is not None or not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
        raise ValueError('invalid_source_digest')
    objects = root / 'objects'
    path = objects / key
    if objects.is_symlink() or path.is_symlink():
        raise ValueError('source_symlink')
    raw = path.read_bytes()
    if sha(raw) != key:
        raise ValueError('changed_source_bytes')
    return raw


def schedule_games(raw, day):
    data = decode(raw)
    if not isinstance(data, dict) or not isinstance(data.get('dates'), list):
        raise ValueError('invalid_schedule')
    games = []
    for block in data['dates']:
        if block['date'] != day or not isinstance(block['games'], list):
            raise ValueError('wrong_schedule_date')
        if type(block.get('totalGames')) is not int or block['totalGames'] != len(block['games']):
            raise ValueError('schedule_count_mismatch')
        games.extend(block['games'])
    if len(data['dates']) > 1 or type(data.get('totalGames')) is not int or data['totalGames'] != len(games):
        raise ValueError('schedule_count_mismatch')
    result = []
    for i, game in enumerate(games):
        if game['officialDate'] != day:
            raise ValueError('wrong_game_date')
        away, home = (game['teams'][s]['team'] for s in ('away', 'home'))
        ids = [positive_id(game['gamePk']), positive_id(away['id']), positive_id(home['id'])]
        if ids[1] == ids[2]:
            raise ValueError('duplicate_team_id')
        instant(game['gameDate'])
        for team in (away, home):
            if not isinstance(team.get('name'), str) or not team['name'].strip():
                raise ValueError('missing_team_name')
        result.append({'game_id': ids[0], 'away_id': ids[1], 'home_id': ids[2],
                       'away_name': away['name'], 'home_name': home['name'],
                       'scheduled_start': game['gameDate'],
                       'source_pointer': f'/dates/0/games/{i}', 'raw_game': game})
    if len({g['game_id'] for g in result}) != len(result):
        raise ValueError('duplicate_schedule_game')
    return result


def market_candidate(raw, game):
    """Exact name/time candidate only. No inferred cross-provider ID equivalence."""
    data = decode(raw)
    if not isinstance(data, dict) or not isinstance(data.get('events'), list):
        raise ValueError('invalid_market_source')
    matches = []
    for i, event in enumerate(data['events']):
        event_id = event['id']
        if not isinstance(event_id, str) or not event_id.strip() or event_id != event_id.strip():
            raise ValueError('invalid_market_event_id')
        for j, comp in enumerate(event['competitions']):
            sides = comp['competitors']
            if len(sides) != 2 or {s['homeAway'] for s in sides} != {'away', 'home'}:
                raise ValueError('invalid_market_sides')
            teams = {s['homeAway']: s['team']['displayName'] for s in sides}
            if teams != {'away': game['away_name'], 'home': game['home_name']}:
                continue
            if instant(comp['date']) != instant(game['scheduled_start']):
                continue
            matches.append({'event_id': event_id, 'source_pointer': f'/events/{i}/competitions/{j}',
                            'odds_present': bool(comp.get('odds'))})
    if len(matches) != 1:
        return {'status': 'refused', 'reason': 'market_identity_missing_or_ambiguous', 'candidates': matches}
    return {'status': 'candidate_only', 'reason': 'cross_provider_id_mapping_unestablished', **matches[0]}


def final_candidate(raw, game):
    data = decode(raw)
    gd = data['gameData']
    ids = [positive_id(data['gamePk']), positive_id(gd['teams']['away']['id']),
           positive_id(gd['teams']['home']['id'])]
    if ids != [game[k] for k in ('game_id', 'away_id', 'home_id')] or positive_id(gd['game']['pk']) != game['game_id']:
        return {'status': 'refused', 'reason': 'final_identity_mismatch'}
    if instant(gd['datetime']['dateTime']) != instant(game['scheduled_start']):
        return {'status': 'refused', 'reason': 'final_schedule_mismatch'}
    original = game['raw_game']
    if gd['datetime']['officialDate'] != original['officialDate']:
        return {'status': 'refused', 'reason': 'final_schedule_mismatch'}
    if any(s.get('abstractGameState') != 'Final' or s.get('detailedState') != 'Final'
           for s in (gd['status'], original['status'])):
        return {'status': 'unavailable', 'reason': 'final_not_complete_in_both_sources'}
    scores = [data['liveData']['linescore']['teams'][s]['runs'] for s in ('away', 'home')]
    original_scores = [original['teams'][s]['score'] for s in ('away', 'home')]
    if any(type(s) is not int or s < 0 for s in scores + original_scores) or scores[0] == scores[1]:
        return {'status': 'refused', 'reason': 'invalid_final_scores'}
    if scores != original_scores:
        return {'status': 'refused', 'reason': 'conflicting_final_scores'}
    return {'status': 'structurally_corroborated_only', 'reason': 'final_authentication_unestablished',
            'away_score': scores[0], 'home_score': scores[1],
            'source_pointer': '/liveData/linescore/teams'}


def report(root):
    root = Path(root)
    manifest_path = root / 'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('manifest_symlink')
    manifest_raw = manifest_path.read_bytes()
    manifest = decode(manifest_raw)
    exact_keys(manifest, ('schema', 'slate_date', 'sources'))
    if type(manifest['schema']) is not int or manifest['schema'] != VERSION:
        raise ValueError('unknown_schema')
    day = manifest['slate_date']
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('invalid_slate_date')
    if not isinstance(manifest['sources'], list):
        raise ValueError('invalid_sources')
    sources = [(e, source_bytes(root, e)) for e in manifest['sources']]
    groups = {kind: [(e, raw) for e, raw in sources if e['kind'] == kind]
              for kind in ('schedule', 'markets', 'final', 'timestamp')}
    if any(len(groups[k]) != 1 for k in ('schedule', 'markets', 'timestamp')):
        raise ValueError('exactly_one_source_slot_required')
    final_ids = [e['game_id'] for e, _ in groups['final']]
    if len(set(final_ids)) != len(final_ids):
        raise ValueError('duplicate_final_attempt_use_separate_manifest')
    result = {'schema': VERSION, 'mode': 'real_source_refusal_only', 'eligible': False,
              'admitted_game_ids': [], 'dry_run_pass': False, 'scoring_enabled': False,
              'manifest_sha256': sha(manifest_raw), 'implementation_sha256': sha(Path(__file__).read_bytes()),
              'slate_date': day, 'scheduled_games': None, 'schedule_status': 'unknown',
              'rows': [], 'sources': [e for e, _ in sources], 'refusals': list(BASE_REFUSALS)}
    schedule, raw = groups['schedule'][0]
    if raw is None:
        result['refusals'].append('schedule_source_unavailable')
        return result
    expected_url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={day}&hydrate=linescore'
    if (schedule['source_id'], schedule['version'], schedule['url']) != ('mlb_statsapi', 'v1', expected_url):
        result['refusals'].append('unsupported_schedule_source')
        return result
    try:
        games = schedule_games(raw, day)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        result['refusals'].append('invalid_schedule:' + type(exc).__name__)
        return result  # No guessed denominator after malformed/duplicate identities.
    result.update(scheduled_games=len(games), schedule_status='source_census_only')
    ids = {g['game_id'] for g in games}
    if set(final_ids) - ids:
        raise ValueError('final_outside_declared_slate')
    market, market_raw = groups['markets'][0]
    receipt, receipt_raw = groups['timestamp'][0]
    for game in games:
        row = {k: v for k, v in game.items() if k != 'raw_game'}
        reasons = list(BASE_REFUSALS)
        reasons.extend(('same_book_market_semantics_unestablished', 'trusted_capture_time_unavailable'))
        row.update(eligible=False, state='refused', source_sha256=schedule['body_sha256'])
        if market_raw is None:
            row['market'] = {'status': 'unavailable', 'reason': 'market_source_unavailable'}
        elif (market['source_id'], market['version'], market['url']) != (
                'espn_scoreboard', 'v2', f'https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={day.replace("-", "")}&limit=100'):
            row['market'] = {'status': 'refused', 'reason': 'unsupported_market_source'}
        else:
            try:
                row['market'] = market_candidate(market_raw, game)
            except (KeyError, TypeError, ValueError, AttributeError):
                row['market'] = {'status': 'refused', 'reason': 'invalid_market_source'}
        row['market']['source_sha256'] = market['body_sha256']
        reasons.append(row['market']['reason'])
        row['timestamp'] = {'status': 'unavailable' if receipt_raw is None else 'unverified',
                            'reason': 'timestamp_receipt_missing' if receipt_raw is None else 'timestamp_verifier_unimplemented',
                            'source_sha256': receipt['body_sha256'], 'trusted_time': None}
        reasons.append(row['timestamp']['reason'])
        final = next(((e, b) for e, b in groups['final'] if e['game_id'] == game['game_id']), None)
        row['final'] = {'status': 'unavailable', 'reason': 'final_source_unavailable', 'source_sha256': None}
        if final is not None:
            e, b = final
            if b is not None:
                if (e['source_id'], e['version'], e['url']) != ('mlb_statsapi', 'v1.1', f'https://statsapi.mlb.com/api/v1.1/game/{game["game_id"]}/feed/live'):
                    row['final'] = {'status': 'refused', 'reason': 'unsupported_final_source'}
                else:
                    try:
                        row['final'] = final_candidate(b, game)
                    except (KeyError, TypeError, ValueError, AttributeError):
                        row['final'] = {'status': 'refused', 'reason': 'invalid_final_source'}
            row['final']['source_sha256'] = e['body_sha256']
        reasons.append(row['final']['reason'])
        # Asserted retrieval/HTTP dates and source status never prove capture lateness.
        row['timing'] = {'status': 'unproven', 'capture_late': None, 'actual_first_pitch': None}
        row['refusals'] = sorted(set(reasons))
        result['rows'].append(row)
    result['refused_games'] = len(result['rows'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = report(args.bundle)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'status': 'invalid_bundle', 'error': type(exc).__name__, 'eligible': False}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 1  # Valid refusal census is never successful admission.


if __name__ == '__main__':
    raise SystemExit(main())
