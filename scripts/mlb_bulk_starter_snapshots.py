#!/usr/bin/env python3
"""Bounded retained starter acquisition and offline source replay; no feature rows."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import urllib.error
import urllib.request

from mlb_bulk_starter_admission import appearances, snapshots, starter
from mlb_chronological_admission import CONTRACT_PATH, ERRORS, contract, load_bundle
from mlb_chronological_checkpoint import dates, encoded
from mlb_chronological_starter_probe import timestamp
from mlb_market_free_checkpoint import interrupted_status, require, schedule_census
from mlb_real_shadow_capture import decode, instant, sha

LIMITS = {'workers': 3, 'timeout_seconds': 20, 'response_bytes': 16 * 1024 * 1024,
          'requests_per_game': 2, 'retries': 0, 'redirects': 0}


def plan(bundle):
    digest, sources = load_bundle(bundle)
    census, games = [], []
    for split, bounds in contract()['splits'].items():
        for day in dates(bounds):
            pair = sources.get(('schedule', day))
            row = dict(date=day, split=split, occurrences=None, regular_occurrences=None, refusal=None)
            try:
                require(pair is not None and pair[1] is not None, 'schedule_unavailable')
                found = schedule_census(pair[1], day)
                require(all(isinstance(g['raw_game'].get('gameType'), str) and g['raw_game']['gameType']
                            for g in found), 'missing_game_type')
                row.update(occurrences=len(found), regular_occurrences=sum(g['raw_game']['gameType'] == 'R' for g in found))
                games.extend(g | {'split': split, 'schedule_sha256': pair[0]['body_sha256']} for g in found)
            except ERRORS as exc:
                row['refusal'] = str(exc)
            census.append(row)
    complete = all(r['refusal'] is None for r in census)
    counts = Counter(g['game_id'] for g in games)
    targets = []
    for g in games:
        row = {k: g[k] for k in ('game_id', 'source_date', 'split', 'scheduled_start', 'schedule_sha256', 'source_pointer')}
        row.update(cutoff=None, refusal=None)
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
            row['cutoff'] = cutoff.isoformat()
        except ERRORS as exc:
            row['refusal'] = str(exc)
        targets.append(row)
    return {'bundle_sha256': digest, 'contract_sha256': sha(CONTRACT_PATH.read_bytes()),
            'limits': LIMITS, 'census_complete': complete, 'census': census, 'targets': targets}, games, sources


def selected_code(body, cutoff):
    require(body is not None, 'timestamp_source_unavailable')
    codes = decode(body)
    require(isinstance(codes, list) and all(isinstance(c, str) for c in codes)
            and len(codes) == len(set(codes)), 'invalid_timestamp_index')
    candidates = [c for c in codes if timestamp(c) < instant(cutoff)]
    require(bool(candidates), 'no_provider_snapshot_before_cutoff')
    return max(candidates, key=timestamp)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(root, kind, gid, url):
    path = root / f'{kind}-{gid}.json'
    if path.exists():
        receipt = decode(path.read_bytes())  # all cached objects validated before workers start
        require((receipt['kind'], receipt['key'], receipt['url']) == (kind, gid, url), 'cached_identity')
        return receipt
    receipt = dict(kind=kind, key=gid, url=url, retrieved_at_local=datetime.now(timezone.utc).isoformat(),
                   http_status=None, headers={}, size=0, body_sha256=None, failure=None)
    body = None
    try:
        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(url, headers={'User-Agent': 'MLB-retained-starter-research/1.0'})
        try:
            response = opener.open(request, timeout=LIMITS['timeout_seconds'])
        except urllib.error.HTTPError as exc:
            response = exc
            receipt['failure'] = 'http_error'
        with response:
            receipt.update(http_status=response.code, headers=dict(response.headers))
            body = response.read(LIMITS['response_bytes'] + 1)
        if len(body) > LIMITS['response_bytes']:
            receipt['failure'] = 'response_size_limit_exceeded'  # retained prefix is never evidence
        elif receipt['http_status'] != 200:
            receipt['failure'] = 'http_error'
    except (OSError, TimeoutError) as exc:
        receipt['failure'] = type(exc).__name__
    if body is not None:
        receipt.update(size=len(body), body_sha256=sha(body))
        obj = root / 'objects' / receipt['body_sha256']
        try:
            with obj.open('xb') as stream:
                stream.write(body)
        except FileExistsError:
            require(not obj.is_symlink() and obj.read_bytes() == body, 'object_collision')
    receipt['completed_at_local'] = datetime.now(timezone.utc).isoformat()
    with path.open('xb') as stream:
        stream.write(encoded(receipt))
    return receipt


def validated_snapshots(root):
    """Enforce this acquirer's receipt contract before loading any cached bodies."""
    root = Path(root)
    require(root.is_dir() and not root.is_symlink() and not (root / 'objects').is_symlink(),
            'snapshot_root_invalid')
    for path in sorted(root.glob('*.json')):
        if not path.name.startswith(('timestamps-', 'snapshot-')):
            continue
        require(not path.is_symlink(), 'snapshot_receipt_symlink')
        r = decode(path.read_bytes())
        require(isinstance(r, dict), 'acquisition_receipt_schema')
        started = instant(r['retrieved_at_local'])
        completed = instant(r['completed_at_local'])
        require(started <= completed, 'acquisition_completion_before_start')
        require(isinstance(r['headers'], dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in r['headers'].items()),
                'acquisition_headers')
        status, failure, size = r['http_status'], r['failure'], r['size']
        require(status is None or (type(status) is int and 100 <= status <= 599), 'acquisition_http_status')
        require(failure is None or (isinstance(failure, str) and bool(failure)), 'acquisition_failure')
        require(type(size) is int and size >= 0, 'acquisition_size')
        # fetch retains one sentinel byte beyond the ceiling only as a refused prefix.
        if failure == 'response_size_limit_exceeded':
            require(size == LIMITS['response_bytes'] + 1 and status is not None,
                    'acquisition_oversize_prefix')
        else:
            require(size <= LIMITS['response_bytes'], 'acquisition_response_size_limit')
        digest = r['body_sha256']
        if digest is None:
            require(size == 0 and failure is not None, 'acquisition_missing_body')
        else:
            require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'snapshot_digest')
            obj = root / 'objects' / digest
            require(not obj.is_symlink(), 'snapshot_object_symlink')
            require(obj.stat().st_size == size, 'snapshot_integrity')
        require(failure is not None or (status == 200 and digest is not None), 'acquisition_success')
    return snapshots(root)


