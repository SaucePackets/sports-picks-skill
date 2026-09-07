#!/usr/bin/env python3
"""Offline MLB shadow evidence. No live admission or runtime integration."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path

FAMILIES = ('market', 'team_strength', 'pitcher_context')
REQUIRED = {'team_strength': ['team_ratings', 'training_dataset'],
            'pitcher_context': ['starting_pitchers', 'bullpen', 'context', 'training_dataset']}
SPECS = {f: {'version': 1, 'family': f, 'side': 'away',
             'estimator': 'normalize_inverse_decimal_odds' if f == 'market' else 'unimplemented',
             'required': ['book', 'market', 'away_decimal', 'home_decimal'] if f == 'market' else REQUIRED[f]}
         for f in FAMILIES}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def identity(game):
    result = tuple(game[k] for k in ('game_id', 'away_id', 'home_id'))
    if any(not isinstance(x, str) or not x.strip() for x in result) or result[1] == result[2]:
        raise ValueError('invalid identity')
    timestamp(game['first_pitch'])
    return result


def same_game(left, right):
    return identity(left) == identity(right) and timestamp(left['first_pitch']) == timestamp(right['first_pitch'])


class Store:
    """Content addressed objects, atomic create-only links, no overwrite API.

    Not protection against an administrator rewriting disk. Every read rehashes.
    """
    def __init__(self, root):
        self.root = Path(root)
        for folder in ('objects', 'attempts', 'finals', 'closings'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def put(self, data, folder='objects'):
        if folder not in ('objects', 'attempts', 'finals', 'closings'):
            raise ValueError('unknown record collection')
        import tempfile
        key = digest(data)
        target = self.root / folder / key
        fd, temp = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                if target.read_bytes() != data:
                    raise ValueError('existing object corrupted')
        finally:
            os.unlink(temp)
        return key

    def get(self, key, folder='objects'):
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid digest')
        data = (self.root / folder / key).read_bytes()
        if digest(data) != key:
            raise ValueError('changed bytes')
        return data

    def records(self, folder):
        return [json.loads(self.get(p.name, folder)) for p in sorted((self.root / folder).iterdir())]

    def append(self, folder, record):
        return self.put(canonical(record), folder)


def prediction(family, source):
    if family not in FAMILIES:
        raise ValueError('unknown model')
    if family != 'market':
        return {'probability': None, 'missing': ['estimator_unimplemented'] + REQUIRED[family]}
    odds = source.get('odds', {})
    missing = [k for k in SPECS[family]['required'] if k not in odds]
    if missing:
        return {'probability': None, 'missing': missing}
    if set(odds) != set(SPECS['market']['required']):
        raise ValueError('ambiguous odds schema')
    if not all(isinstance(odds[k], str) and odds[k].strip() for k in ('book', 'market')):
        raise ValueError('book/market required')
    # One snapshot object supplies both sides; separate books/markets are not accepted.
    if odds['market'] != 'full_game_moneyline_including_extras':
        raise ValueError('unsupported market')
    a, h = odds['away_decimal'], odds['home_decimal']
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 1 for x in (a, h)):
        raise ValueError('invalid odds')
    return {'probability': (1 / a) / (1 / a + 1 / h), 'missing': []}


def capture(store, game, source_bytes, family, producer_key, training_cutoff):
    """Retain source and deterministic bundle. Receipt is appended separately."""
    identity(game)
    source = json.loads(source_bytes)
    if not same_game(game, source['game']):
        raise ValueError('source identity mismatch')
    if not isinstance(source['source_id'], str) or not source['source_id'].strip():
        raise ValueError('missing source identity')
    timestamp(source['observed_at'])
    timestamp(training_cutoff)
    spec = SPECS[family]
    bundle = {'schema': 1, 'game': game, 'source_digest': store.put(source_bytes),
              'source_id': source['source_id'], 'observed_at': source['observed_at'],
              'producer_key': producer_key, 'family': family, 'model_version': 1,
              'spec_digest': digest(canonical(spec)),
              'artifact_digest': digest(Path(__file__).read_bytes()),
              'training_cutoff': training_cutoff, 'prediction': prediction(family, source)}
    return store.put(canonical(bundle))


def receipt_payload(bundle_digest, observed_at, witness_key):
    return {'schema': 1, 'purpose': 'mlb-shadow-fixture-only', 'bundle_digest': bundle_digest,
            'observed_at': observed_at, 'witness_key': witness_key}


def verify_fixture(receipt, bundle_digest, bundle, trusted_keys):
    """Real signature verification, TEST trust domain only; never live evidence."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        payload = receipt['payload']
        key = payload['witness_key']
        if key not in trusted_keys or key == bundle['producer_key']:
            return 'non_independent_or_untrusted_witness'
        expected = receipt_payload(bundle_digest, payload['observed_at'], key)
        if payload != expected:
            return 'invalid_receipt_binding'
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key)).verify(
            bytes.fromhex(receipt['signature']), canonical(payload))
        observed = timestamp(payload['observed_at'])
        if observed >= timestamp(bundle['game']['first_pitch']):
            return 'late_receipt'
        if observed < timestamp(bundle['observed_at']) or observed < timestamp(bundle['training_cutoff']):
            return 'receipt_before_inputs'
    except (KeyError, TypeError, ValueError, InvalidSignature):
        return 'invalid_receipt'
    return None


