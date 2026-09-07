#!/usr/bin/env python3
"""Bounded retained-byte importer and feasibility checkpoint; never scores."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

CONTRACT_PATH = Path(__file__).resolve().parents[1] / 'docs/mlb-historical-admission-contract-v1.json'
MANIFEST_SHA = '8ef152bbb24bd62291a8a47b0fa1bb321af01d042e86c2f95e645bb69ede455e'
RULES_SHA = '4b9d0a5ab406ca7543103fefbb8e41030021279969f89785009ea2adfd91ffce'
SCAN_SHA = 'a719e91d1611d96f5f28e4ff719042c4e8f46dc33fbdad9140f16475c82c6a54'
PREFIX = '.scratch/claude-freshcorpus-20260831/'
TARGETS = [
    'd1e35bebcbd796a059ef98e55aec1e4e2bb8c232a8d5dc9a8b72468919db8273',
    '1b01c9ed8a6294862ec0d0182f6ec644c235c1a5bc3e5cf0848c033202aef5e0',
    '5aed13452819d1cb1496b1690935edcd21e647b61c1f797920cf4cbc7dce98c4',
    'fae8adc3a92de900c0a3f4845bbd97dd2dff6073dc280483ed79eb439b457670',
]
PROBABILITY_FIELDS = {'win_probability', 'dk_fair_prob', 'raw_probability',
                      'conservative_probability', 'model_version', 'probability_components'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON constant: ' + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def read_regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('required regular file missing or symlink')
    return path.read_bytes()


def verify_bundle(bundle, expected_sha):
    """Read only manifest and named content-addressed blobs, never original paths."""
    if bundle.is_symlink() or (bundle / 'blobs').is_symlink():
        raise ValueError('symlink bundle refused')
    raw = read_regular(bundle / 'manifest.json')
    if sha(raw) != expected_sha:
        raise ValueError('manifest digest mismatch')
    manifest = decode(raw)
    if manifest['errors'] or manifest['skipped']:
        raise ValueError('incomplete inventory')
    paths, blobs = {}, {}
    totals = {root: {'files': 0, 'bytes': 0} for root in manifest['roots']}
    for entry in manifest['files']:
        name, digest, size, root = (entry[k] for k in ('path', 'sha256', 'bytes', 'root'))
        if (not isinstance(name, str) or PurePosixPath(name).is_absolute()
                or '..' in PurePosixPath(name).parts or name in paths):
            raise ValueError('invalid or duplicate manifest path')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid digest')
        if type(size) is not int or size < 0 or root not in totals or not name.startswith(root + '/'):
            raise ValueError('invalid manifest size/root')
        if digest not in blobs:
            blobs[digest] = read_regular(bundle / 'blobs' / digest)
        if len(blobs[digest]) != size or sha(blobs[digest]) != digest:
            raise ValueError('blob integrity mismatch')
        paths[name] = entry
        totals[root]['files'] += 1
        totals[root]['bytes'] += size
    if totals != manifest['roots'] or len(blobs) != manifest['unique_digests']:
        raise ValueError('inventory totals mismatch')
    return manifest, paths, blobs


def stamp(value):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('timestamp required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def source_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('positive numeric source ID required')
    return str(value)


def raw_american_price(value):
    """This archive serializes American prices as signed integer strings."""
    if isinstance(value, str) and re.fullmatch(r'[+-][1-9][0-9]*', value):
        value = int(value)
    return number(value) and abs(value) >= 100


def validate_split(split):
    dates = []
    for name in ('training_start', 'training_end', 'heldout_start', 'heldout_end'):
        value = split[name]
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('split requires ISO dates')
        dates.append(datetime.strptime(value, '%Y-%m-%d').date())
    first, last, heldout, end = dates
    cutoff = stamp(split['training_cutoff'])
    if (split['timezone'] != 'UTC' or not first <= last < cutoff.date() < heldout <= end
            or first.year != end.year):
        raise ValueError('split must be same-season, disjoint UTC date blocks')
    return cutoff


def field_census(value):
    result = Counter()
    if isinstance(value, dict):
        result.update(k for k in value if k in PROBABILITY_FIELDS)
        for child in value.values():
            result.update(field_census(child))
    elif isinstance(value, list):
        for child in value:
            result.update(field_census(child))
    return result


def import_candidates(paths, blobs):
    """Normalize only whitelisted outcomes. Never pass posterior features to a model."""
    outcomes, cache_files, receipts = [], 0, 0
    receipt_fields = Counter()
    for name, entry in sorted(paths.items()):
        if name.startswith(PREFIX + 'audit-results/') and name.endswith('.json'):
            cache_files += 1
            data = decode(blobs[entry['sha256']])
            fetched = stamp(data['_audit_fetched_at_utc']).isoformat()
            for di, day in enumerate(data['dates']):
                for gi, game in enumerate(day['games']):
                    away, home = game['teams']['away'], game['teams']['home']
                    a, h = source_id(away['team']['id']), source_id(home['team']['id'])
                    if a == h:
                        raise ValueError('identical teams')
                    outcomes.append({
                        'game_id': source_id(game['gamePk']), 'away_id': a, 'home_id': h,
                        'scheduled_start': stamp(game['gameDate']).isoformat(),
                        'status': game['status']['abstractGameState'],
                        'away_score': away.get('score'), 'home_score': home.get('score'),
                        'retrieved_at': fetched, 'actual_first_pitch': None, 'completed_at': None,
                        'source_sha256': entry['sha256'], 'pointer': f'/dates/{di}/games/{gi}',
                    })
        elif name.startswith(PREFIX + 'receipts/') and name.endswith('.json'):
            receipts += 1
            receipt_fields.update(field_census(decode(blobs[entry['sha256']])))
    ledger = decode(blobs[paths[PREFIX + 'picks.json']['sha256']])['picks']
    scan = decode(blobs[SCAN_SHA])
    if not isinstance(ledger, list) or not isinstance(scan, list):
        raise ValueError('invalid ledger or scan')
    return outcomes, ledger, scan, cache_files, receipts, receipt_fields


def checkpoint(bundle, rules_path):
    contract_raw = read_regular(CONTRACT_PATH)
    contract = decode(contract_raw)
    if contract['manifest_sha256'] != MANIFEST_SHA or contract['recovery_rules_sha256'] != RULES_SHA:
        raise ValueError('contract pins differ from adapter')
    if contract['scoring_enabled'] is not False:
        raise ValueError('scoring cannot be enabled')
    cutoff = validate_split(contract['split'])
    if sha(read_regular(rules_path)) != RULES_SHA:
        raise ValueError('recovery rules digest mismatch')
    manifest, paths, blobs = verify_bundle(bundle, MANIFEST_SHA)
    outcomes, ledger, scan, cache_files, receipt_count, receipt_fields = import_candidates(paths, blobs)
    groups = defaultdict(list)
    for row in outcomes:
        groups[row['game_id']].append(row)
    conflicts = sum(len({(r['away_id'], r['home_id'], r['scheduled_start']) for r in rows}) > 1
                    for rows in groups.values())
    final_conflicts = sum(len({(r['away_score'], r['home_score']) for r in rows if r['status'] == 'Final'}) > 1
                          for rows in groups.values())
    recovered = {
        'result_cache_files': cache_files, 'game_occurrences': len(outcomes),
        'unique_game_ids': len(groups),
        'unique_game_ids_with_final_status': len({r['game_id'] for r in outcomes if r['status'] == 'Final'}),
        'ledger_rows': len(ledger),
        'ledger_rows_with_win_and_dk_probabilities': sum(all(number(r.get(k)) for k in ('win_probability', 'dk_fair_prob')) for r in ledger),
        'ledger_rows_with_model_version': sum('model_version' in r for r in ledger),
        'receipt_json_files': receipt_count,
        'september_2_scan_rows': len(scan),
        'september_2_scan_rows_with_two_sided_odds_and_fair': sum(all(r.get(k) is not None for k in ('away_ml', 'home_ml', 'away_fair', 'home_fair')) for r in scan),
    }
    expected = [99, 1302, 1287, 1274, 45, 8, 0, 463, 15, 9]
    if list(recovered.values()) != expected or receipt_fields:
        raise ValueError('recovery census differs; investigate without admitting evidence')
    matches = {target: sum(e['sha256'] == target for e in manifest['files']) for target in TARGETS}
    if any(matches.values()):
        raise ValueError('prior missing scan finding changed')
    schedule_counts = {}
    for day, target in zip(range(4, 8), TARGETS):
        date = f'2026-09-{day:02}'
        name = f'.scratch/builder-mlb-model-audit/live/execute/{date}-schedule.json'
        schedule = decode(blobs[paths[name]['sha256']])
        denominator = schedule['slate_denominator']
        if schedule['date'] != date or denominator['scan_sha256'] != target:
            raise ValueError('schedule does not corroborate scan hash')
        keys = [source_id(row['game_pk']) for row in denominator['games']]
        if len(set(keys)) != len(keys):
            raise ValueError('duplicate schedule game ID')
        schedule_counts[date] = {'supplied_game_count': len(keys),
                                 'refused_game_count': len(keys),
                                 'reason': 'retained_scan_payload_missing',
                                 'complete_schedule_established': False}
    rows, ids = [], set()
    for index, row in enumerate(scan):
        key = source_id(row['game_pk'])
        if key in ids:
            raise ValueError('duplicate scan game ID')
        ids.add(key)
        start = stamp(row['time'])
        split = contract['split']
        if not split['heldout_start'] <= start.date().isoformat() <= split['heldout_end']:
            raise ValueError('scan outside declared heldout dates')
        reasons = ['source_version_unestablished', 'schedule_completeness_unestablished',
                   'stable_team_ids_unavailable', 'actual_first_pitch_unavailable',
                   'pregame_byte_binding_receipt_unavailable', 'same_book_observation_unavailable',
                   'joined_verified_final_unavailable']
        if key in groups:
            reasons.append('training_heldout_id_overlap')
        price_pair = all(raw_american_price(row.get(k)) for k in ('away_ml', 'home_ml'))
        if not price_pair:
            reasons.append('two_sided_price_missing_or_invalid')
        rows.append({'game_id': key, 'source_sha256': SCAN_SHA, 'pointer': f'/{index}',
                     'scheduled_start': start.isoformat(), 'raw_price_pair_present': price_pair,
                     'admitted': False, 'reasons': reasons})
    refusal_counts = Counter(reason for row in rows for reason in row['reasons'])
    # No source-specific authenticity verifier exists for this retained bundle.
    # Audit-time hashes and asserted timestamps cannot set any gate to passed.
    gates = {name: {'status': 'unavailable'} for name in contract['required_gates']}
    return {
        'schema': contract['schema'], 'mode': contract['mode'],
        'manifest_sha256': MANIFEST_SHA, 'rules_sha256': RULES_SHA,
        'contract_sha256': sha(contract_raw), 'implementation_sha256': sha(Path(__file__).read_bytes()),
        'normalized_outcome_candidates_sha256': sha(canonical(outcomes)),
        'inventory': {'roots': len(manifest['roots']), 'file_instances': len(paths),
                      'unique_file_digests': len(blobs), 'all_blobs_verified': True},
        'recovered': recovered, 'missing_scan_hash_matches': matches,
        'outcome_candidates': {'identity_or_start_conflict_game_ids': conflicts,
                               'final_score_conflict_game_ids': final_conflicts,
                               'retrieved_at_or_after_training_cutoff_occurrences': sum(stamp(r['retrieved_at']) >= cutoff for r in outcomes),
                               'timestamp_semantics': 'retrieval is not completion or pregame availability',
                               'admitted_training_games': 0,
                               'refused_unique_games': len(groups),
                               'reason_counts_nonexclusive': {'source_version_unestablished': len(groups),
                                                             'actual_completion_unavailable': len(groups),
                                                             'asof_training_availability_unestablished': len(groups)}},
        'selected_ledger': {'denominator_kind': 'selected_rows_not_schedule',
                            'refused_rows': len(ledger),
                            'reason_counts_nonexclusive': {'pregame_probability_byte_binding_unavailable': len(ledger)}},
        'split': contract['split'], 'gates': gates,
        'retained_schedule_denominators': schedule_counts,
        'coverage': {'complete_heldout_schedule_games': None,
                     'observed_scan_rows': len(rows), 'two_sided_raw_price_rows': sum(r['raw_price_pair_present'] for r in rows),
                     'authenticated_same_book_pregame_rows': 0, 'missing_scan_dates': 4,
                     'heldout_dates_without_scan_in_this_adapter': ['2026-09-03'],
                     'schedule_coverage_fraction': None, 'refused_scan_rows': len(rows),
                     'reason_counts_nonexclusive': dict(sorted(refusal_counts.items()))},
        'rows': rows, 'feasible': False, 'admitted_game_ids': [],
        'historical_performance': None, 'scoring_enabled': False, 'eligible': False,
        'next_step': 'Missing source gates require separately reviewed evidence; no scoring or expanded search.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--recovery-rules', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = checkpoint(args.bundle, args.recovery_rules)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(json.dumps({'schema': 'mlb-historical-admission-v1', 'feasible': False,
                          'scoring_enabled': False, 'error': str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 1  # verified checkpoint, evidence infeasible; never a scoring success


if __name__ == '__main__':
    raise SystemExit(main())
