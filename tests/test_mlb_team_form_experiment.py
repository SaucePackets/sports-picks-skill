"""Numerical controls and held-out isolation for the offline baseline."""

import math
from copy import deepcopy

import pytest
import mlb_team_form_experiment as m


def source():
    rows = []
    for i, (x, y) in enumerate([(-0.4, 0), (-0.2, 1), (0.1, 0), (0.5, 1)]):
        rows.append(
            dict(
                game_id=str(i + 1),
                source_date="2025-04-20",
                split="train",
                training_label_admitted=True,
                team_history={"home_minus_away": x},
                outcome=dict(
                    home_won=y,
                    completed_at=f"2025-04-20T2{i}:00:00Z",
                    away_id="1",
                    home_id="2",
                ),
            )
        )
    for i, split in enumerate(("validation", "test")):
        rows.append(
            dict(
                game_id=str(i + 10),
                source_date=f"2025-0{i+5}-10",
                split=split,
                training_label_admitted=False,
                team_history={"home_minus_away": 0.2},
                outcome_status="admitted_reconstruction",
                outcome=dict(home_won=i, away_id="1", home_id="2"),
                feed_sha256="f",
                schedule_sha256="s",
                observation_cutoff=f"2025-0{i+5}-10T18:00:00Z",
            )
        )
    return dict(
        census_complete=True,
        occurrences=rows,
        summary={s: {"regular_season_occurrences": 2} for s in ("validation", "test")},
    )


def test_constant_feature_recovers_intercept_only_closed_form():
    model = m.fit([(2, y) for y in (1, 1, 1, 0)])
    assert model["slope"] == 0
    assert model["intercept"] == pytest.approx(math.log(3))
    assert m.predict(model, -100) == pytest.approx(0.75)


def test_newton_solution_independently_minimizes_objective_neighborhood():
    pairs = [(-0.4, 0), (-0.2, 1), (0.1, 0), (0.5, 1)]
    model = m.fit(pairs)

    def objective(b, w):
        values = []
        for x, y in pairs:
            z = b + w * (x - model["mean"]) / model["scale"]
            # Independent direct log-likelihood calculation for these moderate logits.
            values.append(math.log(1 + math.exp(z)) - y * z)
        return sum(values) / len(values) + w * w / 2

    b, w = model["intercept"], model["slope"]
    best = objective(b, w)
    for db in (-0.01, -0.001, 0, 0.001, 0.01):
        for dw in (-0.01, -0.001, 0, 0.001, 0.01):
            assert objective(b + db, w + dw) >= best - 1e-14
    eps = 1e-5
    assert abs((objective(b + eps, w) - objective(b - eps, w)) / (2 * eps)) < 1e-8
    assert abs((objective(b, w + eps) - objective(b, w - eps)) / (2 * eps)) < 1e-8


def test_held_out_labels_never_change_training_or_probabilities():
    data = source()
    before = m.evaluate(data)
    for r in data["occurrences"][-2:]:
        r["outcome"]["home_won"] = 1 - r["outcome"]["home_won"]
    after = m.evaluate(data)
    for key in ("model", "empirical_home", "frozen_elo_ratings"):
        assert before[key] == after[key]
    assert [r["probabilities"] for r in before["predictions"]] == [
        r["probabilities"] for r in after["predictions"]
    ]


def test_training_label_at_cutoff_is_refused():
    data = source()
    data["occurrences"][0]["outcome"]["completed_at"] = "2025-05-01T00:00:00Z"
    with pytest.raises(ValueError, match="training_cutoff_violation"):
        m.evaluate(data)


def test_all_models_share_comparison_rows_and_keep_full_denominator():
    data = source()
    missing = deepcopy(data["occurrences"][-1])
    missing.update(game_id="99", team_history={"home_minus_away": None})
    data["occurrences"].append(missing)
    out = m.evaluate(data)
    assert out["results"]["test"]["regular_occurrences"] == 2
    assert out["results"]["test"]["paired_games"] == 1
    assert {x["n"] for x in out["results"]["test"]["metrics"].values()} == {1}
    assert not out["live_model_admitted"] and not out["eligible"]


def test_metrics_have_hand_computed_values_and_endpoint_bins():
    score = m.metrics([(0.5, 0), (0.5, 1)])
    assert score["brier"] == 0.25
    assert score["log_loss"] == pytest.approx(math.log(2))
    assert score["ece"] == 0
    assert sum(b["n"] for b in m.metrics([(0.0, 0), (1.0, 1)])["calibration"]) == 2
    assert m.metrics([]) is None


def test_single_class_and_unknown_census_refuse():
    with pytest.raises(ValueError, match="training_requires_both_classes"):
        m.fit([(1, 1), (2, 1)])
    with pytest.raises(ValueError, match="schedule_census_unknown"):
        m.evaluate(source() | {"census_complete": False})
