#!/usr/bin/env python3
"""Bounded offline classification of the pinned 326 refused occurrences."""
import argparse
from collections import Counter
from pathlib import Path

from mlb_bulk_starter_admission import PA_EVENTS, appearances
from mlb_bulk_starter_snapshots import plan
from mlb_chronological_admission import ERRORS
from mlb_chronological_checkpoint import encoded
from mlb_real_shadow_capture import decode, positive_id, sha
from mlb_market_free_checkpoint import require

CHECKPOINT = Path(__file__).resolve().parents[1] / 'docs/mlb-bulk-starter-snapshots.json'
RUNNER_OUT_EVENTS = frozenset('caught_stealing_2b caught_stealing_3b caught_stealing_home '
    'pickoff_1b pickoff_2b pickoff_3b pickoff_caught_stealing_2b '
    'pickoff_caught_stealing_3b'.split())
GROUPS = {'unsupported_plate_appearance_event': 'unsupported_pa',
          'repeated_game_across_source_dates': 'duplicate_ids',
          'ambiguous_mid_appearance_pitcher_substitution': 'ambiguous_substitutions',
          'play_before_scheduled_start': 'before_start'}
EXPECTED = {'unsupported_pa': 221, 'duplicate_ids': 40, 'ambiguous_substitutions': 28,
            'before_start': 22, 'other': 15}


def runner_out(play):
    """Only corroborated third-out runner plays with an unfinished batter count.

    A result label alone cannot suppress a PA. Require matching runner movement,
    a distinct baserunner, and a valid play-event reference. Unknown, scoring,
    generic other_out, and zero-PA pitching appearances remain refused.
    """
    result = play['result']
    if result['type'] != 'atBat' or result['eventType'] not in RUNNER_OUT_EVENTS:
        return False
    count = play['count']
    require(all(type(count[k]) is int for k in ('balls', 'strikes', 'outs'))
            and 0 <= count['balls'] < 4 and 0 <= count['strikes'] < 3 and count['outs'] == 3,
            'non_pa_unfinished_count_required')
    require(result['isOut'] is True and play['about']['isComplete'] is True
            and play['about']['isScoringPlay'] is False, 'non_pa_terminal_runner_out_required')
    batter = positive_id(play['matchup']['batter']['id'])
    runners, events = play['runners'], play['playEvents']
    require(isinstance(runners, list) and bool(runners) and isinstance(events, list)
            and bool(events), 'non_pa_runner_evidence_missing')
    out_base = {
        "caught_stealing_2b": "2B", "caught_stealing_3b": "3B", "caught_stealing_home": "4B",
        "pickoff_1b": "1B", "pickoff_2b": "2B", "pickoff_3b": "3B",
        "pickoff_caught_stealing_2b": "2B", "pickoff_caught_stealing_3b": "3B",
    }[result["eventType"]]
    require(all(e["details"].get("eventType") not in PA_EVENTS
                and e["details"].get("isInPlay") is not True for e in events),
            "non_pa_terminal_pa_conflict")
    matched = 0
    for runner in runners:
        movement, details = runner['movement'], runner['details']
        require(positive_id(details['runner']['id']) != batter
                and movement['start'] in ('1B', '2B', '3B'), 'non_pa_batter_or_origin_conflict')
        require(details['isScoringEvent'] is False, 'non_pa_scoring_conflict')
        index = details['playIndex']
        require(type(index) is int and 0 <= index < len(events)
                and type(events[index]['index']) is int and events[index]['index'] == index, 'non_pa_event_reference')
        if details['eventType'] == result['eventType'] and movement['isOut'] is True:
            require(events[index]['details'].get('isOut') is True, 'non_pa_referenced_event_not_out')
            matched += int(type(movement['outNumber']) is int and movement['outNumber'] == 3
                        and movement['end'] is None and movement['outBase'] == out_base)
    require(matched == 1, 'non_pa_runner_out_not_corroborated')
    return True


def inspect_appearance(game, pair, complete, multiplicity, classifier=None):
    trace = {'stage': 'schedule', 'feed_pointer': None}
    records, refusal = None, None
    try:
        require(complete, 'schedule_census_unknown')
        require(multiplicity[game['game_id']] == 1, 'repeated_game_across_source_dates')
        require(pair is not None and pair[1] is not None, 'feed_unavailable')
        records = appearances(game, pair[1], non_pa_classifier=classifier, trace=trace)
    except ERRORS as exc:
        refusal = str(exc)
    return records, refusal, trace


