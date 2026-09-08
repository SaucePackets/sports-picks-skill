#!/usr/bin/env python3
"""Acquire the fixed census and regular-season feeds; never fit or score."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.request

from mlb_chronological_checkpoint import dates, encoded
from mlb_real_shadow_capture import decode, sha

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'docs/mlb-chronological-contract.json'


def fetch(root, kind, key, url):
    receipt = root / (kind + '-' + key + '.json')
    if receipt.exists():
        existing = decode(receipt.read_bytes())
        if (existing['kind'], existing['key'], existing['url']) != (kind, key, url):
            raise ValueError('cached_receipt_identity_mismatch')
        if existing['body_sha256'] is not None:
            body = (root / 'objects' / existing['body_sha256']).read_bytes()
            if sha(body) != existing['body_sha256'] or len(body) != existing['size']:
                raise ValueError('cached_object_mismatch')
        return existing
    r = dict(kind=kind, key=key, url=url,
             retrieved_at_local=datetime.now(timezone.utc).isoformat(),
             http_status=None, headers={}, size=0, body_sha256=None, failure=None)
    body = None
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'MLB-chronological-research/1.0'})
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
            r.update(http_status=response.status, headers=dict(response.headers))
    except urllib.error.HTTPError as exc:
        body = exc.read()
        r.update(http_status=exc.code, headers=dict(exc.headers), failure='http_error')
    except (OSError, TimeoutError) as exc:
        r['failure'] = type(exc).__name__ + ': ' + str(exc)
    if body is not None:
        r.update(size=len(body), body_sha256=sha(body))
        target = root / 'objects' / r['body_sha256']
        if not target.exists():
            target.write_bytes(body)
    receipt.write_bytes(encoded(r))
    print(kind, key, r['http_status'], r['failure'], flush=True)
    return r


def acquire(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'objects').mkdir(exist_ok=True)
    if (root / 'manifest.json').exists():
        raise ValueError('manifest_exists_use_new_bundle')
    contract = decode(CONTRACT.read_bytes())
    days = [d for bounds in contract['splits'].values() for d in dates(bounds)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        sources = list(pool.map(lambda d: fetch(root, 'schedule', d,
            f'https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}&hydrate=linescore'), days))
        gids = set()
        for s in sources:
            if s['failure'] is None and s['body_sha256']:
                data = decode((root / 'objects' / s['body_sha256']).read_bytes())
                gids.update(str(g['gamePk']) for d in data['dates'] for g in d['games'] if g.get('gameType') == 'R')
        sources += list(pool.map(lambda g: fetch(root, 'feed', g,
            f'https://statsapi.mlb.com/api/v1.1/game/{g}/feed/live'), sorted(gids, key=int)))
    (root / 'manifest.json').write_bytes(encoded({
        'schema': 'mlb-chronological-source-v1', 'contract_sha256': sha(CONTRACT.read_bytes()),
        'acquisition_sha256': sha(Path(__file__).read_bytes()), 'sources': sources}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    acquire(parser.parse_args().bundle)
