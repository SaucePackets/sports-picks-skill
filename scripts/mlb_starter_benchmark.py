#!/usr/bin/env python3
"""Frozen exploratory starter benchmark; no network or live model admission."""

import argparse
import math
from pathlib import Path

import mlb_team_form_experiment as team
from mlb_chronological_checkpoint import encoded
from mlb_real_shadow_capture import decode, instant, sha
from mlb_market_free_checkpoint import require

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs/mlb-starter-benchmark-spec.json"
SPEC_SHA256 = "6a76f577012292c4f68d164d9af73e1821218312a86bfe4692cdf349f80e646d"
KEYS = ("game_id", "source_date", "split")


def solve(matrix, rhs):
    a = [list(row) + [v] for row, v in zip(matrix, rhs)]
    for i in range(len(a)):
        pivot = max(range(i, len(a)), key=lambda j: abs(a[j][i]))
        a[i], a[pivot] = a[pivot], a[i]
        require(abs(a[i][i]) > 1e-15, "singular_logistic_hessian")
        factor = a[i][i]
        a[i] = [x / factor for x in a[i]]
        for j in range(len(a)):
            if j != i:
                factor = a[j][i]
                a[j] = [x - factor * y for x, y in zip(a[j], a[i])]
    return [row[-1] for row in a]


def fit(pairs):
    require(
        bool(pairs) and {y for _, y in pairs} == {0, 1},
        "training_requires_both_classes",
    )
    n, width = len(pairs), len(pairs[0][0])
    require(
        width > 0
        and all(
            len(x) == width
            and all(type(v) in (int, float) and math.isfinite(v) for v in x)
            for x, _ in pairs
        ),
        "invalid_training_features",
    )
    means = [sum(x[j] for x, _ in pairs) / n for j in range(width)]
    scales = [
        math.sqrt(sum((x[j] - means[j]) ** 2 for x, _ in pairs) / n)
        for j in range(width)
    ]
    xs = [
        [1.0] + [(v - mu) / sd if sd else 0.0 for v, mu, sd in zip(x, means, scales)]
        for x, _ in pairs
    ]
    ys = [y for _, y in pairs]
    weights = [0.0] * (width + 1)

    def objective(ws):
        zs = [sum(w * v for w, v in zip(ws, x)) for x in xs]
        return (
            sum(
                max(z, 0) - y * z + math.log1p(math.exp(-abs(z)))
                for z, y in zip(zs, ys)
            )
            / n
            + sum(w * w for w in ws[1:]) / 2
        )

    for iteration in range(100):
        ps = [team.sigmoid(sum(w * v for w, v in zip(weights, x))) for x in xs]
        gradient = [
            sum((p - y) * x[j] for p, y, x in zip(ps, ys, xs)) / n
            + (weights[j] if j else 0)
            for j in range(width + 1)
        ]
        if max(map(abs, gradient)) < 1e-10:
            return dict(
                weights=weights,
                means=means,
                scales=scales,
                training_rows=n,
                iterations=iteration,
                gradient_max=max(map(abs, gradient)),
            )
        hessian = [
            [
                sum(p * (1 - p) * x[j] * x[k] for p, x in zip(ps, xs)) / n
                + (1 if j == k and j else 0)
                for k in range(width + 1)
            ]
            for j in range(width + 1)
        ]
        delta = solve(hessian, gradient)
        step, old = 1.0, objective(weights)
        while (
            step > 2**-30
            and objective([w - step * d for w, d in zip(weights, delta)]) > old + 1e-15
        ):
            step /= 2
        require(step > 2**-30, "logistic_line_search_failed")
        weights = [w - step * d for w, d in zip(weights, delta)]
    raise ValueError("logistic_not_converged")


def predict(model, features):
    require(len(features) == len(model["means"]), "feature_width")
    x = [1.0] + [
        (v - mu) / sd if sd else 0.0
        for v, mu, sd in zip(features, model["means"], model["scales"])
    ]
    return team.sigmoid(sum(w * v for w, v in zip(model["weights"], x)))


def index(rows):
    result = {}
    for row in rows:
        key = tuple(row[k] for k in KEYS)
        require(key not in result, "duplicate_occurrence")
        result[key] = row
    return result


