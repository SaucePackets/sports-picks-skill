"""Numerical agreement, retained-byte binding, and train/evaluation isolation."""

import json
import math
from copy import deepcopy

import pytest
import mlb_starter_benchmark as m
from test_mlb_team_form_experiment import source


def fixture():
    s = source()
    rows = []
    for i, r in enumerate(s["occurrences"]):
        r.setdefault("observation_cutoff", "2025-04-20T18:00:00Z")
        r.setdefault("outcome_status", "admitted_reconstruction")
        rows.append(
            {k: r[k] for k in (*m.KEYS, "observation_cutoff")}
            | dict(
                features_admitted=True,
                strikeout_fraction_difference=(i % 3) / 10,
                walk_fraction_difference=(i % 2) / 10,
            )
        )
    return (
        dict(census_complete=True, occurrences=rows),
        s,
        json.loads(m.SPEC_PATH.read_text()),
    )


def test_one_feature_matches_existing_independent_solver():
    pairs = [(-0.4, 0), (-0.2, 1), (0.1, 0), (0.5, 1)]
    old = m.team.fit(pairs)
    new = m.fit([([x], y) for x, y in pairs])
    for x in (-2, -0.1, 0, 0.7, 3):
        assert m.predict(new, [x]) == pytest.approx(m.team.predict(old, x), abs=1e-12)


def test_constant_dimensions_recover_closed_form():
    model = m.fit([([1, 2, 3], y) for y in (0, 1, 1, 1)])
    assert model["weights"] == pytest.approx([math.log(3), 0, 0, 0])
    assert m.predict(model, [9, 8, 7]) == pytest.approx(0.75)


def test_multivariate_objective_has_stationary_minimum():
    pairs = [
        ([-0.4, 0.3, -0.1], 0),
        ([0.2, -0.1, 0.1], 1),
        ([0.1, 0.4, 0.1], 0),
        ([0.5, 0.2, -0.2], 1),
    ]
    model = m.fit(pairs)

    def objective(ws):
        total = 0
        for x, y in pairs:
            z = ws[0] + sum(
                w * (v - mu) / sd
                for w, v, mu, sd in zip(ws[1:], x, model["means"], model["scales"])
            )
            total += math.log1p(math.exp(z)) - y * z
        return total / len(pairs) + sum(w * w for w in ws[1:]) / 2

    w = model["weights"]
    for i in range(4):
        plus, minus = w.copy(), w.copy()
        plus[i] += 1e-5
        minus[i] -= 1e-5
        assert objective(plus) >= objective(w) - 1e-14
        assert objective(minus) >= objective(w) - 1e-14
        assert abs((objective(plus) - objective(minus)) / 2e-5) < 1e-8


def test_evaluation_labels_do_not_change_any_probabilities():
    p, s, spec = fixture()
    before = m.evaluate(p, s, spec)
    for r in s["occurrences"][-2:]:
        r["outcome"]["home_won"] = 1 - r["outcome"]["home_won"]
    after = m.evaluate(p, s, spec)
    for key in ("model", "matched_team_model", "full_team_model"):
        assert before[key] == after[key]
    assert [r["probabilities"] for r in before["predictions"]] == [
        r["probabilities"] for r in after["predictions"]
    ]


def test_evaluation_features_do_not_change_training_scaling():
    p, s, spec = fixture()
    before = m.evaluate(p, s, spec)
    p["occurrences"][-1]["strikeout_fraction_difference"] = 0.99
    after = m.evaluate(p, s, spec)
    assert before["model"] == after["model"]
    assert (
        before["predictions"][-1]["probabilities"]["starter"]
        != after["predictions"][-1]["probabilities"]["starter"]
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("duplicate", "duplicate_occurrence"),
        ("cutoff", "join_cutoff_conflict"),
        ("split", "split_date_conflict"),
        ("late_label", "training_cutoff_violation"),
        ("nan", "invalid_features"),
        ("census", "schedule_census_unknown"),
    ],
)
def test_bad_joins_refuse(mutation, reason):
    p, s, spec = fixture()
    if mutation == "duplicate":
        p["occurrences"].append(deepcopy(p["occurrences"][0]))
    if mutation == "cutoff":
        p["occurrences"][0]["observation_cutoff"] = "2025-04-20T19:00:00Z"
    if mutation == "split":
        p["occurrences"][0]["source_date"] = s["occurrences"][0]["source_date"] = (
            "2025-05-02"
        )
    if mutation == "late_label":
        s["occurrences"][0]["outcome"]["completed_at"] = spec[
            "training_completion_cutoff"
        ]
    if mutation == "nan":
        p["occurrences"][0]["walk_fraction_difference"] = float("nan")
    if mutation == "census":
        p["census_complete"] = False
    with pytest.raises(ValueError, match=reason):
        m.evaluate(p, s, spec)


def test_missing_features_preserve_full_denominator():
    p, s, spec = fixture()
    p["occurrences"][-1]["features_admitted"] = False
    result = m.evaluate(p, s, spec)
    assert result["results"]["test"]["paired_games"] == 0
    assert result["results"]["test"]["regular_occurrences"] == 2
    assert all(v is None for v in result["results"]["test"]["metrics"].values())
    assert (
        result["eligible"]
        is result["live_model_admitted"]
        is result["confirmatory_holdout"]
        is False
    )


def test_source_bytes_checked_before_decoding_or_fitting(tmp_path, monkeypatch):
    p, s, spec = fixture()
    a, b = tmp_path / "p.json", tmp_path / "s.json"
    a.write_bytes(m.encoded(p))
    b.write_bytes(m.encoded(s))
    spec["inputs"] = {
        "participant_replay_sha256": m.sha(a.read_bytes()),
        "team_admission_replay_sha256": m.sha(b.read_bytes()),
    }
    path = tmp_path / "spec.json"
    path.write_bytes(m.encoded(spec))
    monkeypatch.setattr(m, "SPEC_PATH", path)
    monkeypatch.setattr(m, "SPEC_SHA256", m.sha(path.read_bytes()))
    m.experiment(a, b)
    for target, reason in [
        (a, "participant_replay_changed"),
        (b, "team_replay_changed"),
    ]:
        original = target.read_bytes()
        target.write_bytes(original + b" ")
        with pytest.raises(ValueError, match=reason):
            m.experiment(a, b)
        target.write_bytes(original)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="experiment_spec_changed"):
        m.experiment(a, b)