def play_inventory(body):
    """All play pointers, including supported PAs behind the first refusal."""
    if body is None:
        return []
    data = decode(body)
    rows = []
    for i, p in enumerate(data.get('liveData', {}).get('plays', {}).get('allPlays', [])):
        row = {'pointer': f'/liveData/plays/allPlays/{i}',
               'event_type': p.get('result', {}).get('eventType'),
               'pitcher_id': p.get('matchup', {}).get('pitcher', {}).get('id'),
               'batter_id': p.get('matchup', {}).get('batter', {}).get('id'),
               'start_time': p.get('about', {}).get('startTime'),
               'end_time': p.get('about', {}).get('endTime'),
               'classification': 'completed_pa_event', 'refusal': None}
        if p.get('result', {}).get('type') != 'atBat' or row['event_type'] not in PA_EVENTS:
            try:
                supported = runner_out(p)
                row['classification'] = 'corroborated_non_pa_runner_out' if supported else 'insufficient_evidence'
                if not supported:
                    row['refusal'] = 'unsupported_plate_appearance_event'
            except ERRORS as exc:
                row.update(classification='insufficient_evidence', refusal=str(exc))
        rows.append(row)
    return rows


def classify(games, sources, complete, baseline):
    multiplicity = Counter(g['game_id'] for g in games)
    old_rows, refused = [], []
    for g in games:
        if g['raw_game']['gameType'] != 'R':
            continue
        pair = sources.get(('feed', g['game_id']))
        records, reason, trace = inspect_appearance(g, pair, complete, multiplicity)
        old_rows.append({k: g[k] for k in ('game_id', 'source_date', 'split')} |
                        {'refusal': reason, 'corroborated_appearances': len(records) if records is not None else None})
        if reason is None:
            continue
        recovered, remaining, after_trace = (None, reason, trace)
        if reason == 'unsupported_plate_appearance_event':
            recovered, remaining, after_trace = inspect_appearance(g, pair, complete, multiplicity, runner_out)
        refused.append({k: g[k] for k in ('game_id', 'source_date', 'split', 'scheduled_start', 'schedule_sha256')} |
            {'schedule_pointer': g['source_pointer'], 'feed_sha256': pair[0]['body_sha256'] if pair else None,
             'baseline_refusal': reason, 'group': GROUPS.get(reason, 'other'), 'baseline_gate': trace,
             'classification': 'recovered_appearance_accounting' if remaining is None else 'still_refused',
             'remaining_refusal': remaining, 'remaining_gate': after_trace if remaining else None,
             'appearances': recovered, 'plays': play_inventory(pair[1] if pair else None)})
    require(old_rows == baseline['appearance_games'], 'baseline_occurrence_replay_mismatch')
    groups = Counter(r['group'] for r in refused)
    require(dict(groups) == EXPECTED and len(refused) == 326, 'baseline_denominator_mismatch')
    return {'regular_occurrences': len(old_rows), 'baseline_refused_occurrences': len(refused),
            'groups': {k: {'occurrences': groups[k],
                          'recovered': sum(r['group'] == k and r['remaining_refusal'] is None for r in refused),
                          'remaining_refusals': dict(sorted(Counter(r['remaining_refusal'] for r in refused
                            if r['group'] == k and r['remaining_refusal'] is not None).items()))}
                       for k in EXPECTED}, 'refused_occurrences': refused}


def census(bundle, baseline_path):
    checkpoint = decode(CHECKPOINT.read_bytes())
    raw = Path(baseline_path).read_bytes()
    require(len(raw) == checkpoint['replay_bytes'] and sha(raw) == checkpoint['replay_sha256'],
            'baseline_replay_integrity')
    baseline = decode(raw)
    planned, games, sources = plan(bundle)
    require(planned['bundle_sha256'] == checkpoint['bundle_sha256'], 'baseline_bundle_mismatch')
    report = classify(games, sources, planned['census_complete'], baseline)
    return report | {'schema': 'mlb-appearance-census-v1', 'bundle_sha256': planned['bundle_sha256'],
                     'baseline_replay_sha256': sha(raw), 'contract_sha256': planned['contract_sha256'],
                     'census': planned['census'],
                     'adapter_sha256': {name: sha(Path(__file__).with_name(name).read_bytes()) for name in
                        ('mlb_appearance_census.py', 'mlb_bulk_starter_admission.py', 'mlb_bulk_starter_snapshots.py',
                         'mlb_chronological_admission.py', 'mlb_market_free_checkpoint.py', 'mlb_real_shadow_capture.py')},
                     'eligible': False, 'feature_rows_created': 0, 'fitting_enabled': False,
                     'scoring_enabled': False, 'historical_performance': None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--baseline-replay', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(encoded(census(args.bundle, args.baseline_replay)).decode(), end='')
    except ERRORS as exc:
        parser.exit(2, str(exc) + '\n')