def assess(store, attempt, game, family, trusted_keys):
    try:
        if attempt['game_id'] != game['game_id'] or attempt['family'] != family:
            raise ValueError('attempt_identity_mismatch')
        raw = store.get(attempt['bundle_digest'])
        bundle = json.loads(raw)
        source = json.loads(store.get(bundle['source_digest']))
        if raw != canonical(bundle) or type(bundle['schema']) is not int or bundle['schema'] != 1:
            raise ValueError('noncanonical_bundle')
        if not same_game(game, bundle['game']) or not same_game(game, source['game']):
            raise ValueError('identity_mismatch')
        if (bundle['family'] != family or type(bundle['model_version']) is not int or bundle['model_version'] != 1 or
                bundle['spec_digest'] != digest(canonical(SPECS[family])) or
                bundle['artifact_digest'] != digest(Path(__file__).read_bytes())):
            raise ValueError('unknown_model')
        if not isinstance(source['source_id'], str) or not source['source_id'].strip():
            raise ValueError('missing_source_identity')
        producer = bundle['producer_key']
        if not isinstance(producer, str) or len(producer) != 64 or any(c not in '0123456789abcdef' for c in producer):
            raise ValueError('invalid_producer_key')
        if bundle['source_id'] != source['source_id'] or bundle['observed_at'] != source['observed_at']:
            raise ValueError('source_metadata_mismatch')
        if bundle['prediction'] != prediction(family, source):
            raise ValueError('prediction_not_reproducible')
        if timestamp(bundle['training_cutoff']) > timestamp(bundle['observed_at']):
            raise ValueError('future_training_cutoff')
        if timestamp(bundle['observed_at']) >= timestamp(game['first_pitch']):
            return 'late', 'late_source', bundle
        if bundle['prediction']['missing']:
            return 'missing', bundle['prediction']['missing'], bundle
        if not attempt.get('receipt'):
            return 'captured', 'missing_receipt', bundle
        reason = verify_fixture(attempt['receipt'], attempt['bundle_digest'], bundle, trusted_keys)
        if reason:
            return ('late' if reason == 'late_receipt' else 'invalid'), reason, bundle
        return 'captured', 'fixture_verified_live_disabled', bundle
    except (KeyError, ValueError, TypeError, OSError) as exc:
        return 'invalid', str(exc), None


