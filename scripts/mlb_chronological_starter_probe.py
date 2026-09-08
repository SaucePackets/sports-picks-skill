#!/usr/bin/env python3
"""Bounded historical starter feasibility probe, not feature admission."""
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re

from mlb_chronological_acquire import fetch
from mlb_chronological_admission import load_bundle, contract, ERRORS
from mlb_chronological_checkpoint import dates, encoded
from mlb_market_free_checkpoint import require, schedule_census
from mlb_real_shadow_capture import decode, instant, positive_id, sha


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{8}_\d{6}', value), 'invalid_timecode')
    return datetime.strptime(value, '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc)


def retained(root, kind, key, url, acquire):
    if acquire:
        receipt = fetch(root, kind, key, url)
    else:
        path = root / (kind + '-' + key + '.json')
        require(not path.is_symlink(), 'receipt_symlink')
        receipt = decode(path.read_bytes())
    require((receipt['kind'], receipt['key'], receipt['url']) == (kind, key, url), 'probe_identity')
    instant(receipt['retrieved_at_local'])
    digest = receipt['body_sha256']
    body = None
    if digest is not None:
        require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'probe_digest')
        path = root / 'objects' / digest
        require(not path.is_symlink(), 'probe_object_symlink')
        body = path.read_bytes()
        require(sha(body) == digest and len(body) == receipt['size'], 'probe_integrity')
    require(receipt['failure'] is None and receipt['http_status'] == 200 and body is not None,
            'probe_source_unavailable')
    return receipt, decode(body)


def probe(bundle, root, acquire=False):
    root = Path(root)
    require(not root.is_symlink() and not (root / 'objects').is_symlink(), 'probe_symlink')
    if acquire:
        (root / 'objects').mkdir(parents=True, exist_ok=True)
    manifest_hash, sources = load_bundle(bundle)
    rows = []
    for split, bounds in contract()['splits'].items():
        record = {'split': split, 'status': 'blocked', 'reason': None, 'receipts': []}
        try:
            selected = None
            for day in dates(bounds):
                pair = sources.get(('schedule', day))
                require(pair is not None and pair[1] is not None, 'sample_census_unknown')
                games = [g for g in schedule_census(pair[1], day) if g['raw_game']['gameType'] == 'R']
                if games:
                    selected = min(games, key=lambda g: int(g['game_id']))
                    record['schedule_sha256'] = pair[0]['body_sha256']
                    record['schedule_pointer'] = selected['source_pointer']
                    break
            require(selected is not None, 'no_regular_season_occurrence_in_split')
            g = selected
            cutoff = instant(g['scheduled_start']) - timedelta(minutes=60)
            record.update(game_id=g['game_id'], source_date=g['source_date'], cutoff=cutoff.isoformat())
            url = f"https://statsapi.mlb.com/api/v1.1/game/{g['game_id']}/feed/live"
            receipt, codes = retained(root, 'timestamps', g['game_id'], url + '/timestamps', acquire)
            record['receipts'].append(receipt)
            require(isinstance(codes, list) and len(codes) == len(set(codes)), 'invalid_timestamp_index')
            candidates = [c for c in codes if timestamp(c) < cutoff]
            require(bool(candidates), 'no_provider_snapshot_before_cutoff')
            code = max(candidates, key=timestamp)
            receipt, data = retained(root, 'snapshot', g['game_id'], url + '?timecode=' + code, acquire)
            record['receipts'].append(receipt)
            require(data['metaData']['timeStamp'] == code, 'returned_snapshot_time_mismatch')
            gd = data['gameData']
            require([positive_id(data['gamePk']), positive_id(gd['game']['pk'])]
                    == [g['game_id'], g['game_id']], 'snapshot_game_mismatch')
            require([positive_id(gd['teams'][s]['id']) for s in ('away', 'home')]
                    == [g['away_id'], g['home_id']], 'snapshot_team_mismatch')
            require(gd['datetime']['officialDate'] == g['source_date'] == g['raw_game']['officialDate']
                    and instant(gd['datetime']['dateTime']) == instant(g['scheduled_start']), 'snapshot_schedule_mismatch')
            require(gd['game']['type'] == 'R' and gd['game']['season'] == '2025', 'snapshot_game_type')
            require(gd['status']['abstractGameState'] == 'Preview', 'snapshot_not_pregame')
            pitchers = {s: positive_id(gd['probablePitchers'][s]['id']) for s in ('away', 'home')}
            require(pitchers['away'] != pitchers['home'], 'same_probable_pitcher')
            record.update(status='provider_historical_candidate', timecode=code, probable_pitcher_ids=pitchers,
                          pitcher_pointer='/gameData/probablePitchers', timestamp_pointer='/metaData/timeStamp')
        except (OSError, *ERRORS) as exc:
            record['reason'] = str(exc)
        rows.append(record)
    return {'schema': 'mlb-chronological-starter-feasibility-v1', 'manifest_sha256': manifest_hash,
            'selection': 'first regular-season occurrence per split by source date then numeric game ID',
            'samples': rows, 'bulk_coverage_established': False, 'starter_features_admitted': False,
            'historical_availability': 'provider timestamp index and returned snapshot metadata only; independently unverified',
            'blocker': 'bulk_starter_and_corroborated_pitch_appearance_admission_not_implemented',
            'implementation_sha256': sha(Path(__file__).read_bytes()),
            'fitting_enabled': False, 'scoring_enabled': False, 'eligible': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--probe-dir', required=True, type=Path)
    parser.add_argument('--acquire', action='store_true')
    args = parser.parse_args()
    print(encoded(probe(args.bundle, args.probe_dir, args.acquire)).decode(), end='')
