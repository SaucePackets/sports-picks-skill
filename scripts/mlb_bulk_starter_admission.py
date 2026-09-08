#!/usr/bin/env python3
"""Offline bulk starter/history reconstruction from retained bytes; never score."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from pathlib import Path
import re

from mlb_chronological_admission import CONTRACT_PATH, ERRORS, contract, load_bundle, outcome
from mlb_chronological_checkpoint import dates, encoded
from mlb_chronological_starter_probe import timestamp
from mlb_market_free_checkpoint import interrupted_status, require, schedule_census
from mlb_real_shadow_capture import decode, instant, positive_id, sha

SIDES = ('away', 'home')
# Unknown/non-PA events are refused, never silently counted as ordinary outs.
PA_EVENTS = frozenset('single double triple home_run field_out force_out grounded_into_double_play '
    'double_play triple_play fielders_choice fielders_choice_out field_error strikeout '
    'strikeout_double_play walk intent_walk hit_by_pitch sac_fly sac_bunt sac_fly_double_play '
    'sac_bunt_double_play catcher_interf'.split())
K_EVENTS = {'strikeout', 'strikeout_double_play'}
BB_EVENTS = {'walk', 'intent_walk'}


def snapshots(root):
    """Verify every indexed receipt/object, including unused and failed responses."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink() and not (root / 'objects').is_symlink(),
            'snapshot_root_invalid')
    sources, bindings = {}, []
    for path in sorted(root.glob('*.json')):
        # The feasibility directory also contains exploratory precutoff diagnostics.
        if not path.name.startswith(('timestamps-', 'snapshot-')):
            continue
        require(not path.is_symlink(), 'snapshot_receipt_symlink')
        raw = path.read_bytes()
        receipt = decode(raw)
        kind, key = receipt['kind'], receipt['key']
        require(kind in ('timestamps', 'snapshot') and isinstance(key, str)
                and re.fullmatch('[1-9][0-9]*', key) and path.name == f'{kind}-{key}.json',
                'snapshot_receipt_identity')
        url = f'https://statsapi.mlb.com/api/v1.1/game/{key}/feed/live'
        require(isinstance(receipt['url'], str) and (receipt['url'] == url + '/timestamps'
                if kind == 'timestamps' else re.fullmatch(re.escape(url) + r'\?timecode=\d{8}_\d{6}', receipt['url'])),
                'snapshot_source_url')
        instant(receipt['retrieved_at_local'])
        require(isinstance(receipt['headers'], dict), 'snapshot_headers')
        digest, body = receipt['body_sha256'], None
        if digest is not None:
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'snapshot_digest')
            obj = root / 'objects' / digest
            require(not obj.is_symlink(), 'snapshot_object_symlink')
            body = obj.read_bytes()
            require(sha(body) == digest and type(receipt['size']) is int and len(body) == receipt['size'],
                    'snapshot_integrity')
        if receipt['failure'] is None:
            require(receipt['http_status'] == 200 and body is not None, 'snapshot_response')
        else:
            require(isinstance(receipt['failure'], str) and bool(receipt['failure']), 'snapshot_failure')
            body = None
        sources[kind, key] = (receipt, body)
        bindings.append({'receipt': path.name, 'sha256': sha(raw), 'body_sha256': digest})
    return sources, bindings


