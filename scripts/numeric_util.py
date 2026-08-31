#!/usr/bin/env python3
"""One shared "is this a number the arithmetic downstream can use" predicate.

Canonical copy: ``scripts/numeric_util.py`` in the sports-picks-skill repo.
A byte-identical copy ships next to deployed skill scripts (for same-directory
imports); keep every copy in sync with the canonical file.

Two modules validate recorded numbers against the same rule — the write side
(``mlb_baseball_evidence`` at the execution gate) and the read side
(``mlb_postgame_evidence`` at settlement). They had two copies of it, and two
copies of one computation agree only until one of them changes: the write side
gained ``math.isfinite`` in PR #43 and the grader did not, which is the drift
PR #68 was fixing. This module is the single implementation both import, and
neither may re-derive it.

Deliberately dependency-free: the write side is on the execution path and the
read-only analysis layer pins its import closure OFF that path, so a shared
helper between them can only live in a module that reaches neither.

No third-party dependencies: standard library only.
"""

from __future__ import annotations

import math
from typing import Any


def is_finite_number(value: Any) -> bool:
    """Whether ``value`` is a real number that float arithmetic can use.

    Total by construction — it returns for every input and raises for none,
    which is the property its callers' "a bad value is rejected, never an
    exception" contracts rest on.

    Three rejections, for three different reasons:

    - ``bool`` is an ``int`` in Python and ``True`` is not a recorded quantity.
    - ``NaN`` and the infinities are numbers no threshold can order.
    - An integer too large to convert to a float. Not because it is infinite —
      a Python ``int`` never is — but because ``math.isfinite`` itself raises
      ``OverflowError`` on one, and so does every downstream ``expected_ip * 3``
      or ``0.6 * outs``. A value no float arithmetic can accept is unusable
      wherever it would be used, so the honest answer here is False rather
      than an exception thrown from inside a predicate. ``json.loads`` parses
      an arbitrarily long integer literal straight off a card, so this is the
      shape that made the guard non-total in the first place.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
