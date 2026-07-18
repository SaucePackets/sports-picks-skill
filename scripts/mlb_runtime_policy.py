#!/usr/bin/env python3
"""Deterministic detection of local MLB standing authorization."""

from __future__ import annotations

import os
from pathlib import Path


def resolve_state_dir(home: Path | None = None) -> Path:
    override = os.environ.get("VIG_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return ((home or Path.home()) / ".hermes" / "vig" / "state").resolve()


def standing_authorization_enabled(state_dir: Path | None = None) -> bool:
    root = state_dir or resolve_state_dir()
    try:
        policy = (root / "policy.md").read_text(encoding="utf-8").casefold()
        risk = (root / "risk_limits.md").read_text(encoding="utf-8").casefold()
    except OSError:
        return False
    return (
        "standing authorization" in policy
        and "current market path: polymarket sports moneyline" in policy
        and "auto-entry: only standing-authorized mlb polymarket moneyline candidates" in risk
    )
