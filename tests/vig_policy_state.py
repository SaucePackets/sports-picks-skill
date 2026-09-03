"""One deployed-policy fixture, shared by every suite that needs the edge floor.

`mlb_game_reads` now refuses a `price_discipline` claim it cannot check against
`min_conservative_edge`, and the floor is loaded from `risk_limits.json` — so a
test that leaves the policy to the machine passes or fails depending on whether
the developer happens to have a live Vig state dir. That is the same
machine-dependence the validator's required `policy` argument exists to stop,
and it would be silly to reintroduce it one directory over.

The block mirrors the live `~/.hermes/vig/state/risk_limits.json` under the key
the loader actually reads there (`mlb_policy`, with `policy_effective_at`), so
the fixture exercises the deployed spelling rather than a convenient one.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

LIVE_MIN_CONSERVATIVE_EDGE = 0.05


def policy_block(**overrides):
    block = {
        "schema": "vig-mlb-selection-policy-v1",
        "policy_version": "2026-08-24-probation-lifted",
        "policy_effective_at": "2026-08-24T01:20:00Z",
        "min_conservative_edge": LIVE_MIN_CONSERVATIVE_EDGE,
        "max_mlb_official_bets_per_day": 2,
        "starter_pending_promotions_enabled": True,
        "max_small_bets_per_day_during_probation": 3,
    }
    for key, value in overrides.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return block


def write_policy(state_dir: Path, **overrides) -> Path:
    """Write a risk_limits.json carrying the selection policy. Returns the dir."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "risk_limits.json"
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    existing["mlb_policy"] = policy_block(**overrides)
    path.write_text(json.dumps(existing), encoding="utf-8")
    return state_dir


@contextmanager
def deployed_policy(state_dir: Path, **overrides):
    """Point VIG_STATE_DIR at a state dir carrying the selection policy."""
    write_policy(state_dir, **overrides)
    with mock.patch.dict("os.environ", {"VIG_STATE_DIR": str(state_dir)}):
        yield state_dir


def loaded_policy(state_dir: Path, **overrides):
    """The `MlbSelectionPolicy` a caller would load from that state dir."""
    import sys

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import mlb_runtime_policy

    write_policy(state_dir, **overrides)
    return mlb_runtime_policy.load_mlb_selection_policy(Path(state_dir))