def acquire(bundle, root):
    root = Path(root)
    expected, _, _ = plan(bundle)
    require(not root.is_symlink() and not (root / 'objects').is_symlink(), 'snapshot_root_invalid')
    root.mkdir(parents=True, exist_ok=True)
    (root / 'objects').mkdir(exist_ok=True)
    plan_path = root / 'plan.json'
    require(not plan_path.is_symlink(), 'plan_symlink')
    if plan_path.exists():
        require(plan_path.read_bytes() == encoded(expected), 'acquisition_plan_changed')
    else:
        require(not list(root.iterdir()) or list(root.iterdir()) == [root / 'objects'], 'unplanned_existing_files')
        require(not list((root / 'objects').iterdir()), 'unplanned_existing_objects')
        with plan_path.open('xb') as stream:
            stream.write(encoded(expected))
    require(not (root / 'manifest.json').is_symlink(), 'snapshot_manifest_symlink')
    cached, _ = validated_snapshots(root)
    allowed = {r['game_id'] for r in expected['targets'] if r['refusal'] is None}
    require(all(gid in allowed for _, gid in cached), 'receipt_outside_plan')
    cutoffs = {r['game_id']: r['cutoff'] for r in expected['targets'] if r['refusal'] is None}
    for kind, gid in cached:
        if kind == 'snapshot':
            require(('timestamps', gid) in cached, 'cached_index_missing')
            code = selected_code(cached['timestamps', gid][1], cutoffs[gid])
            require(cached[kind, gid][0]['url'].endswith('?timecode=' + code), 'cached_snapshot_selection')
    if (root / 'manifest.json').exists():
        return replay(bundle, root)  # sealed runs never retry or overwrite failures

    def one(row):
        if row['refusal'] is not None:
            return
        gid = row['game_id']
        url = f'https://statsapi.mlb.com/api/v1.1/game/{gid}/feed/live'
        receipt = fetch(root, 'timestamps', gid, url + '/timestamps')
        body = (root / 'objects' / receipt['body_sha256']).read_bytes() if receipt['failure'] is None else None
        try:
            code = selected_code(body, row['cutoff'])
        except ERRORS:
            return
        fetch(root, 'snapshot', gid, url + '?timecode=' + code)

    with ThreadPoolExecutor(max_workers=LIMITS['workers']) as pool:
        list(pool.map(one, expected['targets']))
    _, bindings = validated_snapshots(root)
    manifest = dict(schema='mlb-bulk-starter-snapshots-v1', plan_sha256=sha(encoded(expected)),
                    acquisition_sha256=sha(Path(__file__).read_bytes()), receipts=bindings,
                    sealed_at_local=datetime.now(timezone.utc).isoformat())
    with (root / 'manifest.json').open('xb') as stream:
        stream.write(encoded(manifest))
    return replay(bundle, root)


