#!/usr/bin/env python3
"""Predeclared offline team-form baseline. Never writes model registry or orders."""

import argparse
import math
from pathlib import Path

from mlb_chronological_admission import admission, CONTRACT_PATH, ERRORS
from mlb_chronological_checkpoint import encoded
from mlb_market_free_checkpoint import require
from mlb_real_shadow_capture import decode, instant, sha

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs/mlb-team-form-experiment.json"

FROZEN_SPEC_SHA256 = "6d559b3d2cdc0a03048c97bf8563f312dacd23fed100bd6e94e7f1ea7177b704"


def sigmoid(z):
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def fit(pairs):
    require(
        bool(pairs) and {y for _, y in pairs} == {0, 1},
        "training_requires_both_classes",
    )
    n = len(pairs)
    mean = sum(x for x, _ in pairs) / n
    scale = math.sqrt(sum((x - mean) ** 2 for x, _ in pairs) / n)
    xs = [(x - mean) / scale if scale else 0 for x, _ in pairs]
    ys = [y for _, y in pairs]
    b, w = 0.0, 0.0

    def objective(intercept, slope):
        zs = [intercept + slope * x for x in xs]
        return (
            sum(
                max(z, 0) - y * z + math.log1p(math.exp(-abs(z)))
                for z, y in zip(zs, ys)
            )
            / n
            + 0.5 * slope * slope
        )

    for iteration in range(100):
        ps = [sigmoid(b + w * x) for x in xs]
        g0 = sum(p - y for p, y in zip(ps, ys)) / n
        g1 = sum((p - y) * x for p, y, x in zip(ps, ys, xs)) / n + w
        if max(abs(g0), abs(g1)) < 1e-10:
            return dict(
                intercept=b,
                slope=w,
                mean=mean,
                scale=scale,
                iterations=iteration,
                training_rows=n,
                gradient_max=max(abs(g0), abs(g1)),
            )
        curvature = [p * (1 - p) for p in ps]
        a = sum(curvature) / n
        c = sum(v * x for v, x in zip(curvature, xs)) / n
        d = sum(v * x * x for v, x in zip(curvature, xs)) / n + 1
        determinant = a * d - c * c
        require(determinant > 0, "singular_logistic_hessian")
        db, dw = (d * g0 - c * g1) / determinant, (a * g1 - c * g0) / determinant
        step, old = 1.0, objective(b, w)
        while step > 2**-30 and objective(b - step * db, w - step * dw) > old + 1e-15:
            step /= 2
        require(step > 2**-30, "logistic_line_search_failed")
        b, w = b - step * db, w - step * dw
    raise ValueError("logistic_not_converged")


def predict(model, x):
    z = (x - model["mean"]) / model["scale"] if model["scale"] else 0
    return sigmoid(model["intercept"] + model["slope"] * z)


def metrics(pairs):
    if not pairs:
        return None
    n = len(pairs)
    bins = []
    for i in range(10):
        selected = [(p, y) for p, y in pairs if min(int(p * 10), 9) == i]
        bins.append(
            dict(
                lower=i / 10,
                upper=(i + 1) / 10,
                n=len(selected),
                mean_probability=(
                    sum(p for p, _ in selected) / len(selected) if selected else None
                ),
                outcome_fraction=(
                    sum(y for _, y in selected) / len(selected) if selected else None
                ),
            )
        )
    clipped = [(min(1 - 1e-15, max(1e-15, p)), y) for p, y in pairs]
    return dict(
        n=n,
        brier=sum((p - y) ** 2 for p, y in pairs) / n,
        log_loss=-sum(y * math.log(p) + (1 - y) * math.log1p(-p) for p, y in clipped)
        / n,
        ece=sum(
            b["n"] * abs(b["mean_probability"] - b["outcome_fraction"])
            for b in bins
            if b["n"]
        )
        / n,
        calibration=bins,
    )


