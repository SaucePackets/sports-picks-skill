#!/usr/bin/env python3
"""Offline history coverage experiment using the reviewed appearance recovery.

Replays retained sources; preserves the frozen global unresolved-game rule.
Does not train, score, admit a model, or write runtime state.
"""

import argparse
from collections import Counter
from copy import deepcopy
from pathlib import Path

from mlb_appearance_census import census
from mlb_bulk_starter_admission import SIDES, admission, history
from mlb_chronological_admission import ERRORS
from mlb_chronological_checkpoint import encoded
from mlb_market_free_checkpoint import require
from mlb_real_shadow_capture import decode, instant, sha

CHECKPOINT = Path(__file__).resolve().parents[1] / "docs/mlb-appearance-census.json"


def identity(row):
    return row["game_id"], row["source_date"], row["split"]


def combine(baseline, recovery):
    """Compose verified replays; retain refusals and full contributing pointers."""
    require(baseline["manifest_sha256"] == recovery["bundle_sha256"], "bundle_mismatch")
    require(
        baseline["contract_sha256"] == recovery["contract_sha256"], "contract_mismatch"
    )
    old = {identity(r): r for r in baseline["appearance_games"]}
    require(
        len(old) == len(baseline["appearance_games"]), "duplicate_appearance_occurrence"
    )
    recovered = {identity(r): r for r in recovery["refused_occurrences"]}
    require(
        len(recovered) == len(recovery["refused_occurrences"]),
        "duplicate_recovery_occurrence",
    )
    require(
        set(recovered) == {k for k, r in old.items() if r["refusal"] is not None},
        "recovery_denominator_mismatch",
    )
    records, gaps, recovered_count = [], [], 0
    for key, original in old.items():
        row = deepcopy(original)
        if key in recovered:
            new = recovered[key]
            require(
                new["baseline_refusal"] == original["refusal"]
                and all(
                    new[k] == original[k] for k in ("feed_sha256", "schedule_sha256")
                ),
                "recovery_source_mismatch",
            )
            if new["remaining_refusal"] is None:
                require(
                    new["classification"] == "recovered_appearance_accounting"
                    and bool(new["appearances"]),
                    "recovery_accounting_missing",
                )
                row.update(appearances=new["appearances"], refusal=None)
                recovered_count += 1
            else:
                row["refusal"] = new["remaining_refusal"]
        if row["refusal"] is None:
            records.extend(row["appearances"])
        else:
            gaps.append(
                {
                    k: row[k]
                    for k in ("game_id", "source_date", "refusal", "feed_sha256")
                }
            )
    rows = deepcopy(baseline["occurrences"])
    for row in rows:
        # A valid starter and existing histories mean all target identity gates passed.
        if row["histories"] is None:
            continue
        cutoff = instant(row["observation_cutoff"])
        row["histories"] = {
            side: history(
                row,
                row["starter"]["pitcher_ids"][side],
                records,
                gaps,
                cutoff,
                baseline["census_complete"],
            )
            for side in SIDES
        }
        admitted = all(row["histories"][s]["refusal"] is None for s in SIDES)
        row.update(
            features_admitted=admitted,
            refusal=None if admitted else "pitcher_history_refused",
        )
        for feature in ("strikeout_fraction", "walk_fraction"):
            row[feature + "_difference"] = (
                row["histories"]["home"][feature] - row["histories"]["away"][feature]
                if admitted
                else None
            )
    summary = {}
    for split in baseline["summary"]:
        selected = [r for r in rows if r["split"] == split]
        summary[split] = {
            "starter_candidates": sum(r["starter"] is not None for r in selected),
            "feature_pairs_before": baseline["summary"][split][
                "feature_pairs_admitted"
            ],
            "feature_pairs_after": sum(r["features_admitted"] for r in selected),
            "history_refusals": dict(
                sorted(
                    Counter(
                        h["refusal"]
                        for r in selected
                        if r["histories"] is not None
                        for h in r["histories"].values()
                        if h["refusal"] is not None
                    ).items()
                )
            ),
        }
    return {
        "schema": "mlb-recovered-history-experiment-v1",
        "evidence_kind": "historical_reconstruction",
        "bundle_sha256": baseline["manifest_sha256"],
        "contract_sha256": baseline["contract_sha256"],
        "snapshot_receipts_sha256": baseline["snapshot_receipts_sha256"],
        "census_complete": baseline["census_complete"],
        "appearance_occurrences_before": len(old),
        "recovered_occurrences": recovered_count,
        "remaining_gaps": sorted(gaps, key=lambda r: (r["source_date"], r["game_id"])),
        "summary": summary,
        "occurrences": rows,
        "history_scope": baseline["history_scope"],
        "asof_snapshot_verified": False,
        "original_pregame_predictions": False,
        "fitting_enabled": False,
        "scoring_enabled": False,
        "eligible": False,
        "historical_performance": None,
    }


def experiment(bundle, snapshot_dir, baseline_replay):
    recovery = census(bundle, baseline_replay)
    checkpoint = decode(CHECKPOINT.read_bytes())
    raw = encoded(recovery)
    require(
        len(raw) == checkpoint["replay_bytes"]
        and sha(raw) == checkpoint["replay_sha256"],
        "appearance_checkpoint_mismatch",
    )
    baseline = admission(bundle, snapshot_dir)
    report = combine(baseline, recovery)
    report.update(
        baseline_replay_sha256=sha(encoded(baseline)),
        appearance_replay_sha256=sha(raw),
        adapter_sha256=sha(Path(__file__).read_bytes()),
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--snapshot-dir", required=True, type=Path)
    parser.add_argument("--baseline-replay", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(
            encoded(
                experiment(args.bundle, args.snapshot_dir, args.baseline_replay)
            ).decode(),
            end="",
        )
    except (OSError, *ERRORS) as exc:
        parser.exit(2, str(exc) + "\n")
