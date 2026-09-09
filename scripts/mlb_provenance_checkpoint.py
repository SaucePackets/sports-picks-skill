#!/usr/bin/env python3
"""Verify pinned retained sources and their full offline replay. No acquisition."""
import argparse
from pathlib import Path

from mlb_bulk_starter_snapshots import replay
from mlb_chronological_admission import CONTRACT_PATH, ERRORS
from mlb_chronological_checkpoint import encoded
from mlb_market_free_checkpoint import require
from mlb_real_shadow_capture import decode, sha

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = ROOT / 'docs/mlb-provenance-checkpoint.json'


def checkpoint(bundle, snapshot_dir):
    # Repository-owned trust anchor: never selected from the supplied source tree.
    metadata_raw = CHECKPOINT.read_bytes()
    metadata = decode(metadata_raw)
    require(metadata['schema'] == 'mlb-provenance-checkpoint-v1', 'provenance_schema')
    for path, field in (
        (Path(bundle) / 'manifest.json', 'bundle_sha256'),
        (Path(snapshot_dir) / 'manifest.json', 'manifest_sha256'),
        (Path(snapshot_dir) / 'plan.json', 'plan_sha256'),
        (CONTRACT_PATH, 'contract_sha256'),
    ):
        require(not path.is_symlink(), 'provenance_symlink')
        require(sha(path.read_bytes()) == metadata[field], 'provenance_' + field + '_mismatch')
    manifest = decode((Path(snapshot_dir) / 'manifest.json').read_bytes())
    require(manifest['acquisition_sha256'] == metadata['acquisition_sha256'],
            'provenance_acquisition_mismatch')
    for name, digest in metadata['adapter_sha256'].items():
        require(Path(name).name == name, 'provenance_adapter_path')
        require(sha((ROOT / 'scripts' / name).read_bytes()) == digest,
                'provenance_adapter_mismatch')
    # Replay revalidates all retained bodies/receipts, including unused/refused inputs.
    # Matching top-level pins alone is insufficient: bytes underneath may be corrupt.
    report = replay(bundle, snapshot_dir)
    require(report['adapter_sha256'] == metadata['adapter_sha256'],
            'provenance_adapter_inventory_mismatch')
    output = encoded(report)
    require(type(metadata['replay_bytes']) is int and len(output) == metadata['replay_bytes']
            and sha(output) == metadata['replay_sha256'], 'provenance_replay_mismatch')
    return {'schema': 'mlb-provenance-verification-v1', 'pinned_replay_verified': True,
            'checkpoint_sha256': sha(metadata_raw), 'replay_sha256': sha(output),
            'bundle_sha256': metadata['bundle_sha256'],
            'manifest_sha256': metadata['manifest_sha256'],
            'feature_rows_created': 0, 'fitting_enabled': False, 'scoring_enabled': False,
            'eligible': False, 'historical_performance': None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--snapshot-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = checkpoint(args.bundle, args.snapshot_dir)
    except (OSError, *ERRORS) as exc:
        print(encoded({'error': str(exc), 'pinned_replay_verified': False,
                       'fitting_enabled': False, 'scoring_enabled': False,
                       'eligible': False, 'historical_performance': None}).decode(), end='')
        return 2
    print(encoded(result).decode(), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
