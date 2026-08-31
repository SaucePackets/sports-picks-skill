#!/usr/bin/env python3
"""One shared "is this a number the arithmetic downstream can use" predicate.

Canonical copy: ``scripts/numeric_util.py`` in the sports-picks-skill repo.
A byte-identical copy ships next to deployed skill scripts (for same-directory
imports); keep every copy in sync with the canonical file.

Two modules import it today — the write side (``mlb_baseball_evidence`` at the
execution gate) and the read side (``mlb_postgame_evidence`` at settlement).
They had two copies of one rule, and two copies of one computation agree only
until one of them changes: the write side gained ``math.isfinite`` in PR #43
and the grader did not, which is the drift PR #68 was fixing. Neither may
re-derive it; the tests pin that each side CALLS this function, not merely that
it imports it.

**Two of five, not two of two.** The same rule is still written out separately
in ``mlb_probability_model.py`` (a verbatim copy of the body deleted from
``mlb_baseball_evidence``, and non-total in exactly the same way — a
``10 ** 400`` delta makes ``probability_component_errors`` raise instead of
returning its error list), ``vig_review_gate_common.py``, and
``mlb_runtime_policy.py``. ``mlb_lineup_watchlist.py`` has a fourth copy that
is a genuinely DIFFERENT rule wearing the same name: no finiteness clause at
all, so it accepts ``inf``. Those are out of the slice that created this
module and are named here so the next reader does not take this file as
evidence the drift is closed everywhere (Reviewer, PR #69).

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
