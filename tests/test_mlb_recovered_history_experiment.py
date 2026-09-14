"""Coverage composition must preserve temporal and unresolved-history refusals."""

from copy import deepcopy

import pytest
import mlb_recovered_history_experiment as m


def example():
    games = []
    recoveries = []
    for i in range(3):
        row = dict(
            game_id=str(i + 1),
            source_date=f"2025-04-0{i+1}",
            split="train",
            feed_sha256="f",
            schedule_sha256="s",
            refusal=None,
            appearances=[],
        )
        for side, pid in [("home", "10"), ("away", "20")]:
            row["appearances"].append(
                dict(
                    pitcher_id=pid,
                    game_id=row["game_id"],
                    source_date=row["source_date"],
                    completed_at=f"2025-04-0{i+1}T23:00:00Z",
                    plate_appearances=10,
                    strikeouts=2 if side == "home" else 1,
                    walks=1,
                )
            )
        if i == 2:
            recovery = deepcopy(row)
            recovery.update(
                baseline_refusal="unsupported_plate_appearance_event",
                remaining_refusal=None,
                classification="recovered_appearance_accounting",
            )
            recoveries.append(recovery)
            row.update(refusal="unsupported_plate_appearance_event", appearances=[])
        games.append(row)
    target = dict(
        game_id="4",
        source_date="2025-04-05",
        split="train",
        starter={"pitcher_ids": {"home": "10", "away": "20"}},
        histories={},
        observation_cutoff="2025-04-05T18:00:00Z",
        refusal="pitcher_history_refused",
        features_admitted=False,
        strikeout_fraction_difference=None,
        walk_fraction_difference=None,
    )
    base = dict(
        manifest_sha256="bundle",
        contract_sha256="contract",
        snapshot_receipts_sha256="snapshots",
        census_complete=True,
        appearance_games=games,
        occurrences=[target],
        history_scope="unchanged",
        summary={"train": {"feature_pairs_admitted": 0}},
    )
    recovery = dict(
        bundle_sha256="bundle",
        contract_sha256="contract",
        refused_occurrences=recoveries,
    )
    return base, recovery


def test_recovered_accounting_creates_history_without_mutating_baseline():
    base, recovered = example()
    original = deepcopy(base)
    out = m.combine(base, recovered)
    assert base == original
    assert out["summary"]["train"]["feature_pairs_after"] == 1
    assert out["occurrences"][0]["strikeout_fraction_difference"] == pytest.approx(0.1)
    assert len(out["occurrences"][0]["histories"]["home"]["appearances"]) == 3
    assert out["historical_performance"] is None
    assert all(
        out[k] is False
        for k in (
            "eligible",
            "fitting_enabled",
            "scoring_enabled",
            "asof_snapshot_verified",
            "original_pregame_predictions",
        )
    )


def test_unrelated_earlier_unknown_game_still_blocks_both_pitchers():
    base, recovery = example()
    gap = dict(
        game_id="99",
        source_date="2025-04-01",
        split="train",
        feed_sha256="x",
        schedule_sha256="s",
        refusal="unknown",
        appearances=[],
    )
    base["appearance_games"].append(gap)
    recovery["refused_occurrences"].append(
        gap
        | dict(
            baseline_refusal="unknown",
            remaining_refusal="unknown",
            classification="still_refused",
        )
    )
    out = m.combine(base, recovery)
    assert out["summary"]["train"]["feature_pairs_after"] == 0
    assert out["summary"]["train"]["history_refusals"] == {
        "prior_appearance_census_unresolved": 2
    }
    assert out["occurrences"][0]["strikeout_fraction_difference"] is None


def test_latest_appearance_cannot_be_replaced_when_completion_is_late():
    base, recovery = example()
    for record in recovery["refused_occurrences"][0]["appearances"]:
        record["completed_at"] = "2025-04-05T18:00:00Z"
    out = m.combine(base, recovery)
    assert out["summary"]["train"]["history_refusals"] == {
        "prior_completion_at_or_after_cutoff": 2
    }


@pytest.mark.parametrize(
    "field", ["feed_sha256", "schedule_sha256", "baseline_refusal"]
)
def test_recovery_must_match_original_source_and_refusal(field):
    base, recovery = example()
    recovery["refused_occurrences"][0][field] = "changed"
    with pytest.raises(ValueError, match="recovery_source_mismatch"):
        m.combine(base, recovery)


def test_missing_or_duplicate_recovery_is_not_silent():
    base, recovery = example()
    with pytest.raises(ValueError, match="recovery_denominator_mismatch"):
        m.combine(base, recovery | {"refused_occurrences": []})
    with pytest.raises(ValueError, match="duplicate_recovery_occurrence"):
        m.combine(
            base,
            recovery | {"refused_occurrences": recovery["refused_occurrences"] * 2},
        )


def test_target_refused_before_history_remains_refused():
    base, recovery = example()
    base["occurrences"][0].update(
        histories=None, starter=None, refusal="snapshot_not_pregame"
    )
    assert (
        m.combine(base, recovery)["occurrences"][0]["refusal"] == "snapshot_not_pregame"
    )


def test_same_day_appearance_does_not_fill_prior_history():
    base, recovery = example()
    for r in recovery["refused_occurrences"][0]["appearances"]:
        r["source_date"] = "2025-04-05"
    out = m.combine(base, recovery)
    assert out["summary"]["train"]["history_refusals"] == {
        "fewer_than_three_prior_appearances": 2
    }


def test_changed_checkpoint_is_rejected_before_history_replay(monkeypatch):
    monkeypatch.setattr(m, "census", lambda *args: {})
    monkeypatch.setattr(
        m, "admission", lambda *args: pytest.fail("unverified census used")
    )
    with pytest.raises(ValueError, match="appearance_checkpoint_mismatch"):
        m.experiment("bundle", "snapshots", "baseline")