def starter(game, sources, cutoff):
    pairs = [sources.get((kind, game['game_id'])) for kind in ('timestamps', 'snapshot')]
    require(all(p is not None and p[1] is not None for p in pairs), 'retained_starter_source_unavailable')
    index, snapshot = pairs
    codes = decode(index[1])
    require(isinstance(codes, list) and all(isinstance(c, str) for c in codes)
            and len(codes) == len(set(codes)), 'invalid_timestamp_index')
    candidates = [c for c in codes if timestamp(c) < cutoff]
    require(bool(candidates), 'no_provider_snapshot_before_cutoff')
    code = max(candidates, key=timestamp)
    require(snapshot[0]['url'].endswith('?timecode=' + code), 'snapshot_not_latest_pre_cutoff')
    data = decode(snapshot[1])
    gd = data['gameData']
    require(data['metaData']['timeStamp'] == code, 'returned_snapshot_time_mismatch')
    require(positive_id(data['gamePk']) == positive_id(gd['game']['pk']) == game['game_id'],
            'snapshot_game_mismatch')
    require([positive_id(gd['teams'][s]['id']) for s in SIDES] == [game[s + '_id'] for s in SIDES],
            'snapshot_team_mismatch')
    require(gd['datetime'].get('originalDate', gd['datetime']['officialDate'])
            == gd['datetime']['officialDate'] == game['source_date'] == game['raw_game']['officialDate']
            and instant(gd['datetime']['dateTime']) == instant(game['scheduled_start']),
            'snapshot_schedule_mismatch')
    require(gd['game']['type'] == game['raw_game']['gameType'] == 'R'
            and gd['game']['season'] == '2025', 'snapshot_game_type')
    require(gd['status']['abstractGameState'] == 'Preview' and not interrupted_status(gd['status'])
            and not any('resum' in k.lower() or 'suspend' in k.lower() for k in gd['datetime']),
            'snapshot_not_pregame')
    pitchers = {s: positive_id(gd['probablePitchers'][s]['id']) for s in SIDES}
    require(pitchers['away'] != pitchers['home'], 'same_probable_pitcher')
    return {'pitcher_ids': pitchers, 'timecode': code, 'snapshot_sha256': snapshot[0]['body_sha256'],
            'timestamp_index_sha256': index[0]['body_sha256'],
            'pitcher_pointer': '/gameData/probablePitchers', 'timestamp_pointer': '/metaData/timeStamp'}


def appearances(game, body):
    """Corroborate all listed pitchers, per-game BF/K/BB, and completed PA identity."""
    final = outcome(game, body)
    data = decode(body)
    boxes = data['liveData']['boxscore']['teams']
    records = {}
    for side in SIDES:
        box = boxes[side]
        require(positive_id(box['team']['id']) == game[side + '_id'], 'box_team_mismatch')
        ids = [positive_id(p) for p in box['pitchers']]
        require(bool(ids) and len(ids) == len(set(ids)), 'box_pitcher_list')
        for pid in ids:
            require(pid not in records, 'pitcher_on_both_teams')
            player = box['players']['ID' + pid]
            require(positive_id(player['person']['id']) == pid, 'box_pitcher_identity')
            stats = player['stats']['pitching']
            require(all(type(stats[k]) is int and stats[k] >= 0
                        for k in ('battersFaced', 'strikeOuts', 'baseOnBalls')), 'pitching_counts')
            records[pid] = {'pitcher_id': pid, 'team_id': game[side + '_id'], 'side': side,
                'game_id': game['game_id'], 'source_date': game['source_date'],
                'scheduled_start': game['scheduled_start'], 'completed_at': final['completed_at'],
                'feed_sha256': sha(body), 'schedule_sha256': game['schedule_sha256'],
                'schedule_pointer': game['source_pointer'], 'completion_pointer': final['completion_pointer'],
                'box_pointer': f'/liveData/boxscore/teams/{side}/players/ID{pid}/stats/pitching',
                'plate_appearances': 0, 'strikeouts': 0, 'walks': 0, 'play_pointers': [], 'expected': stats}
    for i, play in enumerate(data['liveData']['plays']['allPlays']):
        about = play['about']
        require(type(about['atBatIndex']) is int and about['atBatIndex'] == i
                and type(about['isTopInning']) is bool, 'play_index_or_side')
        require(instant(game['scheduled_start']) <= instant(about['startTime']), 'play_before_scheduled_start')
        require(play['result']['type'] == 'atBat' and play['result']['eventType'] in PA_EVENTS,
                'unsupported_plate_appearance_event')
        pid = positive_id(play['matchup']['pitcher']['id'])
        positive_id(play['matchup']['batter']['id'])
        events = play['playEvents']
        require(isinstance(events, list) and bool(events), 'missing_play_events')
        pitch_seen = False
        for event in events:
            require(type(event['isPitch']) is bool and isinstance(event['details'], dict),
                    'invalid_play_event')
            if event['details'].get('eventType') == 'pitching_substitution':
                require(not pitch_seen, 'ambiguous_mid_appearance_pitcher_substitution')
                require(positive_id(event['player']['id']) == pid, 'substitution_pitcher_mismatch')
            pitch_seen = pitch_seen or event['isPitch']
        require(pid in records, 'play_pitcher_not_in_box')
        record = records[pid]
        require(record['side'] == ('home' if about['isTopInning'] else 'away'), 'play_pitcher_team_mismatch')
        record['plate_appearances'] += 1
        record['strikeouts'] += int(play['result']['eventType'] in K_EVENTS)
        record['walks'] += int(play['result']['eventType'] in BB_EVENTS)
        record['play_pointers'].append(f'/liveData/plays/allPlays/{i}')
    for record in records.values():
        stats = record.pop('expected')
        require(record['plate_appearances'] > 0, 'appearance_without_completed_pa')
        require([record[k] for k in ('plate_appearances', 'strikeouts', 'walks')] ==
                [stats[k] for k in ('battersFaced', 'strikeOuts', 'baseOnBalls')], 'pitching_count_conflict')
    return list(records.values())