def joined_rows(participants, source, spec):
    require(
        participants["census_complete"] and source["census_complete"],
        "schedule_census_unknown",
    )
    lookup = index(source["occurrences"])
    joined = []
    for key, row in index(participants["occurrences"]).items():
        if not row.get("features_admitted"):
            continue
        require(key in lookup, "missing_team_occurrence")
        other = lookup[key]
        require(
            row["observation_cutoff"] == other["observation_cutoff"],
            "join_cutoff_conflict",
        )
        split = row["split"]
        require(
            split in spec["splits"]
            and spec["splits"][split][0]
            <= row["source_date"]
            <= spec["splits"][split][1],
            "split_date_conflict",
        )
        x = [
            other.get("team_history", {}).get("home_minus_away"),
            row["strikeout_fraction_difference"],
            row["walk_fraction_difference"],
        ]
        if (
            any(v is None for v in x)
            or other["outcome_status"] != "admitted_reconstruction"
        ):
            continue
        require(
            all(
                type(v) in (int, float) and math.isfinite(v) and -1 <= v <= 1 for v in x
            ),
            "invalid_features",
        )
        outcome = other["outcome"]
        require(
            type(outcome["home_won"]) is int and outcome["home_won"] in (0, 1),
            "invalid_label",
        )
        if split == "train":
            if not other.get("training_label_admitted"):
                continue
            require(
                instant(outcome["completed_at"])
                < instant(spec["training_completion_cutoff"]),
                "training_cutoff_violation",
            )
        joined.append(
            dict(
                game_id=row["game_id"],
                source_date=row["source_date"],
                split=split,
                features=x,
                home_won=outcome["home_won"],
                observation_cutoff=row["observation_cutoff"],
            )
        )
    return joined


def evaluate(participants, source, spec):
    rows = joined_rows(participants, source, spec)
    train = [r for r in rows if r["split"] == "train"]
    model = fit([(r["features"], r["home_won"]) for r in train])
    matched = team.fit([(r["features"][0], r["home_won"]) for r in train])
    previous = team.evaluate(source)
    baselines = index(previous["predictions"])
    predictions, results = [], {}
    for split in ("validation", "test"):
        paired = []
        missing_baselines = 0
        for row in rows:
            if row["split"] != split:
                continue
            prior = baselines.get(tuple(row[k] for k in KEYS))
            if prior is None:
                missing_baselines += 1
                continue
            require(prior["home_won"] == row["home_won"], "baseline_label_conflict")
            probabilities = {
                ("team_full" if k == "team_form" else k): v
                for k, v in prior["probabilities"].items()
            }
            probabilities.update(
                starter=predict(model, row["features"]),
                team_matched=team.predict(matched, row["features"][0]),
            )
            paired.append(dict(row, probabilities=probabilities))
        names = (
            "starter",
            "team_matched",
            "team_full",
            "coin",
            "empirical_home",
            "elo",
        )
        scores = {
            name: team.metrics(
                [(r["probabilities"][name], r["home_won"]) for r in paired]
            )
            for name in names
        }
        results[split] = dict(
            regular_occurrences=source["summary"][split]["regular_season_occurrences"],
            paired_games=len(paired),
            missing_baselines=missing_baselines,
            metrics=scores,
            starter_minus_baseline=(
                {
                    name: {
                        measure: scores["starter"][measure] - scores[name][measure]
                        for measure in ("brier", "log_loss")
                    }
                    for name in names[1:]
                }
                if paired
                else {}
            ),
        )
        predictions.extend(paired)
    return dict(
        schema="mlb-starter-benchmark-result-v1",
        model=model,
        matched_team_model=matched,
        full_team_model=previous["model"],
        training_rows=len(train),
        training_labels_for_baselines=previous["training_labels"],
        results=results,
        predictions=predictions,
        eligible=False,
        live_model_admitted=False,
        original_pregame_predictions=False,
        asof_snapshot_verified=False,
        sportsbook_comparison_available=False,
        confirmatory_holdout=False,
    )


def experiment(participant_path, team_path):
    spec_raw = SPEC_PATH.read_bytes()
    require(sha(spec_raw) == SPEC_SHA256, "experiment_spec_changed")
    spec = decode(spec_raw)
    p, t = participant_path.read_bytes(), team_path.read_bytes()
    require(
        sha(p) == spec["inputs"]["participant_replay_sha256"],
        "participant_replay_changed",
    )
    require(
        sha(t) == spec["inputs"]["team_admission_replay_sha256"], "team_replay_changed"
    )
    report = evaluate(decode(p), decode(t), spec)
    report.update(
        spec_sha256=sha(spec_raw),
        input_sha256=spec["inputs"],
        implementation_sha256=sha(Path(__file__).read_bytes()),
        team_implementation_sha256=sha(Path(team.__file__).read_bytes()),
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--participants", required=True, type=Path)
    parser.add_argument("--team-admission", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(
            encoded(experiment(args.participants, args.team_admission)).decode(), end=""
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, str(exc) + "\n")