def join_observation(store, records, game, kind, bundle, capture_time):
    matched = []
    for record in records:
        # All bytes rehashed; identities and first pitch corroborate schedule.
        source = json.loads(store.get(record['source_digest']))
        if source.get('game', {}).get('game_id') != game['game_id']:
            continue
        if not same_game(game, source['game']) or not source.get('source_id'):
            return {'status': 'invalid', 'reason': 'identity_or_source_mismatch'}
        observed = timestamp(source['observed_at'])
        if kind == 'finals':
            scores = [source.get('away_score'), source.get('home_score')]
            if source.get('status') != 'Final' or observed <= timestamp(game['first_pitch']) or any(type(x) is not int or x < 0 for x in scores) or scores[0] == scores[1]:
                return {'status': 'invalid', 'reason': 'invalid_final'}
            matched.append({'status': 'joined', 'away_won': scores[0] > scores[1]})
        else:
            opening = json.loads(store.get(bundle['source_digest'])).get('odds', {})
            odds = source.get('odds', {})
            if not (timestamp(capture_time) <= observed < timestamp(game['first_pitch'])) or any(odds.get(k) != opening.get(k) for k in ('book', 'market')):
                return {'status': 'unavailable', 'reason': 'noncomparable_closing'}
            pred = prediction('market', source)
            if pred['probability'] is None:
                return {'status': 'unavailable', 'reason': 'missing_closing_odds'}
            matched.append({'status': 'joined', 'away_fair_probability': pred['probability']})
    if len(matched) > 1:
        return {'status': 'invalid', 'reason': 'ambiguous_observations'}
    return matched[0] if matched else {'status': 'unavailable', 'reason': 'missing_observation'}


def report(store, schedule, trusted_fixture_keys=()):
    """Full denominator. Earliest verified witness time then bundle hash wins.

    Eligibility and shared eligible cohort are unconditionally disabled.
    """
    ids = [identity(g)[0] for g in schedule]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate_schedule_game')
    attempts = store.records('attempts')
    finals, closings = store.records('finals'), store.records('closings')
    unmatched = [a for a in attempts if a.get('game_id') not in ids or a.get('family') not in FAMILIES]
    rows = []
    for game in schedule:
        for family in FAMILIES:
            candidates = []
            for attempt in attempts:
                if attempt.get('game_id') != game['game_id'] or attempt.get('family') != family:
                    continue
                state, reason, bundle = assess(store, attempt, game, family, trusted_fixture_keys)
                candidates.append({'attempt_digest': digest(canonical(attempt)), 'bundle_digest': attempt.get('bundle_digest'),
                                   'state': state, 'reason': reason, 'bundle': bundle, 'receipt': attempt.get('receipt')})
            valid = [c for c in candidates if c['reason'] == 'fixture_verified_live_disabled']
            valid.sort(key=lambda c: (timestamp(c['receipt']['payload']['observed_at']), c['bundle_digest'], c['attempt_digest']))
            chosen = valid[0] if valid else None
            row = {'game_id': game['game_id'], 'family': family, 'eligible': False,
                   'state': chosen['state'] if chosen else (candidates[0]['state'] if candidates else 'missing'),
                   'reason': chosen['reason'] if chosen else (candidates[0]['reason'] if candidates else 'no_attempt'),
                   'fixture_contract_pass': bool(chosen), 'selected_fixture_bundle': chosen['bundle_digest'] if chosen else None,
                   'attempts': [{k: v for k, v in c.items() if k not in ('bundle', 'receipt')} for c in candidates]}
            for kind, records in (('finals', finals), ('closings', closings)):
                try:
                    row[kind] = join_observation(store, records, game, kind, chosen['bundle'], chosen['receipt']['payload']['observed_at']) if chosen else {'status': 'unavailable', 'reason': 'no_verified_fixture_prediction'}
                except (KeyError, ValueError, TypeError, OSError):
                    row[kind] = {'status': 'invalid', 'reason': 'malformed_observation'}
            rows.append(row)
    cohorts = [{r['game_id'] for r in rows if r['family'] == f and r['eligible']} for f in FAMILIES]
    return {'schema': 1, 'mode': 'offline_fixture_only', 'live_eligibility_enabled': False,
            'scheduled_games': len(schedule), 'rows': rows, 'unmatched_attempts': unmatched, 'shared_eligible_game_ids': sorted(set.intersection(*cohorts))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--schedule', required=True)
    parser.add_argument('--fixture-keys', help='JSON array of trusted TEST public keys')
    args = parser.parse_args()
    keys = json.loads(Path(args.fixture_keys).read_bytes()) if args.fixture_keys else []
    print(json.dumps(report(Store(args.store), json.loads(Path(args.schedule).read_bytes()), keys), indent=2))


if __name__ == '__main__':
    main()