def evaluate(source):
    raw_spec = SPEC_PATH.read_bytes()
    require(sha(raw_spec) == FROZEN_SPEC_SHA256, "experiment_spec_changed")
    spec = decode(raw_spec)
    require(source["census_complete"], "schedule_census_unknown")
    rows = source["occurrences"]
    labels = [r for r in rows if r.get("training_label_admitted")]
    cutoff = instant(spec["training_completion_cutoff"])
    for r in labels:
        require(
            r["split"] == "train"
            and spec["splits"]["train"][0]
            <= r["source_date"]
            <= spec["splits"]["train"][1]
            and instant(r["outcome"]["completed_at"]) < cutoff,
            "training_cutoff_violation",
        )
    train = [
        r
        for r in labels
        if r.get("team_history", {}).get("home_minus_away") is not None
    ]
    model = fit(
        [
            (r["team_history"]["home_minus_away"], r["outcome"]["home_won"])
            for r in train
        ]
    )
    empirical = (sum(r["outcome"]["home_won"] for r in labels) + 1) / (len(labels) + 2)
    ratings = {}
    for r in sorted(
        labels, key=lambda r: (instant(r["outcome"]["completed_at"]), int(r["game_id"]))
    ):
        o = r["outcome"]
        a, h = o["away_id"], o["home_id"]
        ar, hr = ratings.get(a, 1500.0), ratings.get(h, 1500.0)
        p = 1 / (1 + 10 ** ((ar - hr) / 400))
        change = 20 * (o["home_won"] - p)
        ratings[a], ratings[h] = ar - change, hr + change
    predictions, results = [], {}
    for split in ("validation", "test"):
        selected = [r for r in rows if r["split"] == split]
        paired = []
        for r in selected:
            x = r.get("team_history", {}).get("home_minus_away")
            if x is None or r["outcome_status"] != "admitted_reconstruction":
                continue
            o = r["outcome"]
            a, h = o["away_id"], o["home_id"]
            if a not in ratings or h not in ratings:
                continue
            probabilities = dict(
                team_form=predict(model, x),
                coin=0.5,
                empirical_home=empirical,
                elo=1 / (1 + 10 ** ((ratings[a] - ratings[h]) / 400)),
            )
            paired.append(
                dict(
                    game_id=r["game_id"],
                    source_date=r["source_date"],
                    split=split,
                    feature=x,
                    home_won=o["home_won"],
                    probabilities=probabilities,
                    feed_sha256=r["feed_sha256"],
                    schedule_sha256=r["schedule_sha256"],
                    observation_cutoff=r["observation_cutoff"],
                )
            )
        scores = {
            name: metrics([(r["probabilities"][name], r["home_won"]) for r in paired])
            for name in ("team_form", "coin", "empirical_home", "elo")
        }
        results[split] = dict(
            regular_occurrences=source["summary"][split]["regular_season_occurrences"],
            paired_games=len(paired),
            metrics=scores,
        )
        predictions.extend(paired)
    return dict(
        schema="mlb-team-form-result-v1",
        evidence_kind="historical_reconstruction",
        experiment_spec_sha256=sha(SPEC_PATH.read_bytes()),
        model=model,
        training_labels=len(labels),
        empirical_home=empirical,
        frozen_elo_ratings=ratings,
        results=results,
        predictions=predictions,
        eligible=False,
        original_pregame_predictions=False,
        asof_snapshot_verified=False,
        live_model_admitted=False,
        sportsbook_comparison_available=False,
    )


def experiment(bundle):
    source = admission(bundle)
    report = evaluate(source)
    report.update(
        bundle_sha256=source["manifest_sha256"],
        source_contract_sha256=sha(CONTRACT_PATH.read_bytes()),
        source_replay_sha256=sha(encoded(source)),
        implementation_sha256=sha(Path(__file__).read_bytes()),
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(encoded(experiment(args.bundle)).decode(), end="")
    except (OSError, *ERRORS) as exc:
        parser.exit(2, str(exc) + "\n")
