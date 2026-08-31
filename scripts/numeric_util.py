#!/usr/bin/env python3
"""One shared "is this a number the arithmetic downstream can use" predicate.

Canonical copy: ``scripts/numeric_util.py`` in the sports-picks-skill repo.
A byte-identical copy ships next to deployed skill scripts (for same-directory
imports); keep every copy in sync with the canonical file.

Three modules import it today — the write side (``mlb_baseball_evidence`` at
the execution gate), the read side (``mlb_postgame_evidence`` at settlement),
and ``mlb_probability_model`` at the probability-component contract. They had
three copies of one rule, and copies of one computation agree only until one of
them changes: the write side gained ``math.isfinite`` in PR #43 and the other
two did not, which is the drift PR #68 and PR #70 were fixing. None may
re-derive it.

**What the tests actually pin, stated no wider than the evidence.** Identity is
pinned for all three importers: each holds THIS function object, so a change to
the rule cannot reach one and miss another. Consultation — that a validator
CALLS the shared rule rather than re-deriving it inline while leaving the
import untouched — is pinned PER CALL SITE, at exactly five named sites:

- ``mlb_postgame_evidence.usable_expected_ip``
- ``mlb_baseball_evidence.validate_baseball_evidence``'s ``expected_ip`` check
- ``mlb_probability_model._component_entries``' entry-value check (the
  adjustment ``delta`` / haircut ``amount`` path)
- ``mlb_probability_model.validate_probability_components``' two own sites: the
  ``uncertainty_haircut`` numeric check and the ``conservative_probability``
  consistency check

Every other call site rests on identity alone, and identity does not catch a
re-derived copy at a call site. ``mlb_baseball_evidence`` has six ``_is_number``
call sites and one is pinned; re-deriving the check inline at another
(``supported_price``) leaves the suite green (Reviewer, PR #69). An earlier
version of this list said "``validate_probability_components`` here", which was
wrong twice over — the pinned site was in ``_component_entries``, and that
function's own two sites were unpinned, so re-deriving the rule at the haircut
check left the full suite green (Reviewer, PR #70). Naming a function when the
pin is on one call site inside it is the same overclaim in miniature. So: no
module-wide enforcement is claimed anywhere. Identity everywhere, consultation
at the five sites listed above.

**Three of five, not three of three.** The same rule is still written out
separately in ``vig_review_gate_common.py`` and ``mlb_runtime_policy.py``.
``mlb_lineup_watchlist.py`` has a copy that is a genuinely DIFFERENT rule
wearing the same name: no finiteness clause at all, so it accepts ``inf``.
Those are out of the slices that built this module and are named here so the
next reader does not take this file as evidence the drift is closed everywhere
(Reviewer, PR #69).

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
