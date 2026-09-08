#!/usr/bin/env python3
"""Offline, market-free historical reconstruction. No scoring or live admission."""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mlb_real_shadow_capture import decode, final_candidate, instant, positive_id, sha

SPEC = {
    'version': 1, 'training_dates': [f'2025-04-{d}' for d in range(21, 28)],
    'cutoff': '2025-04-30T00:00:00Z', 'evaluation_date': '2025-05-01',
    'savant_date': '2025-04-21', 'initial': 1500, 'k': 20, 'scale': 400,
    'home_offset': 0, 'minimum_prior': 1, 'empirical_home': '(home_wins+1)/(n+2)',
    'tie_break': 'numeric_game_id', 'savant_features': [],
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def load_bundle(root):
    """Verify every retained byte, including unused/excluded sources, before parsing."""
    root = Path(root)
    require(not root.is_symlink() and not (root / 'objects').is_symlink(), 'bundle_symlink')
    manifest = root / 'manifest.json'
    require(not manifest.is_symlink(), 'manifest_symlink')
    raw = manifest.read_bytes()
    data = decode(raw)
    require(data.get('schema') == 1 and isinstance(data.get('sources'), list), 'manifest_schema')
    sources = {}
    for s in data['sources']:
        kind, key = s['kind'], s['key']
        require(kind in ('schedule', 'feed', 'savant') and isinstance(key, str), 'source_key')
        require((kind, key) not in sources, 'duplicate_source')
        if kind == 'schedule':
            require(key in SPEC['training_dates'] + [SPEC['evaluation_date']], 'unexpected_schedule')
            require(s['url'] == f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={key}&hydrate=linescore', 'schedule_url')
        elif kind == 'feed':
            require(re.fullmatch('[1-9][0-9]*', key), 'feed_id')
            require(s['url'] == f'https://statsapi.mlb.com/api/v1.1/game/{key}/feed/live', 'feed_url')
        else:
            u = urlparse(s['url'])
            require(key == SPEC['savant_date'] and u.scheme == 'https'
                    and u.netloc == 'baseballsavant.mlb.com' and u.path == '/statcast_search/csv', 'savant_url')
            require(parse_qs(u.query) == {
                'all': ['true'], 'type': ['details'], 'player_type': ['pitcher'], 'hfGT': ['R|'],
                'game_date_gt': [key], 'game_date_lt': [key], 'group_by': ['name'],
                'min_pitches': ['0'], 'min_results': ['0']}, 'savant_query')
        instant(s['retrieved_at_local'])  # diagnostic, never the historical cutoff
        digest = s['body_sha256']
        body = None
        if digest is None:
            require(isinstance(s['failure'], str) and bool(s['failure']), 'missing_failure')
        else:
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'invalid_digest')
            require(s['failure'] is None and s['http_status'] == 200, 'source_response')
            path = root / 'objects' / digest
            require(not path.is_symlink(), 'object_symlink')
            body = path.read_bytes()
            require(sha(body) == digest and len(body) == s['size'], 'source_integrity')
        sources[kind, key] = (s, body)
    for day in SPEC['training_dates'] + [SPEC['evaluation_date']]:
        require(('schedule', day) in sources, 'missing_schedule_slot')
    require(('savant', SPEC['savant_date']) in sources, 'missing_savant_slot')
    return sha(raw), sources


def schedule_census(raw, day):
    data = decode(raw)
    require(isinstance(data['dates'], list) and len(data['dates']) <= 1, 'schedule_dates')
    games = []
    for block in data['dates']:
        require(block['date'] == day and type(block['totalGames']) is int
                and block['totalGames'] == len(block['games']), 'schedule_date_or_count')
        for i, g in enumerate(block['games']):
            ids = [positive_id(g['gamePk'])] + [positive_id(g['teams'][s]['team']['id']) for s in ('away', 'home')]
            require(ids[1] != ids[2], 'same_team')
            instant(g['gameDate'])
            # Postponed occurrences can point at a later official date. Retain them.
            require(isinstance(g['officialDate'], str) and re.fullmatch(r'2025-\d{2}-\d{2}', g['officialDate']), 'official_date')
            games.append({'game_id': ids[0], 'away_id': ids[1], 'home_id': ids[2],
                          'scheduled_start': g['gameDate'], 'source_date': day,
                          'source_pointer': f'/dates/0/games/{i}', 'raw_game': g})
    require(type(data['totalGames']) is int and data['totalGames'] == len(games), 'schedule_count')
    require(len({g['game_id'] for g in games}) == len(games), 'duplicate_game_within_date')
    return games


def training_row(game, feed):
    """Admit reconstructed final facts only; do not forge a historical observed_at."""
    result = final_candidate(feed, game)
    require(result['status'] == 'structurally_corroborated_only', result['reason'])
    d = decode(feed)
    gd, plays = d['gameData'], d['liveData']['plays']['allPlays']
    require(gd['game']['type'] == game['raw_game']['gameType'] == 'R', 'not_regular_season')
    require(gd['game']['season'] == '2025', 'wrong_season')
    require(not any('resum' in k.lower() or 'suspend' in k.lower()
                    for obj in (gd['datetime'], game['raw_game']) for k in obj), 'resumed_or_suspended')
    require(gd['datetime'].get('originalDate', gd['datetime']['officialDate']) == gd['datetime']['officialDate'], 'changed_original_date')
    require(bool(plays), 'missing_plays')
    last = plays[-1]
    completion = instant(last['about']['endTime'])
    require(last['about']['isComplete'] is True, 'incomplete_last_play')
    require(instant(game['scheduled_start']) < completion < instant(SPEC['cutoff']), 'completion_outside_window')
    for p in plays:
        require(p['about']['isComplete'] is True
                and instant(p['about']['startTime']) <= instant(p['about']['endTime']) <= completion, 'play_chronology')
    require([last['result']['awayScore'], last['result']['homeScore']] ==
            [result['away_score'], result['home_score']], 'last_play_score_conflict')
    return {'game_id': game['game_id'], 'away_id': game['away_id'], 'home_id': game['home_id'],
            'completed_at': last['about']['endTime'], 'away_won': int(result['away_score'] > result['home_score']),
            'away_score': result['away_score'], 'home_score': result['home_score'],
            'completion_pointer': f'/liveData/plays/allPlays/{len(plays)-1}/about/endTime',
            'score_pointer': '/liveData/linescore/teams',
            'identity_pointer': '/gameData/teams'}


def train(rows):
    ratings, counts, trace = {}, {}, []
    for r in sorted(rows, key=lambda r: (instant(r['completed_at']), int(r['game_id']))):
        a, h = r['away_id'], r['home_id']
        ar, hr = (ratings.get(t, SPEC['initial']) for t in (a, h))
        p = 1 / (1 + 10 ** ((hr - ar) / SPEC['scale']))
        delta = SPEC['k'] * (r['away_won'] - p)
        ratings[a], ratings[h] = ar + delta, hr - delta
        for t in (a, h):
            counts[t] = counts.get(t, 0) + 1
        trace.append({'game_id': r['game_id'], 'prior_away': ar, 'prior_home': hr,
                      'away_probability': p, 'delta': delta,
                      'after_away': ratings[a], 'after_home': ratings[h]})
    return ratings, counts, trace


def savant_lineage(raw, games, feeds):
    if raw is None:
        return {'status': 'blocked', 'reason': 'savant_not_acquired', 'rows': []}
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    fields = ['game_pk', 'game_date', 'game_type', 'home_team', 'away_team', 'batter', 'pitcher',
              'at_bat_number', 'pitch_number', 'plate_x', 'plate_z']
    require(reader.fieldnames is not None and len(set(reader.fieldnames)) == len(reader.fieldnames)
            and set(fields) <= set(reader.fieldnames), 'savant_schema')
    rows, seen, cache = [], set(), {}
    for line, row in enumerate(reader, 2):
        out = {'csv_record': line, 'status': 'joined', 'reason': None,
               'source_values': {f: row.get(f) for f in fields}}
        try:
            require(None not in row and all(row.get(f) is not None for f in fields), 'csv_row_shape')
            require(all(re.fullmatch('[1-9][0-9]*', row[f]) for f in
                        ('game_pk', 'batter', 'pitcher', 'at_bat_number', 'pitch_number')), 'savant_numeric_ids')
            key = (row['game_pk'], row['at_bat_number'], row['pitch_number'])
            require(key not in seen, 'duplicate_pitch_key')
            seen.add(key)
            g = games.get(row['game_pk'])
            require(g is not None and g['raw_game']['officialDate'] == SPEC['savant_date'], 'savant_game_unmatched')
            require(row['game_date'] == SPEC['savant_date'] and row['game_type'] == 'R', 'savant_date_or_type')
            raw_feed = feeds.get(row['game_pk'])
            require(raw_feed is not None, 'savant_feed_missing')
            if row['game_pk'] not in cache:
                cache[row['game_pk']] = decode(raw_feed)
            d = cache[row['game_pk']]
            gd = d['gameData']
            require(str(d['gamePk']) == g['game_id'] and str(gd['game']['pk']) == g['game_id'], 'savant_feed_game_conflict')
            require(gd['datetime']['officialDate'] == row['game_date'], 'savant_feed_date_conflict')
            for side in ('away', 'home'):
                require(str(gd['teams'][side]['id']) == g[side+'_id']
                        and row[side+'_team'] == gd['teams'][side]['abbreviation'], 'savant_team_conflict')
            for role in ('batter', 'pitcher'):
                require(gd['players']['ID'+row[role]]['id'] == int(row[role]), 'savant_player_conflict')
            # Corroborate the player pair at the actual plate appearance, not merely roster presence.
            index = int(row['at_bat_number']) - 1
            play = d['liveData']['plays']['allPlays'][index]
            require(play['about']['atBatIndex'] == index, 'savant_plate_appearance_conflict')
            require(all(play['matchup'][role]['id'] == int(row[role]) for role in ('batter', 'pitcher')), 'savant_matchup_conflict')
            coords = {}
            for f in ('plate_x', 'plate_z'):
                coords[f] = None if row[f] == '' else float(row[f])
                require(coords[f] is None or math.isfinite(coords[f]), 'savant_nonfinite_coordinate')
            out.update(game_id=g['game_id'], feed_sha256=sha(raw_feed),
                       player_pointer='/gameData/players',
                       matchup_pointer=f'/liveData/plays/allPlays/{index}/matchup',
                       coordinates=coords, feature_use='lineage_only')
        except (KeyError, IndexError, TypeError, ValueError) as e:
            out.update(status='refused', reason=str(e))
        rows.append(out)
    return {'status': 'complete' if rows and all(r['status'] == 'joined' for r in rows) else 'blocked',
            'row_count': len(rows), 'joined_count': sum(r['status'] == 'joined' for r in rows),
            'rows': rows}


def checkpoint(root):
    manifest_hash, sources = load_bundle(root)
    games, training, refusals, evaluation, blockers, feeds = {}, [], [], [], [], {}
    occurrences = []
    schedule_counts = {}
    for day in SPEC['training_dates'] + [SPEC['evaluation_date']]:
        entry, body = sources['schedule', day]
        if body is None:
            blockers.append('missing_schedule:'+day)
            schedule_counts[day] = None
            continue
        day_games = schedule_census(body, day)
        schedule_counts[day] = len(day_games)
        for g in day_games:
            g['schedule_sha256'] = entry['body_sha256']
            occurrences.append(g)
            games.setdefault(g['game_id'], g)
            if day == SPEC['evaluation_date']:
                evaluation.append(g)
    multiplicity = {}
    for g in occurrences:
        multiplicity[g['game_id']] = multiplicity.get(g['game_id'], 0) + 1
    for g in occurrences:
        if g['source_date'] == SPEC['evaluation_date']:
            continue
        pair = sources.get(('feed', g['game_id']))
        feed = pair[1] if pair else None
        feeds[g['game_id']] = feed
        lineage = {'game_id': g['game_id'], 'source_date': g['source_date'],
                   'schedule_sha256': g['schedule_sha256'], 'schedule_pointer': g['source_pointer'],
                   'feed_sha256': sha(feed) if feed else None}
        try:
            require(multiplicity[g['game_id']] == 1, 'repeated_game_across_source_dates')
            require(g['raw_game']['officialDate'] == g['source_date'], 'official_date_moved')
            require(feed is not None, 'missing_training_feed')
            training.append({**training_row(g, feed), **lineage})
        except (KeyError, TypeError, ValueError, IndexError) as e:
            refusals.append({**lineage, 'status': 'refused', 'reason': str(e)})
    require(all(kind != 'feed' or key in feeds for kind, key in sources), 'unmatched_feed')
    ratings, counts, trace = train(training)
    empirical = (sum(1-r['away_won'] for r in training)+1)/(len(training)+2) if training else None
    predictions = []
    for g in sorted(evaluation, key=lambda g: int(g['game_id'])):
        reasons = []
        if multiplicity[g['game_id']] != 1:
            reasons.append('evaluation_overlaps_training')
        if g['raw_game']['officialDate'] != SPEC['evaluation_date']:
            reasons.append('evaluation_official_date_moved')
        if blockers:
            reasons.append('incomplete_training_schedule')
        if instant(g['scheduled_start']) <= instant(SPEC['cutoff']):
            reasons.append('evaluation_not_after_cutoff')
        if g['raw_game']['gameType'] != 'R':
            reasons.append('not_regular_season')
        team_reasons = reasons + ([] if all(counts.get(g[s+'_id'], 0) >= 1 for s in ('away', 'home')) else ['missing_prior_team_games'])
        ep = None if reasons or empirical is None else empirical
        p = None if team_reasons else 1/(1+10**((ratings[g['home_id']]-ratings[g['away_id']])/SPEC['scale']))
        predictions.append({k: g[k] for k in ('game_id', 'away_id', 'home_id', 'scheduled_start', 'schedule_sha256', 'source_pointer')} |
                           {'status': 'predicted' if p is not None and ep is not None else 'refused',
                            'elo_away_probability': p, 'empirical_home_probability': ep,
                            'team_feature_lineage': {side: {'team_id': g[side+'_id'],
                                'rating': ratings.get(g[side+'_id']), 'prior_games': counts.get(g[side+'_id'], 0),
                                'training_game_ids': [t['game_id'] for t in training if g[side+'_id'] in (t['away_id'], t['home_id'])]}
                                for side in ('away', 'home')},
                            'elo_refusals': team_reasons, 'empirical_refusals': reasons + ([] if empirical is not None else ['no_training_games'])})
    savant = savant_lineage(sources['savant', SPEC['savant_date']][1], games, feeds)
    if savant['status'] != 'complete':
        blockers.append('savant_lineage_incomplete')
    if not training:
        blockers.append('no_accepted_training')
    return {'schema': 'mlb-market-free-checkpoint-v1', 'evidence_kind': 'historical_reconstruction',
            'eligible': False, 'scoring_enabled': False, 'historical_performance': None,
            'asof_snapshot_verified': False, 'original_pregame_predictions': False,
            'schedule_completeness': 'current_source_census_only',
            'checkpoint_complete': not blockers, 'blockers': blockers,
            'spec': SPEC, 'spec_sha256': sha(canonical(SPEC)), 'manifest_sha256': manifest_hash,
            'implementation_sha256': sha(Path(__file__).read_bytes()),
            'source_adapter_sha256': sha(Path(__file__).with_name('mlb_real_shadow_capture.py').read_bytes()),
            'schedule_counts': schedule_counts, 'training': training, 'training_refusals': refusals,
            'training_update_order': trace, 'team_ratings': ratings, 'team_prior_counts': counts,
            'empirical_training_home_wins': sum(1-r['away_won'] for r in training),
            'predictions': predictions, 'savant': savant,
            'savant_sha256': sources['savant', SPEC['savant_date']][0]['body_sha256']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = checkpoint(args.bundle)
        print(json.dumps(report, sort_keys=True, indent=2, allow_nan=False))
        return 0 if report['checkpoint_complete'] else 1
    except (OSError, KeyError, ValueError, TypeError, IndexError, csv.Error) as e:
        print(json.dumps({'error': str(e), 'eligible': False, 'scoring_enabled': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