def history(game, pid, records, gaps, cutoff, census_complete):
    # Never skip a recent appearance that fails completion to select an older one.
    prior = sorted((r for r in records if r['pitcher_id'] == pid and r['source_date'] < game['source_date']),
                   key=lambda r: (instant(r['completed_at']), int(r['game_id'])))[-3:]
    relevant_gaps = [g for g in gaps if g['source_date'] < game['source_date']]
    reason = ('schedule_census_unknown' if not census_complete else
              'prior_appearance_census_unresolved' if relevant_gaps else
              'fewer_than_three_prior_appearances' if len(prior) < 3 else
              'prior_completion_at_or_after_cutoff' if any(instant(r['completed_at']) >= cutoff for r in prior)
              else None)
    denominator = sum(r['plate_appearances'] for r in prior)
    return {'pitcher_id': pid, 'appearances': prior, 'unresolved_games': relevant_gaps,
            'plate_appearances': denominator, 'refusal': reason,
            'strikeout_fraction': sum(r['strikeouts'] for r in prior) / denominator if reason is None else None,
            'walk_fraction': sum(r['walks'] for r in prior) / denominator if reason is None else None}


def admission(bundle, snapshot_dir):
    manifest_hash, sources = load_bundle(bundle)
    retained, bindings = snapshots(snapshot_dir)
    census, games = [], []
    for split, bounds in contract()['splits'].items():
        for day in dates(bounds):
            pair = sources.get(('schedule', day))
            row = {'date': day, 'split': split, 'occurrences': None, 'regular_season_occurrences': None,
                   'refusal': None, 'schedule_sha256': pair[0]['body_sha256'] if pair else None}
            try:
                require(pair is not None and pair[1] is not None, 'schedule_unavailable')
                found = schedule_census(pair[1], day)
                require(all(isinstance(g['raw_game'].get('gameType'), str) and g['raw_game']['gameType']
                            for g in found), 'missing_game_type')
                row.update(occurrences=len(found), regular_season_occurrences=sum(g['raw_game']['gameType'] == 'R' for g in found))
                games.extend(g | {'split': split, 'schedule_sha256': row['schedule_sha256']} for g in found)
            except ERRORS as exc:
                row['refusal'] = str(exc)
            census.append(row)
    complete = all(r['refusal'] is None for r in census)
    counts = Counter(g['game_id'] for g in games)
    records, gaps, appearance_rows = [], [], []
    for g in games:
        if g['raw_game']['gameType'] != 'R':
            continue
        pair = sources.get(('feed', g['game_id']))
        row = {k: g[k] for k in ('game_id', 'source_date', 'split', 'schedule_sha256')}
        row.update(feed_sha256=pair[0]['body_sha256'] if pair else None, refusal=None, appearances=[])
        try:
            require(complete, 'schedule_census_unknown')
            require(counts[g['game_id']] == 1, 'repeated_game_across_source_dates')
            require(pair is not None and pair[1] is not None, 'feed_unavailable')
            row['appearances'] = appearances(g, pair[1])
            records.extend(row['appearances'])
        except ERRORS as exc:
            row['refusal'] = str(exc)
            gaps.append({k: row[k] for k in ('game_id', 'source_date', 'refusal', 'feed_sha256')})
        appearance_rows.append(row)
    rows = []
    for g in games:
        row = {k: g[k] for k in ('game_id', 'source_date', 'split', 'scheduled_start', 'schedule_sha256')}
        row.update(schedule_pointer=g['source_pointer'], starter=None, histories=None, refusal=None,
                   strikeout_fraction_difference=None, walk_fraction_difference=None, features_admitted=False)
        try:
            require(g['raw_game']['gameType'] == 'R', 'not_regular_season')
            require(complete, 'schedule_census_unknown')
            require(counts[g['game_id']] == 1, 'repeated_game_across_source_dates')
            require(g['raw_game']['officialDate'] == g['source_date']
                    and not interrupted_status(g['raw_game']['status'])
                    and not any('resum' in k.lower() or 'suspend' in k.lower() for k in g['raw_game']),
                    'target_schedule_identity_refused')
            cutoff = instant(g['scheduled_start']) - timedelta(minutes=60)
            if g['split'] == 'train':
                cutoff = min(cutoff, instant(contract()['training_completion_cutoff']))
            row['observation_cutoff'] = cutoff.isoformat()
            row['starter'] = starter(g, retained, cutoff)
            row['histories'] = {s: history(g, row['starter']['pitcher_ids'][s], records, gaps, cutoff, complete) for s in SIDES}
            require(all(row['histories'][s]['refusal'] is None for s in SIDES), 'pitcher_history_refused')
            for feature in ('strikeout_fraction', 'walk_fraction'):
                row[feature + '_difference'] = row['histories']['home'][feature] - row['histories']['away'][feature]
            row['features_admitted'] = True
        except ERRORS as exc:
            row['refusal'] = str(exc)
        rows.append(row)
    summary = {}
    for split in contract()['splits']:
        days = [r for r in census if r['split'] == split]
        selected = [r for r in rows if r['split'] == split]
        summary[split] = {'expected_dates': len(days), 'known_dates': sum(r['refusal'] is None for r in days),
            'occurrences': sum(r['occurrences'] for r in days) if all(r['refusal'] is None for r in days) else None,
            'regular_season_occurrences': sum(r['regular_season_occurrences'] for r in days) if all(r['refusal'] is None for r in days) else None,
            'starter_candidates': sum(r['starter'] is not None for r in selected),
            'feature_pairs_admitted': sum(r['features_admitted'] for r in selected),
            'refusals': dict(Counter(r['refusal'] for r in selected if r['refusal']))}
    return {'schema': 'mlb-bulk-starter-admission-v1', 'evidence_kind': 'historical_reconstruction',
        'manifest_sha256': manifest_hash, 'contract_sha256': sha(CONTRACT_PATH.read_bytes()),
        'adapter_sha256': {name: sha(Path(__file__).with_name(name).read_bytes()) for name in
            ('mlb_bulk_starter_admission.py', 'mlb_chronological_admission.py', 'mlb_chronological_starter_probe.py',
             'mlb_chronological_checkpoint.py', 'mlb_market_free_checkpoint.py', 'mlb_real_shadow_capture.py')},
        'snapshot_receipts': bindings, 'snapshot_receipts_sha256': sha(encoded(bindings)),
        'census_complete': complete, 'census': census, 'summary': summary,
        'occurrences': rows, 'appearance_games': appearance_rows,
        'history_scope': 'last three regular-season appearances in fixed window; any earlier unresolved regular game blocks history',
        'historical_availability': 'provider timestamp metadata only; independently unverified',
        'asof_snapshot_verified': False, 'original_pregame_predictions': False,
        'fitting_enabled': False, 'scoring_enabled': False, 'eligible': False, 'historical_performance': None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--snapshot-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(encoded(admission(args.bundle, args.snapshot_dir)).decode(), end='')
    except (OSError, *ERRORS) as exc:
        print(encoded({'error': str(exc), 'fitting_enabled': False, 'scoring_enabled': False,
                       'eligible': False}).decode(), end='')
        raise SystemExit(2)
