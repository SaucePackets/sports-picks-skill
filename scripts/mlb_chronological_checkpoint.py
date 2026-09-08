#!/usr/bin/env python3
"""Verify the retained checkpoint offline; report longer evaluation as blocked."""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
import json
from pathlib import Path

import mlb_market_free_checkpoint as replay

ROOT = Path(__file__).resolve().parents[1]


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def dates(bounds):
    start, end = map(date.fromisoformat, bounds)
    return [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)]


def checkpoint(bundle):
    # Pins are repository-owned, never supplied by the bundle or caller.
    metadata = json.loads((ROOT / 'docs/mlb-market-free-checkpoint.json').read_bytes())
    contract_bytes = (ROOT / 'docs/mlb-chronological-contract.json').read_bytes()
    contract = json.loads(contract_bytes)
    replay.require(replay.sha((Path(bundle) / 'manifest.json').read_bytes()) == metadata['manifest_sha256'],
                   'retained_manifest_mismatch')
    for filename, field in [('mlb_market_free_checkpoint.py', 'implementation_sha256'),
                            ('mlb_real_shadow_capture.py', 'source_adapter_sha256')]:
        replay.require(replay.sha((ROOT / 'scripts' / filename).read_bytes()) == metadata[field],
                       'replay_implementation_mismatch')
    # This reconstructs ONLY the already published short-window baselines.
    # No longer-window features, challenger fit or performance score is computed.
    report = replay.checkpoint(bundle)
    output = encoded(report)
    replay.require(len(output) == metadata['report_bytes']
                   and replay.sha(output) == metadata['report_sha256'], 'retained_report_mismatch')
    replay.require(report['checkpoint_complete'], 'retained_checkpoint_incomplete')
    missing = {split: sorted(set(dates(bounds)) - set(report['schedule_counts']))
               for split, bounds in contract['splits'].items()}
    return {
        'schema': 'mlb-chronological-checkpoint-v1',
        'retained_replay_verified': True,
        'manifest_sha256': metadata['manifest_sha256'],
        'replayed_report_sha256': replay.sha(output),
        'contract_sha256': replay.sha(contract_bytes),
        'retained_training_accepted': len(report['training']),
        'retained_training_refused': len(report['training_refusals']),
        'retained_evaluation_rows': len(report['predictions']),
        'missing_schedule_dates': missing,
        'longer_evaluation_admitted': False,
        'blockers': ['longer_window_source_census_not_retained',
                     'lagged_pitcher_inputs_and_precutoff_starter_identity_not_admitted',
                     'longer_window_labels_not_admitted',
                     'longer_window_admission_adapter_not_implemented'],
        'fitting_enabled': False, 'scoring_enabled': False, 'eligible': False,
        'historical_performance': None,
        'asof_snapshot_verified': False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = checkpoint(args.bundle)
        print(encoded(result).decode(), end='')
        return 1  # Expected: replay verified, longer evaluation blocked.
    except (OSError, KeyError, ValueError, TypeError, IndexError, csv.Error) as exc:
        print(encoded({'error': str(exc), 'retained_replay_verified': False,
                       'longer_evaluation_admitted': False, 'fitting_enabled': False,
                       'scoring_enabled': False, 'eligible': False,
                       'historical_performance': None}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
