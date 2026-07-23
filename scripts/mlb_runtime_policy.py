#!/usr/bin/env python3
"""Deterministic detection of local MLB standing authorization."""

from __future__ import annotations

import json
import os
from pathlib import Path


def resolve_state_dir(home: Path | None = None) -> Path:
    override = os.environ.get("VIG_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return ((home or Path.home()) / ".hermes" / "vig" / "state").resolve()


def standing_authorization_enabled(state_dir: Path | None = None) -> bool:
    """Authorization is an explicit flag file, never prose substring matching.

    Prose matching was both over-broad ("standing authorization is suspended"
    still matched) and fragile (innocent rewording silently disabled
    automation). Fails closed on any read/parse problem.
    """
    root = state_dir or resolve_state_dir()
    try:
        flag = json.loads((root / "standing_authorization.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(flag, dict)
        and flag.get("schema") == "vig-standing-authorization-v1"
        and flag.get("enabled") is True
    )
