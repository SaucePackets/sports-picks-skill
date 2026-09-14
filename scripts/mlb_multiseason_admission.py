#!/usr/bin/env python3
"""Verify sealed batches and report multiseason training coverage. Offline only."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import mlb_history_batches as batches
import mlb_multiseason_evidence as evidence
from mlb_appearance_census import runner_out
from mlb_bulk_starter_admission import history, SIDES
from mlb_chronological_admission import team_history, ERRORS
from mlb_participant_history_experiment import final_identity
from mlb_market_free_checkpoint import require
from mlb_real_shadow_capture import decode, instant, sha
from mlb_chronological_checkpoint import encoded

CONTRACT = (
    Path(__file__).resolve().parents[1] / "docs/mlb-multiseason-admission-contract.json"
)
CONTRACT_SHA = "ae3308f34f9ba2dc7d8c8534721953a182feaeb5e64000e35b797ab99d2f49da"


def inspect_game(target, body):
    interval_refusals = []
    intervals = []
    records = []
    outcome = None
    cert = None
    refusal = "feed_not_collected"
    cert_refusal = None
    if body is not None:
        for g in target["occurrences"]:
            if g["raw_game"]["status"].get("detailedState") == "Postponed":
                try:
                    intervals.append(evidence.postponed_interval(g, body))
                except ERRORS as exc:
                    interval_refusals.append(
                        dict(source_date=g["source_date"], refusal=str(exc))
                    )
        try:
            require(len(target["occurrences"]) == 1, "repeated_schedule_occurrence")
            g = target["occurrences"][0]
            outcome = evidence.outcome(g, body)
            records = evidence.appearances(g, body, non_pa_classifier=runner_out)
            refusal = None
        except ERRORS as exc:
            refusal = str(exc)
        try:
            matching = []
            for g in target["occurrences"]:
                try:
                    final_identity(g, body)
                    matching.append(g)
                except ERRORS:
                    pass
            require(len(matching) == 1, "unique_final_occurrence_unavailable")
            cert = evidence.participants(matching[0], body)
        except ERRORS as exc:
            cert_refusal = str(exc)
    return dict(
        postponed_intervals=intervals,
        postponed_refusals=interval_refusals,
        records=records,
        outcome=outcome,
        certificate=cert,
        refusal=refusal,
        certificate_refusal=cert_refusal,
    )


def certificate_cache(certificates):
    return {
        gid: dict(
            certificate=entry["certificate"],
            certificate_sha256=sha(encoded(entry["certificate"])),
            intervals=[
                (c, sha(encoded(c))) for c in entry.get("postponed_intervals", [])
            ],
        )
        for gid, entry in certificates.items()
    }


def narrow_history(game, old, cache, cutoff):
    """Same exclusion rule as the pinned experiment, with one hash per certificate."""
    retained, excluded = [], []
    pid = old["pitcher_id"]
    for gap in old["unresolved_games"]:
        entry = cache.get(gap["game_id"], {})
        interval = next(
            (
                (c, digest)
                for c, digest in entry.get("intervals", [])
                if c.get("kind") == "postponed_no_play_interval"
                and c.get("status") == "structurally_corroborated_only"
                and c["game_id"] == gap["game_id"]
                and c["source_date"] == gap["source_date"]
                and c["feed_sha256"] == gap["feed_sha256"]
                and cutoff < instant(c["before_rescheduled_start"])
            ),
            None,
        )
        reason, digest = None, None
        if interval:
            reason, digest = "postponed_before_reschedule", interval[1]
        else:
            cert = entry.get("certificate")
            if (
                cert is not None
                and cert.get("status") == "structurally_corroborated_only"
                and bool(cert.get("pitcher_ids"))
                and cert["feed_sha256"] == gap["feed_sha256"]
                and cert["game_id"] == gap["game_id"]
                and instant(cert["completion_upper_bound"]) < cutoff
            ):
                prior = old["appearances"]
                older = len(prior) == 3 and instant(
                    cert["completion_upper_bound"]
                ) < min(instant(a["completed_at"]) for a in prior)
                if pid not in cert["pitcher_ids"] or older:
                    reason = (
                        "corroborated_nonparticipant"
                        if pid not in cert["pitcher_ids"]
                        else "completed_before_selected_three"
                    )
                    digest = entry["certificate_sha256"]
        if reason:
            excluded.append(
                dict(
                    game_id=gap["game_id"],
                    source_date=gap["source_date"],
                    certificate_sha256=digest,
                    reason=reason,
                )
            )
        else:
            retained.append(gap)
    new = history(game, pid, old["appearances"], retained, cutoff, True)
    new["excluded_gaps"] = excluded
    return new


def admission(root):
    script_paths = [
        Path(__file__),
        Path(evidence.__file__),
        Path(batches.__file__),
        Path(history.__code__.co_filename),
        Path(team_history.__code__.co_filename),
        Path(runner_out.__code__.co_filename),
        Path(final_identity.__code__.co_filename),
    ]
    script_hashes = {p.name: sha(p.read_bytes()) for p in script_paths}
    raw = CONTRACT.read_bytes()
    require(sha(raw) == CONTRACT_SHA, "admission_contract_changed")
    spec = decode(raw)
    inv = batches.inventory(root / "inventory", False)
    loaded = {}
    bindings = []
    for number in range(inv["batches"]):
        directory = root / f"batch-{number:03}"
        if not (directory / "report.json").exists():
            continue
        report = batches.batch(root / "inventory", directory, number, False)
        bindings.append(dict(number=number, sha256=sha(encoded(report))))
        for row in report["rows"]:
            require(row["game_id"] not in loaded, "duplicate_collected_game")
            body = None
            if row["feed_available"]:
                gid = row["game_id"]
                body = batches.read_source(
                    directory,
                    "feed",
                    gid,
                    f"https://statsapi.mlb.com/api/v1.1/game/{gid}/feed/live",
                    False,
                )
            loaded[row["game_id"]] = (row, body)
    appearances = defaultdict(list)
    outcomes = []
    gaps = []
    certificates = {}
    diagnostics = []
    for target in inv["targets"]:
        gid = target["game_id"]
        row, body = loaded.get(gid, ({}, None))
        info = inspect_game(target, body)
        for r in info["records"]:
            appearances[r["pitcher_id"]].append(r)
        if info["outcome"] is not None:
            outcomes.append(info["outcome"])
        if info["refusal"]:
            for g in target["occurrences"]:
                gaps.append(
                    dict(
                        game_id=gid,
                        source_date=g["source_date"],
                        feed_sha256=sha(body) if body else None,
                        refusal=info["refusal"],
                    )
                )
        certificates[gid] = dict(
            certificate=info["certificate"],
            refusal=info["certificate_refusal"],
            postponed_intervals=info["postponed_intervals"],
            postponed_refusals=info["postponed_refusals"],
        )
        diagnostics.append(
            dict(
                game_id=gid,
                appearance_refusal=info["refusal"],
                certificate_refusal=info["certificate_refusal"],
                appearance_count=len(info["records"]),
                outcome_admitted=info["outcome"] is not None,
            )
        )
    cache = certificate_cache(certificates)
    occurrence_rows = []
    summary = {}
    for target in inv["targets"]:
        gid = target["game_id"]
        collected = loaded.get(gid, ({}, None))[0]
        for g in target["occurrences"]:
            split = (
                "train"
                if spec["training_dates"][0]
                <= g["source_date"]
                <= spec["training_dates"][1]
                else "warmup"
            )
            cutoff = instant(g["scheduled_start"]) - timedelta(minutes=60)
            row = dict(
                game_id=gid,
                source_date=g["source_date"],
                split=split,
                observation_cutoff=cutoff.isoformat(),
                histories=None,
                features_admitted=False,
                refusal="starter_unavailable",
            )
            if collected.get("starter_candidate") and len(target["occurrences"]) == 1:
                row["histories"] = {
                    s: history(
                        g,
                        collected["pitcher_ids"][s],
                        appearances[collected["pitcher_ids"][s]],
                        gaps,
                        cutoff,
                        True,
                    )
                    for s in SIDES
                }
            row["team_history"] = team_history(g, outcomes, cutoff, True)
            label = next(
                (
                    o
                    for o in outcomes
                    if o["game_id"] == gid and o["source_date"] == g["source_date"]
                ),
                None,
            )
            row["label"] = (
                label
                if label is not None
                and instant(label["completed_at"])
                < instant(spec["training_label_cutoff"])
                else None
            )
            occurrence_rows.append(row)
    for split in ("warmup", "train"):
        summary[split] = dict(
            starter_candidates=sum(
                r["histories"] is not None
                for r in occurrence_rows
                if r["split"] == split
            ),
            feature_pairs_after=0,
        )
    for row in occurrence_rows:
        if row["histories"] is not None:
            row["histories"] = {
                s: narrow_history(row, h, cache, instant(row["observation_cutoff"]))
                for s, h in row["histories"].items()
            }
            row["features_admitted"] = all(
                h["refusal"] is None for h in row["histories"].values()
            )
            row["refusal"] = (
                None if row["features_admitted"] else "pitcher_history_refused"
            )
            for feature in ("strikeout_fraction", "walk_fraction"):
                row[feature + "_difference"] = (
                    row["histories"]["home"][feature]
                    - row["histories"]["away"][feature]
                    if row["features_admitted"]
                    else None
                )
        row["complete_training_row"] = (
            row["split"] == "train"
            and row["features_admitted"]
            and row["team_history"]["home_minus_away"] is not None
            and row["label"] is not None
        )
    for split in summary:
        selected = [r for r in occurrence_rows if r["split"] == split]
        summary[split].update(
            feature_pairs_before=0,
            feature_pairs_after=sum(r["features_admitted"] for r in selected),
            history_refusals=dict(
                Counter(
                    h["refusal"]
                    for r in selected
                    if r["histories"]
                    for h in r["histories"].values()
                    if h["refusal"]
                )
            ),
        )
    require(
        all(sha(p.read_bytes()) == script_hashes[p.name] for p in script_paths),
        "source_changed_during_replay",
    )
    return dict(
        source_script_sha256=script_hashes,
        schema="mlb-multiseason-admission-v1",
        contract_sha256=sha(raw),
        implementation_sha256=sha(Path(__file__).read_bytes()),
        evidence_sha256=sha(Path(evidence.__file__).read_bytes()),
        inventory_sha256=sha(encoded(inv)),
        batch_bindings=bindings,
        summary=dict(
            total_batches=inv["batches"],
            sealed_batches=len(bindings),
            collected_games=len(loaded),
            regular_occurrences=inv["regular_occurrences"],
            appearance_games=sum(d["appearance_refusal"] is None for d in diagnostics),
            appearance_refusals=dict(
                Counter(
                    d["appearance_refusal"]
                    for d in diagnostics
                    if d["appearance_refusal"]
                )
            ),
            complete_training_rows=sum(
                r["complete_training_row"] for r in occurrence_rows
            ),
            pitcher_features=summary,
        ),
        occurrences=occurrence_rows,
        diagnostics=diagnostics,
        participant_certificates=certificates,
        eligible=False,
        fitting_enabled=False,
        scoring_enabled=False,
        historical_performance=None,
        original_pregame_predictions=False,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    try:
        print(encoded(admission(args.root)).decode(), end="")
    except (OSError, *ERRORS) as exc:
        p.exit(2, str(exc) + "\n")
