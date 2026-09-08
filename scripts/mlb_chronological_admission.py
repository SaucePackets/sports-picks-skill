#!/usr/bin/env python3
"""Offline admission of fixed-window reconstructed facts. No fitting or scoring."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from pathlib import Path
import re

from mlb_chronological_checkpoint import dates, encoded
from mlb_market_free_checkpoint import require, schedule_census, interrupted_status
from mlb_real_shadow_capture import decode, final_candidate, instant, sha

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / 'docs/mlb-chronological-contract.json'
ERRORS = (ValueError, KeyError, TypeError, IndexError, AttributeError)


def contract():
    return decode(CONTRACT_PATH.read_bytes())


def load_bundle(root):
    root = Path(root)
    require(not root.is_symlink() and not (root / 'objects').is_symlink()
            and not (root / 'manifest.json').is_symlink(), 'bundle_symlink')
    raw = (root / 'manifest.json').read_bytes()
    manifest = decode(raw)
    require(manifest['schema'] == 'mlb-chronological-source-v1', 'manifest_schema')
    require(manifest['contract_sha256'] == sha(CONTRACT_PATH.read_bytes()), 'contract_mismatch')
    days = {d for bounds in contract()['splits'].values() for d in dates(bounds)}
    sources = {}
    for entry in manifest['sources']:
        kind, key = entry['kind'], entry['key']
        require(isinstance(key, str) and (kind, key) not in sources, 'duplicate_or_invalid_source')
        if kind == 'schedule':
            require(key in days, 'schedule_outside_contract')
            url = f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={key}&hydrate=linescore'
        else:
            require(kind == 'feed' and re.fullmatch('[1-9][0-9]*', key), 'feed_id')
            url = f'https://statsapi.mlb.com/api/v1.1/game/{key}/feed/live'
        require(entry['url'] == url, 'source_url')
        instant(entry['retrieved_at_local'])
        require(isinstance(entry['headers'], dict), 'response_headers')
        digest = entry['body_sha256']
        body = None
        if digest is not None:
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'source_digest')
            path = root / 'objects' / digest
            require(not path.is_symlink(), 'object_symlink')
            body = path.read_bytes()
            require(sha(body) == digest and type(entry['size']) is int
                    and len(body) == entry['size'], 'source_integrity')
        if entry['failure'] is None:
            require(entry['http_status'] == 200 and body is not None, 'source_response')
        else:
            require(isinstance(entry['failure'], str) and bool(entry['failure']), 'source_failure')
            body = None  # HTTP error bytes are retained and checked, never parsed as evidence.
        sources[kind, key] = (entry, body)
    return sha(raw), sources


def outcome(game, feed):
    result = final_candidate(feed, game)
    require(result['status'] == 'structurally_corroborated_only', result['reason'])
    data = decode(feed)
    gd, plays = data['gameData'], data['liveData']['plays']['allPlays']
    require(gd['game']['type'] == game['raw_game']['gameType'] == 'R', 'not_regular_season')
    require(gd['game']['season'] == '2025', 'wrong_season')
    require(not any('resum' in k.lower() or 'suspend' in k.lower()
                    for obj in (gd['datetime'], game['raw_game']) for k in obj), 'resumed_or_suspended')
    require(not any(interrupted_status(s) for s in (gd['status'], game['raw_game']['status'])),
            'resumed_or_suspended')
    require(gd['datetime'].get('originalDate', gd['datetime']['officialDate'])
            == gd['datetime']['officialDate'] == game['source_date'], 'official_date_moved')
    require(isinstance(plays, list) and bool(plays), 'missing_plays')
    last = plays[-1]
    completion = instant(last['about']['endTime'])
    require(instant(game['scheduled_start']) < completion, 'completion_before_start')
    previous = None
    for play in plays:
        start, end = instant(play['about']['startTime']), instant(play['about']['endTime'])
        require(play['about']['isComplete'] is True and start <= end <= completion
                and (previous is None or previous <= end), 'play_chronology')
        previous = end
    require([last['result']['awayScore'], last['result']['homeScore']] ==
            [result['away_score'], result['home_score']], 'last_play_score_conflict')
    return {k: game[k] for k in ('game_id', 'away_id', 'home_id', 'source_date')} | {
        'completed_at': last['about']['endTime'], 'home_won': int(result['home_score'] > result['away_score']),
        'away_score': result['away_score'], 'home_score': result['home_score'],
        'completion_pointer': f'/liveData/plays/allPlays/{len(plays)-1}/about/endTime',
        'score_pointer': '/liveData/linescore/teams', 'identity_pointer': '/gameData/teams'}


def team_history(game, outcomes, cutoff, census_known):
    result = {}
    for side in ('away', 'home'):
        team = game[side + '_id']
        prior = sorted((r for r in outcomes if team in (r['away_id'], r['home_id'])
                        and r['source_date'] < game['source_date']
                        and instant(r['completed_at']) < cutoff),
                       key=lambda r: (instant(r['completed_at']), int(r['game_id'])))[-10:]
        wins = sum(r['home_won'] if r['home_id'] == team else 1-r['home_won'] for r in prior)
        reason = ('prior_census_unknown' if not census_known else
                  'fewer_than_10_accepted_prior_games' if len(prior) < 10 else None)
        result[side] = {'team_id': team, 'games': prior, 'wins': wins, 'denominator': len(prior),
                        'win_fraction': wins / 10 if reason is None else None, 'refusal': reason}
    result['home_minus_away'] = (result['home']['win_fraction'] - result['away']['win_fraction']
                                 if all(result[s]['refusal'] is None for s in ('away', 'home')) else None)
    return result


def admission(root):
    manifest_hash, sources = load_bundle(root)
    spec = contract()
    day_splits = {d: split for split, bounds in spec['splits'].items() for d in dates(bounds)}
    census, games = [], []
    for day, split in day_splits.items():
        pair = sources.get(('schedule', day))
        record = {'date': day, 'split': split, 'occurrences': None, 'regular_season_occurrences': None,
                  'schedule_sha256': pair[0]['body_sha256'] if pair else None, 'refusal': None}
        try:
            require(pair is not None and pair[1] is not None, 'schedule_unavailable')
            rows = schedule_census(pair[1], day)
            require(all(isinstance(g['raw_game'].get('gameType'), str) and
                        bool(g['raw_game']['gameType']) for g in rows), 'missing_game_type')
            record.update(occurrences=len(rows), regular_season_occurrences=sum(
                g['raw_game']['gameType'] == 'R' for g in rows))
            games.extend(g | {'split': split, 'schedule_sha256': pair[0]['body_sha256']} for g in rows)
        except ERRORS as exc:
            record['refusal'] = str(exc)
        census.append(record)
    census_known = all(r['refusal'] is None for r in census)
    multiplicity = Counter(g['game_id'] for g in games)
    regular_ids = {g['game_id'] for g in games if g['raw_game']['gameType'] == 'R'}
    require(all(kind != 'feed' or key in regular_ids for kind, key in sources)
            or not census_known, 'unmatched_feed')
    occurrences, outcomes = [], []
    for g in games:
        pair = sources.get(('feed', g['game_id']))
        lineage = {k: g[k] for k in ('game_id', 'source_date', 'split', 'schedule_sha256', 'scheduled_start')}
        lineage.update(schedule_pointer=g['source_pointer'], feed_sha256=pair[0]['body_sha256'] if pair else None)
        row = lineage | {'outcome_status': 'refused', 'outcome_refusal': None}
        try:
            require(g['raw_game']['gameType'] == 'R', 'not_regular_season')
            require(census_known, 'full_census_unknown_duplicate_check_blocked')
            require(multiplicity[g['game_id']] == 1, 'repeated_game_across_source_dates')
            require(g['raw_game']['officialDate'] == g['source_date'], 'official_date_moved')
            require(pair is not None and pair[1] is not None, 'feed_unavailable')
            accepted = outcome(g, pair[1]) | lineage
            outcomes.append(accepted)
            row.update(outcome_status='admitted_reconstruction', outcome=accepted)
        except ERRORS as exc:
            row['outcome_refusal'] = str(exc)
        occurrences.append(row)
    for g, row in zip(games, occurrences):
        if g['raw_game']['gameType'] != 'R':
            continue
        cutoff = instant(g['scheduled_start']) - timedelta(minutes=60)
        row['observation_cutoff'] = cutoff.isoformat()
        # Target final feeds establish labels only; histories use prior outcomes.
        target_known = (multiplicity[g['game_id']] == 1
                        and g['raw_game']['officialDate'] == g['source_date']
                        and not interrupted_status(g['raw_game']['status'])
                        and not any('resum' in k.lower() or 'suspend' in k.lower() for k in g['raw_game']))
        if target_known:
            row['team_history'] = team_history(g, outcomes, cutoff, census_known)
        else:
            row['team_history'] = {'home_minus_away': None, 'refusal': 'target_schedule_identity_refused'}
        row['training_label_admitted'] = (g['split'] == 'train' and row['outcome_status'] == 'admitted_reconstruction'
            and instant(row['outcome']['completed_at']) < instant(spec['training_completion_cutoff']))
        row['training_label_refusal'] = ('completion_at_or_after_training_cutoff'
            if g['split'] == 'train' and row['outcome_status'] == 'admitted_reconstruction'
            and not row['training_label_admitted'] else row['outcome_refusal'] if g['split'] == 'train' else 'not_training_split')
        row['starter_features'] = {'status': 'refused', 'reason': 'precutoff_starter_and_pitch_appearance_adapter_not_admitted',
                                   'strikeout_fraction_difference': None, 'walk_fraction_difference': None}
        row['challenger_row_admitted'] = False
    summary = {}
    for split in spec['splits']:
        days = [r for r in census if r['split'] == split]
        rows = [r for r in occurrences if r['split'] == split]
        summary[split] = {'expected_dates': len(days), 'known_dates': sum(r['refusal'] is None for r in days),
            'occurrences': sum(r['occurrences'] for r in days) if all(r['refusal'] is None for r in days) else None,
            'regular_season_occurrences': sum(r['regular_season_occurrences'] for r in days)
                if all(r['refusal'] is None for r in days) else None,
            'outcomes_admitted': sum(r['outcome_status'] == 'admitted_reconstruction' for r in rows),
            'training_labels_admitted': sum(r.get('training_label_admitted', False) for r in rows),
            'team_history_pairs_admitted': sum(r.get('team_history', {}).get('home_minus_away') is not None for r in rows),
            'outcome_refusals': dict(Counter(r['outcome_refusal'] for r in rows if r['outcome_refusal'])),
            'challenger_rows_admitted': 0}
    return {'schema': 'mlb-chronological-admission-v1', 'evidence_kind': 'historical_reconstruction',
        'manifest_sha256': manifest_hash, 'contract_sha256': sha(CONTRACT_PATH.read_bytes()),
        'implementation_sha256': sha(Path(__file__).read_bytes()),
        'source_adapter_sha256': sha((ROOT / 'scripts/mlb_real_shadow_capture.py').read_bytes()),
        'census_adapter_sha256': sha((ROOT / 'scripts/mlb_market_free_checkpoint.py').read_bytes()),
        'census_complete': census_known, 'census': census, 'summary': summary, 'occurrences': occurrences,
        'fitting_enabled': False, 'scoring_enabled': False, 'eligible': False, 'historical_performance': None,
        'asof_snapshot_verified': False, 'original_pregame_predictions': False,
        'blockers': ['precutoff_starter_and_pitch_appearance_adapter_not_admitted']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    try:
        print(encoded(admission(parser.parse_args().bundle)).decode(), end='')
    except (OSError, *ERRORS) as exc:
        print(encoded({'error': str(exc), 'fitting_enabled': False, 'scoring_enabled': False,
                       'eligible': False, 'historical_performance': None}).decode(), end='')
        raise SystemExit(2)