def replay(bundle, root):
    root = Path(root)
    expected, games, feeds = plan(bundle)
    for name in ('plan.json', 'manifest.json'):
        require(not (root / name).is_symlink(), 'snapshot_manifest_symlink')
    require((root / 'plan.json').read_bytes() == encoded(expected), 'acquisition_plan_changed')
    manifest_raw = (root / 'manifest.json').read_bytes()
    manifest = decode(manifest_raw)
    require(manifest['schema'] == 'mlb-bulk-starter-snapshots-v1'
            and manifest['plan_sha256'] == sha(encoded(expected)), 'snapshot_manifest_identity')
    instant(manifest['sealed_at_local'])
    retained, bindings = validated_snapshots(root)
    require(manifest['receipts'] == bindings, 'snapshot_manifest_receipts_changed')
    wanted, rows, appearance_rows = set(), [], []
    counts = Counter(g['game_id'] for g in games)
    for target, g in zip(expected['targets'], games):
        row = target | {'starter': None, 'acquisition_refusal': None}
        gid = g['game_id']
        if row['refusal'] is None:
            wanted.add(('timestamps', gid))
            pair = retained.get(('timestamps', gid))
            require(pair is not None, 'planned_timestamp_receipt_missing')
            try:
                code = selected_code(pair[1], row['cutoff'])
                wanted.add(('snapshot', gid))
                require(('snapshot', gid) in retained, 'planned_snapshot_receipt_missing')
                row['starter'] = starter(g, retained, instant(row['cutoff']))
            except ERRORS as exc:
                # Missing planned receipts are integrity failures, not provider missingness.
                if str(exc) == 'planned_snapshot_receipt_missing':
                    raise
                row['acquisition_refusal'] = str(exc)
        rows.append(row)
        if g['raw_game']['gameType'] == 'R':
            appearance = {k: g[k] for k in ('game_id', 'source_date', 'split')}
            appearance.update(refusal=None, corroborated_appearances=None)
            try:
                require(expected['census_complete'], 'schedule_census_unknown')
                require(counts[gid] == 1, 'repeated_game_across_source_dates')
                pair = feeds.get(('feed', gid))
                require(pair is not None and pair[1] is not None, 'feed_unavailable')
                appearance['corroborated_appearances'] = len(appearances(g, pair[1]))
            except ERRORS as exc:
                appearance['refusal'] = str(exc)
            appearance_rows.append(appearance)
    require(set(retained) == wanted, 'receipt_outside_selected_plan')
    summary = {}
    for split in contract()['splits']:
        selected = [r for r in rows if r['split'] == split]
        days = [r for r in expected['census'] if r['split'] == split]
        summary[split] = dict(known_dates=sum(r['refusal'] is None for r in days),
            occurrences=sum(r['occurrences'] for r in days) if all(r['refusal'] is None for r in days) else None,
            regular_occurrences=sum(r['regular_occurrences'] for r in days) if all(r['refusal'] is None for r in days) else None,
            starter_candidates=sum(r['starter'] is not None for r in selected),
            refusals=dict(Counter(r['refusal'] or r['acquisition_refusal'] for r in selected if r['refusal'] or r['acquisition_refusal'])))
    return dict(schema='mlb-bulk-starter-snapshot-replay-v1', manifest_sha256=sha(manifest_raw),
        plan=expected, receipts=bindings, summary=summary, occurrences=rows, appearance_games=appearance_rows,
        acquired_responses=sum(p[1] is not None for p in retained.values()),
        inherited_missing_source_refusals=1243,
        prior_appearance_census_unresolved=sum(r['refusal'] is not None for r in appearance_rows),
        feature_rows_created=0, fitting_enabled=False, scoring_enabled=False, eligible=False,
        historical_performance=None, historical_availability='provider timestamp metadata only; independently unverified',
        adapter_sha256={name: sha(Path(__file__).with_name(name).read_bytes()) for name in
            ('mlb_bulk_starter_snapshots.py', 'mlb_bulk_starter_admission.py', 'mlb_chronological_admission.py',
             'mlb_chronological_starter_probe.py', 'mlb_chronological_checkpoint.py',
             'mlb_market_free_checkpoint.py', 'mlb_real_shadow_capture.py')})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--snapshot-dir', required=True, type=Path)
    parser.add_argument('--acquire', action='store_true')
    args = parser.parse_args()
    try:
        report = (acquire if args.acquire else replay)(args.bundle, args.snapshot_dir)
        print(encoded(report).decode(), end='')
    except (OSError, *ERRORS) as exc:
        print(encoded({'error': str(exc), 'fitting_enabled': False, 'scoring_enabled': False,
                       'eligible': False}).decode(), end='')
        raise SystemExit(2)
