#!/usr/bin/env python3
"""Per-game refusal recording for the MLB slate, and its coverage check.

Every analysis this repo has run so far — the gate replay (PR #63), the
side-selection attribution (PR #64), the loss-evidence report (PR #66) — takes
a PRICED CANDIDATE as its unit. A day on which nothing was priced contributes
zero rows to all of them, by construction. So the largest bucket of the
2026-08-11..08-31 drought (eleven ``slate_empty`` days against slates averaging
fourteen games) has never appeared in any corpus we have studied: we know the
gate's record on what it lets THROUGH and have never measured what it REFUSES.

The reasons are not missing — they are written into the slate prose, and on a
good day that prose is genuinely detailed. They are just not recorded. Over
2026-08-11..08-31, seven of twenty-one dates carry structured per-game reads
covering 88 of 209 scheduled games; the other 58% is narrated, and WHICH games
land in that 58% is decided by how tersely a particular run happened to write.
A refusal rate computed over it would measure the writeup's verbosity.

So this module defines a ``game_reads`` array on the schedule JSON and the
check that makes it honest. The design point is the one PR #65 paid two review
rounds to learn: **a field the prompt asks for is not a field that gets
written.** Structural forcing beats prompt instruction, so the denominator
comes from code and the reads come from the run:

- ``scripts/mlb_stage2_scan.py`` enumerates the slate deterministically and now
  emits ``game_pk`` alongside ``event_id``. Its rows are the denominator.
- The run records that denominator into the schedule as ``slate_denominator``
  and one ``game_reads`` entry per game.
- ``--validate`` refuses the slate when the two disagree, and ``--denominator``
  additionally checks the recorded denominator against a fresh scan output, so
  a run cannot shrink its own denominator to match a short read set.

**Both id spaces, always.** The slate's ``event_id`` is an ESPN id and the MLB
``gamePk`` is a different id space entirely — 2026-08-30 records ``401816733``
for the game MLB calls ``824876``. Anything joining the two on one id gets
silence that reads as missing data, which is how the drought diagnostic found
it. A read carries both, and validation requires both.

**An id matches records; it does not corroborate them.** Every join here keys
on ``game_pk``, and for a long time that was the whole of it: a read whose
``game_pk`` matched was counted as the read for that game, whatever else it
said. Both sides of every one of those joins also carry an ``event_id`` and the
two club names, checked for shape and never against each other — so a read
copied off the wrong game, or a doubleheader's two cards filled in against each
other, joined cleanly and passed. ``identity_agreement_errors`` closes that: the
id selects the pair, and the pair then has to agree.

**A missing number must say why.** A numeric field may be absent only when
``unavailable`` carries a non-empty reason for it. ``null`` with no reason is
an error, because "no price" and "a price nobody recorded" are different facts
and only one of them is a finding.

**What this does not do.** It does not judge a refusal, rank rails, or change
any gate. It records what the run already computed. And the vocabulary is
closed but not complete — it is the set of rails observable in the slate prose
plus the gates the watchlist already names; a refusal that fits none of them is
an error here rather than a silent ``other`` bucket, so the vocabulary grows by
review instead of by default.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import mlb_runtime_policy  # noqa: E402
from mlb_lineup_watchlist import (  # noqa: E402
    ALLOWED_BLOCKERS,
    PROMOTED_STATUS,
    REQUIRED_ORIGINAL_GATES,
)
from numeric_util import is_finite_number  # noqa: E402

# Rails that are not handicapping gates: the slate could not price the game at
# all, or a volume rail closed the card before the read mattered. Every one is
# observable verbatim in the 2026-08 slate prose ("DK line unavailable", "the
# exact Polymarket slug returned no market data", "underway, no action",
# "Coors is a 112 run-factor extreme hitter park, which caps confidence").
STRUCTURAL_RAILS = frozenset(
    {
        "no_dk_price",
        "no_polymarket_market",
        "game_already_started",
        "park_environment_cap",
        "daily_volume_cap",
        # Added because the hypothesis scan found refusals that fitted none of
        # the other twelve: "missing offense input", "Arizona offense row was
        # missing, so non-starter data is incomplete", "Starter missing for
        # Colorado". A required input that never arrived is a real and common
        # reason to refuse, and it was not a rail anyone had named. That is the
        # passenger doing its one job.
        "incomplete_input_data",
    }
)

# The closed vocabulary. The handicapping gates and the two deferrable blockers
# are IMPORTED rather than restated: this repo has now spent three review
# rounds (PRs #69, #70, #71) on copies of one rule drifting apart, and a
# restated gate list would be the same defect in a new file.
REFUSAL_RAILS = frozenset(REQUIRED_ORIGINAL_GATES) | frozenset(ALLOWED_BLOCKERS) | STRUCTURAL_RAILS

# What the run decided about the game. ``not_priced`` is deliberately distinct
# from ``pass``: a game nobody could price was never handicapped, and folding
# the two would recreate the drought report's own collapsed-class defect.
DISPOSITIONS = frozenset({"candidate", "lineup_watchlist", "pass", "not_priced"})

# A disposition that means "the card refused this game" must name at least one
# rail; a disposition that means "the card took it" must name none. Anything
# else is a read that does not say what happened.
REFUSING_DISPOSITIONS = frozenset({"pass", "not_priced"})
ACCEPTING_DISPOSITIONS = frozenset({"candidate", "lineup_watchlist"})

# Per-side probability fields. Each is either a two-sided object of usable
# probabilities or explicitly unavailable with a reason.
SIDE_PROBABILITY_FIELDS = (
    "dk_fair_prob",
    "polymarket_ask",
    # The handicap BEFORE the uncertainty buffer. Recorded on every game, not
    # just the ones we bet, because ``picks.json`` — the only source the model
    # evaluator has ever had — contains exclusively games the model liked
    # enough to clear a five-point floor. Calibration measured on that set is
    # calibration measured where the model was most confident. The reads are
    # the unbiased population.
    "raw_probability",
    "conservative_probability",
)
# Edges are signed and may legitimately be negative, so they are checked for
# usability but not for the 0 < x < 1 range.
SIDE_SIGNED_FIELDS = ("net_edge",)
SIDE_FIELDS = SIDE_PROBABILITY_FIELDS + SIDE_SIGNED_FIELDS

# One non-negative number for the whole read, not a per-side object: the
# uncertainty haircut is a buffer on the handicap, and the market-only fallback
# charges ZERO. Zero is the single most common legal value, which is why this
# field can never be checked with the probability rule — ``0 < x < 1`` would
# reject the fallback's own contract.
SCALAR_NON_NEGATIVE_FIELDS = ("uncertainty_haircut",)

# Which model produced ``raw_probability``. The deployment gate filters rows by
# this exact string, so a probability recorded without it cannot be evaluated,
# only counted.
READ_STRING_FIELDS = ("model_version",)

# Everything a read may declare unavailable-with-a-reason. Absence still has to
# be explained; that requirement is the module's whole point.
EXPLAINABLE_FIELDS = SIDE_FIELDS + SCALAR_NON_NEGATIVE_FIELDS + READ_STRING_FIELDS

# ``conservative_probability == raw_probability - uncertainty_haircut`` is the
# contract in mlb_probability_model, applied here per side. The tolerance is
# defined independently rather than imported: a recording check must not pull
# the execution-path model module into its import closure. A test pins the two
# constants equal so the pair cannot drift apart in silence.
COHERENCE_TOLERANCE = 1e-3

IDENTITY_STRING_FIELDS = ("event_id", "away", "home")


def _is_probability(value: Any) -> bool:
    """A usable number strictly inside (0, 1).

    Consults the shared rule rather than re-deriving it. See
    ``numeric_util.is_finite_number`` for why the range clause alone is not
    enough and why the two clauses are not interchangeable.
    """
    return is_finite_number(value) and 0 < value < 1


def _identity_errors(label: str, entry: dict[str, Any]) -> list[str]:
    """Both id spaces present and well-formed, plus the team names."""
    errors: list[str] = []
    game_pk = entry.get("game_pk")
    if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
        errors.append(f"{label}.game_pk must be a positive integer MLB gamePk")
    for field in IDENTITY_STRING_FIELDS:
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}.{field} must be a non-empty string")
    return errors


def _normalized_name(value: Any) -> str | None:
    """A club name reduced to what two vocabularies can be compared on.

    Case and internal whitespace only. Nothing here tries to map ``NYM`` onto
    ``New York Mets``: the comparison this feeds is a *crossing* test, and a
    normalisation that silently failed to match would make that test pass for
    every row — a check indistinguishable from having no check at all.
    """
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split()).casefold()
    return collapsed or None


def normalize_event_id(value: Any) -> Any:
    """The one canonical spelling of an ``event_id``.

    The same move ``mlb_slate_writer.normalize_slate_date`` makes, for the same
    reason: the agreement check below strips both sides, so ``" 401 "`` agrees
    with ``"401"`` and is then persisted with its padding — validated in one
    spelling, written in another. An ``event_id`` is an address as well as a
    label (``mlb_lineup_watchlist`` builds a URL out of ``str(event_id)``), so
    the padded form is a wrong address rather than a cosmetic difference.

    Anything that is not a non-empty string is returned unchanged: refusing it
    is ``_identity_errors``' job, and that refusal should name the value the
    producer actually wrote.
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return value


def identity_agreement_errors(
    label: str, expected: dict[str, Any], actual: dict[str, Any]
) -> list[str]:
    """Two records already matched on ``game_pk`` must agree on the rest.

    ``game_pk`` is a *disambiguator*, not a corroboration. Every join in this
    module keys on it alone, and both sides of every one of those joins also
    carry an ``event_id`` and the two club names — shape-checked individually by
    ``_identity_errors`` and until now never compared to each other. A read
    copied from the wrong game, or a doubleheader's two cards filled in against
    each other, joins cleanly on an edited ``game_pk`` and is counted as
    coverage. Nothing downstream can see it: the probabilities are well-formed,
    they are simply about another game.

    The two rules are deliberately different strengths, because the two fields
    are different kinds of thing:

    - ``event_id`` must AGREE. It is an opaque ESPN id with exactly one source
      — the scan — travelling by copy. There is no honest reason for two
      records about one game to carry different ones.
    - ``away``/``home`` are checked for CROSSING, not equality. The names may be
      written in different vocabularies (``NYM`` against ``New York Mets``) and
      no corpus exists yet to measure how often, so demanding equality would
      refuse honest rows for cosmetic drift. A crossed pair is the failure that
      changes what a read MEANS — every probability on it lands on the other
      club.

    DECLARED LIMIT, and it is the price of choosing crossing over equality: the
    crossing test compares normal forms, so it bites only when the two records
    name the clubs in the SAME vocabulary. A read carrying ``away: "NYM"`` and
    ``home: "ATL"`` against a denominator saying ``Atlanta Braves`` at ``New
    York Mets`` is crossed and matches on NEITHER side, so nothing is reported.
    Seeing it would take a club-name resolver this module does not have and
    will not guess at.

    What keeps the limit narrow is that on the sanctioned path both records
    descend from one scan: ``mlb_slate_writer.skeleton`` prefills ``away``,
    ``home`` and ``event_id`` into the stub the run fills in, so the vocabulary
    is shared and a crossing made while filling that stub IS caught. The
    uncaught case is a read whose club names were retyped in another vocabulary
    AND crossed. It is pinned by
    ``test_a_crossing_written_in_another_vocabulary_is_a_declared_limit`` so the
    boundary is a checked fact rather than a gap, and it is stated in
    ``mlb.md``, which is what the run reads — a rail promised in the doc and
    absent from the code is worse than an absent rail, because nobody looks for
    it again.

    Fields the two records do not both carry are skipped here; their absence is
    already reported by ``_identity_errors``, and repeating it would bury the
    disagreement this function exists to name.
    """
    errors: list[str] = []
    # Compare the canonical forms, and let the writer persist that same form —
    # one function decides what an ``event_id`` IS, so the value compared here
    # is the value written there.
    expected_event = normalize_event_id(expected.get("event_id"))
    actual_event = normalize_event_id(actual.get("event_id"))
    # A blank id is an absence, not a disagreement: ``_identity_errors`` already
    # names it, and repeating it here would bury the finding this exists for.
    if (
        isinstance(expected_event, str)
        and isinstance(actual_event, str)
        and expected_event.strip()
        and actual_event.strip()
        and expected_event != actual_event
    ):
        errors.append(
            f"{label}.event_id is {actual_event!r} but the same game_pk is "
            f"{expected_event!r}; one of these records is about a different "
            "game — matching game_pk alone does not make them the same game"
        )

    expected_away = _normalized_name(expected.get("away"))
    expected_home = _normalized_name(expected.get("home"))
    actual_away = _normalized_name(actual.get("away"))
    actual_home = _normalized_name(actual.get("home"))
    # A pair that cannot tell its own sides apart cannot witness a crossing:
    # against ``away == home`` every row reads as swapped. That record is
    # already broken and says so elsewhere.
    if None not in (expected_away, expected_home, actual_away, actual_home) and (
        expected_away != expected_home
    ):
        # Either side crossed is the same backwards row. Requiring both to cross
        # would let a half-transposed record through, and a read with the away
        # club in the home slot is exactly as wrong as one with both.
        if actual_away == expected_home or actual_home == expected_away:
            errors.append(
                f"{label} has away/home transposed against the same game_pk "
                f"(recorded {actual.get('away')!r} at {actual.get('home')!r}, "
                f"the game is {expected.get('away')!r} at {expected.get('home')!r}); "
                "every per-side number on this record is on the wrong club"
            )
    return errors


def _side_field_errors(label: str, entry: dict[str, Any]) -> list[str]:
    """Every numeric field is either usable on both sides or explained.

    The explanation requirement is the point. A field left ``null`` with no
    entry in ``unavailable`` is indistinguishable from a field nobody got
    around to writing, and this whole module exists because that distinction
    was lost in prose.
    """
    errors: list[str] = []
    unavailable = entry.get("unavailable")
    if unavailable is None:
        unavailable = {}
    if not isinstance(unavailable, dict):
        return [f"{label}.unavailable must be an object mapping field name to reason"]

    for field in unavailable:
        if field not in EXPLAINABLE_FIELDS:
            errors.append(f"{label}.unavailable names unknown field {field!r}")
        reason = unavailable[field]
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"{label}.unavailable[{field!r}] must be a non-empty reason")

    for field in SIDE_FIELDS:
        value = entry.get(field)
        explained = field in unavailable
        if value is None:
            if not explained:
                errors.append(
                    f"{label}.{field} is absent and {label}.unavailable does not say why"
                )
            continue
        if explained:
            errors.append(
                f"{label}.{field} is recorded but also listed unavailable; it is one or the other"
            )
        if not isinstance(value, dict):
            errors.append(f"{label}.{field} must be an object with away and home")
            continue
        extra = set(value) - {"away", "home"}
        if extra:
            errors.append(f"{label}.{field} has unexpected side(s) {sorted(extra)}")
        for side in ("away", "home"):
            if side not in value:
                errors.append(f"{label}.{field}.{side} is missing")
                continue
            side_value = value[side]
            if field in SIDE_PROBABILITY_FIELDS:
                if not _is_probability(side_value):
                    errors.append(
                        f"{label}.{field}.{side} must be a usable probability strictly inside (0, 1)"
                    )
            elif not is_finite_number(side_value):
                errors.append(f"{label}.{field}.{side} must be a usable number")

    for field in SCALAR_NON_NEGATIVE_FIELDS:
        value = entry.get(field)
        explained = field in unavailable
        if value is None:
            if not explained:
                errors.append(
                    f"{label}.{field} is absent and {label}.unavailable does not say why"
                )
            continue
        if explained:
            errors.append(
                f"{label}.{field} is recorded but also listed unavailable; it is one or the other"
            )
        # Deliberately NOT the probability rule: a zero haircut is the
        # market-only fallback's own contract, and 0 < x < 1 would reject it.
        if not is_finite_number(value) or value < 0:
            errors.append(f"{label}.{field} must be a usable number greater than or equal to 0")

    for field in READ_STRING_FIELDS:
        value = entry.get(field)
        explained = field in unavailable
        if value is None:
            if not explained:
                errors.append(
                    f"{label}.{field} is absent and {label}.unavailable does not say why"
                )
            continue
        if explained:
            errors.append(
                f"{label}.{field} is recorded but also listed unavailable; it is one or the other"
            )
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}.{field} must be a non-empty string")

    return errors


# The trail is all-or-nothing. Naming them here so the rule below is a list to
# check rather than a chain of conditions to reason about.
MODEL_TRAIL_FIELDS = (
    "raw_probability",
    "uncertainty_haircut",
    "conservative_probability",
    "model_version",
)

# The other half of the same idea, on the price side. ``net_edge`` is the number
# every selection rail is argued from, and until now it was a free-form signed
# number: a read could say ``net_edge`` +0.09 beside a ``conservative`` and an
# ``ask`` whose difference was -0.01 and validate clean. Two facts written side
# by side and never joined — the shape this lane has now paid for four times.
EDGE_TRAIL_FIELDS = (
    "polymarket_ask",
    "conservative_probability",
    "net_edge",
)


def _model_trail_errors(label: str, entry: dict[str, Any]) -> list[str]:
    """The model trail is recorded whole, or not at all, and it must reconcile.

    A read may legitimately carry no handicap — a game with no DK price was
    never handicapped, and every field then says so in ``unavailable``. What it
    may not do is carry PART of a handicap, because every check that makes the
    rest of the trail trustworthy is conditional on the piece that is missing.

    That was a real hole and not a hypothetical one: with
    ``uncertainty_haircut`` merely excused as unavailable, ``conservative``
    could disagree with ``raw`` by fifty points and validate clean, because the
    coherence loop had nothing to subtract. A guard that treats "the field was
    explained away" as "there is nothing to check here" is half-wired — the
    explanation is exactly what removes the evidence. So the requirement is
    symmetric: record one of these and you owe all of them.
    """
    errors: list[str] = []
    recorded = [field for field in MODEL_TRAIL_FIELDS if entry.get(field) is not None]
    if not recorded:
        return errors
    absent = [field for field in MODEL_TRAIL_FIELDS if entry.get(field) is None]
    if absent:
        errors.append(
            f"{label} records part of the model trail ({', '.join(recorded)}) but not "
            f"{', '.join(absent)}; a partial handicap cannot be checked or evaluated"
        )

    raw = entry.get("raw_probability")
    conservative = entry.get("conservative_probability")
    haircut = entry.get("uncertainty_haircut")
    if not isinstance(raw, dict) or not isinstance(conservative, dict):
        return errors
    if not is_finite_number(haircut) or haircut < 0:
        return errors
    for side in ("away", "home"):
        raw_side = raw.get(side)
        conservative_side = conservative.get(side)
        if not _is_probability(raw_side) or not _is_probability(conservative_side):
            continue
        expected = float(raw_side) - float(haircut)
        if abs(float(conservative_side) - expected) > COHERENCE_TOLERANCE:
            errors.append(
                f"{label}.conservative_probability.{side} is {float(conservative_side):.4f} but "
                f"raw_probability.{side} minus uncertainty_haircut is {expected:.4f}"
            )
    return errors


def _edge_trail_errors(label: str, entry: dict[str, Any]) -> list[str]:
    """``net_edge`` is the arithmetic it claims to be, per side.

    ``conservative_probability - polymarket_ask`` is the definition every
    selection rail in this repo argues from — ``mlb_runtime_policy`` recomputes
    exactly it at execution time and refuses a candidate whose stored edge
    disagrees. On a ``game_reads`` entry the same number was free-form: nothing
    compared it to the two numbers recorded beside it, so a read could name
    ``price_discipline`` while its own recorded edge cleared the floor, or claim
    an edge on a game it never priced, and pass every check.

    Symmetric, for the reason PR #74's blocker taught: record one of the three
    and you owe all three. Excusing ``polymarket_ask`` as unavailable while
    keeping ``net_edge`` would leave nothing to subtract, and a guard that reads
    "the field was explained away" as "there is nothing to check here" is
    half-wired — the explanation is exactly what removes the evidence.

    Record ``net_edge`` and you owe both operands: an edge with nothing to
    subtract is a number no one can check, which is the state it was already in.

    The converse is deliberately NOT required. A read may record both operands
    and explain, in ``unavailable``, that the edge was never computed — the
    2026-08 corpus contains exactly that, and ``mlb_measurement_lane`` classifies
    it as a process failure rather than pretending it did not happen. Demanding
    the field here would relabel a real recorded state as a malformed record and
    make it invisible to the report that counts it. It costs nothing, because
    ``policy_disposition_errors`` and ``mlb_eligibility_report`` both RECOMPUTE
    the edge from the operands: a read cannot escape the floor rail by declining
    to write the number down.
    """
    errors: list[str] = []
    if entry.get("net_edge") is None:
        return errors
    absent = [
        field
        for field in ("polymarket_ask", "conservative_probability")
        if entry.get(field) is None
    ]
    if absent:
        errors.append(
            f"{label} records net_edge but not {', '.join(absent)}; net_edge is "
            "conservative_probability minus polymarket_ask and cannot be checked "
            "without both"
        )
        return errors

    ask = entry.get("polymarket_ask")
    conservative = entry.get("conservative_probability")
    edge = entry.get("net_edge")
    if not all(isinstance(value, dict) for value in (ask, conservative, edge)):
        return errors
    for side in ("away", "home"):
        ask_side = ask.get(side)
        conservative_side = conservative.get(side)
        edge_side = edge.get(side)
        if not _is_probability(ask_side) or not _is_probability(conservative_side):
            continue
        if not is_finite_number(edge_side):
            continue
        expected = float(conservative_side) - float(ask_side)
        if abs(float(edge_side) - expected) > COHERENCE_TOLERANCE:
            errors.append(
                f"{label}.net_edge.{side} is {float(edge_side):.4f} but "
                f"conservative_probability.{side} minus polymarket_ask.{side} is "
                f"{expected:.4f}"
            )
    return errors


def side_edges(entry: dict[str, Any]) -> dict[str, float]:
    """Each side's conservative edge, RECOMPUTED from the read's own numbers.

    One definition of "this read's edge on this side", read by the disposition
    rail below and by ``mlb_eligibility_report``. Two copies of it would be two
    answers to the question the whole slice exists to make answerable.

    Recomputed and not read off ``net_edge``, for two reasons that point the
    same way. A stored number is a claim and the subtraction is the fact —
    ``mlb_runtime_policy.live_conservative_edge`` makes exactly this choice at
    execution time. And a read may legally omit ``net_edge`` with a reason, so
    keying the floor rail on the stored field would let a run escape the rail by
    declining to write the number down. Where both exist they are equal:
    ``_edge_trail_errors`` refuses the read otherwise.
    """
    edges: dict[str, float] = {}
    ask = entry.get("polymarket_ask")
    conservative = entry.get("conservative_probability")
    if not isinstance(ask, dict) or not isinstance(conservative, dict):
        return edges
    for side in ("away", "home"):
        ask_side = ask.get(side)
        conservative_side = conservative.get(side)
        if is_finite_number(ask_side) and is_finite_number(conservative_side):
            edges[side] = float(conservative_side) - float(ask_side)
    return edges


def _disposition_number_errors(label: str, entry: dict[str, Any]) -> list[str]:
    """The disposition has to agree with the numbers recorded beside it.

    ``_disposition_errors`` already checks the disposition against its RAILS.
    This checks it against the read's own arithmetic, which is the half that was
    missing: the 2026-09-02 slate could have carded a game it never priced, or
    written ``pass`` on a side clearing the floor, and every validator in this
    repo would have passed it.

    The floor itself is NOT here. It is policy, it is loaded, and it lives in
    ``policy_disposition_errors`` so that a check depending on machine state
    cannot masquerade as a property of the record.
    """
    errors: list[str] = []
    disposition = entry.get("disposition")
    if disposition not in ACCEPTING_DISPOSITIONS:
        return errors
    # You cannot card a game you did not price, and you cannot card one you did
    # not handicap: the candidate the reviewer receives is built from exactly
    # these numbers, and a card whose read carries none of them is a decision
    # with no recorded basis at all.
    missing = [
        field
        for field in ("polymarket_ask", *MODEL_TRAIL_FIELDS)
        if entry.get(field) is None
    ]
    if missing:
        errors.append(
            f"{label}.disposition is {disposition!r} but the read records no "
            f"{', '.join(missing)}; a game that was not priced and handicapped "
            "cannot be carded"
        )
    return errors


def _disposition_errors(label: str, entry: dict[str, Any]) -> list[str]:
    """The disposition and its rails have to agree about what happened."""
    errors: list[str] = []
    disposition = entry.get("disposition")
    if disposition not in DISPOSITIONS:
        errors.append(
            f"{label}.disposition must be one of {sorted(DISPOSITIONS)}, got {disposition!r}"
        )

    rails = entry.get("refusing_rails")
    if not isinstance(rails, list):
        errors.append(f"{label}.refusing_rails must be a list")
        return errors
    for rail in rails:
        if rail not in REFUSAL_RAILS:
            errors.append(
                f"{label}.refusing_rails contains unknown rail {rail!r}; "
                f"known rails are {sorted(REFUSAL_RAILS)}"
            )
    if len(set(map(repr, rails))) != len(rails):
        errors.append(f"{label}.refusing_rails repeats a rail")

    if disposition in REFUSING_DISPOSITIONS and not rails:
        errors.append(f"{label}.disposition is {disposition!r} but names no refusing rail")
    if disposition in ACCEPTING_DISPOSITIONS and rails:
        errors.append(
            f"{label}.disposition is {disposition!r} but names refusing rails {sorted(rails)}"
        )
    return errors


def validate_read(entry: Any, index: int) -> list[str]:
    """Every error in one ``game_reads`` entry, as a flat list of strings."""
    label = f"game_reads[{index}]"
    if not isinstance(entry, dict):
        return [f"{label} must be an object"]
    return (
        _identity_errors(label, entry)
        + _side_field_errors(label, entry)
        + _model_trail_errors(label, entry)
        + _edge_trail_errors(label, entry)
        + _disposition_errors(label, entry)
        + _disposition_number_errors(label, entry)
    )


# The one rail this module cannot answer from the record alone: the edge floor
# is policy, loaded from risk_limits.json, and a validator that guessed at it
# would be the restated-constant defect three PRs in this repo have already
# paid for. So the floor arrives as an argument and its ABSENCE is reported,
# never skipped.
PRICE_RAIL = "price_discipline"


def policy_disposition_errors(schedule: Any, policy: Any) -> list[str]:
    """``price_discipline`` may not be named on a side whose own edge clears the floor.

    This is the rail that makes a refusal auditable rather than narrated. A read
    saying "the price was not disciplined enough" while recording an edge at or
    above ``min_conservative_edge`` is a record that contradicts itself, and it
    is exactly the state the 2026-09-02 slate could not be checked for: the
    rails were recorded and nothing compared them to the numbers.

    ``policy`` is ``mlb_runtime_policy.MlbSelectionPolicy`` or ``None``. None is
    what the loader returns when the policy block is missing or malformed — it
    fails closed on purpose so no caller silently substitutes a hard-coded
    floor, and this function keeps that promise: with no policy, a read naming
    the price rail is UNCHECKABLE and says so. Reads that never name the rail
    need no floor, so a day that does not turn on the policy is not blocked by
    its absence.
    """
    errors: list[str] = []
    if not isinstance(schedule, dict):
        return errors
    reads = schedule.get("game_reads")
    if not isinstance(reads, list):
        return errors
    floor = getattr(policy, "min_conservative_edge", None)
    for index, entry in enumerate(reads):
        if not isinstance(entry, dict):
            continue
        rails = entry.get("refusing_rails")
        if not isinstance(rails, list) or PRICE_RAIL not in rails:
            continue
        label = f"game_reads[{index}]"
        if not is_finite_number(floor):
            errors.append(
                f"{label} names {PRICE_RAIL!r} and the MLB selection policy is "
                "unavailable, so the claim cannot be checked against the edge "
                "floor; a refusal nobody can check is the state this record exists "
                "to end"
            )
            continue
        for side, edge in sorted(side_edges(entry).items()):
            if edge >= float(floor):
                errors.append(
                    f"{label} names {PRICE_RAIL!r} but its own "
                    f"conservative_probability.{side} minus polymarket_ask.{side} is "
                    f"{edge:.4f}, at or above the {float(floor):.4f} floor; the "
                    "recorded rail and the recorded numbers disagree about this side"
                )
    return errors


def denominator_games(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    """The games the slate is accountable for, or ``[]`` when unrecorded."""
    denominator = schedule.get("slate_denominator")
    if not isinstance(denominator, dict):
        return []
    games = denominator.get("games")
    return games if isinstance(games, list) else []


def _denominator_errors(schedule: dict[str, Any]) -> list[str]:
    """The recorded denominator itself has to be usable before it can bind.

    Fail closed: a slate with no denominator is UNVERIFIABLE, not compliant.
    An earlier draft of this module keyed coverage on ``audit-results/``, and
    every file in that directory turned out to carry an ``_audit_fetched_at_utc``
    from the drought lane's own analysis run — it is written by the audit
    tooling, not by the scan, so at slate time it does not exist yet.
    """
    denominator = schedule.get("slate_denominator")
    if not isinstance(denominator, dict):
        return ["slate_denominator is missing; coverage cannot be checked"]
    errors: list[str] = []
    source = denominator.get("source")
    if not isinstance(source, str) or not source.strip():
        errors.append("slate_denominator.source must name where the game list came from")
    fetched = denominator.get("fetched_at_utc")
    if not isinstance(fetched, str) or not fetched.strip():
        errors.append("slate_denominator.fetched_at_utc must be a non-empty timestamp")
    games = denominator.get("games")
    if not isinstance(games, list):
        return errors + ["slate_denominator.games must be a list"]
    for index, game in enumerate(games):
        label = f"slate_denominator.games[{index}]"
        if not isinstance(game, dict):
            errors.append(f"{label} must be an object")
            continue
        errors.extend(_identity_errors(label, game))
    return errors


def coverage_errors(schedule: dict[str, Any]) -> list[str]:
    """One read per scheduled game, matched on ``game_pk`` and then corroborated.

    Both directions of the count matter and for different reasons. A game in
    the denominator with no read is the terse-writeup failure this module
    exists to stop. A read for a game NOT in the denominator means the read set
    and the schedule disagree about what was played, which is the id-space
    defect showing up as a phantom row.

    Counting is not the whole job. ``game_pk`` decides WHICH denominator entry
    a read is about; it does not establish that the read is about that game.
    Each matched pair is put through ``identity_agreement_errors`` so a read
    carrying another game's ``event_id`` or the two clubs the wrong way round
    is named rather than counted as coverage.
    """
    errors = _denominator_errors(schedule)
    reads = schedule.get("game_reads")
    if not isinstance(reads, list):
        return errors + ["game_reads must be a list"]

    expected: list[int] = []
    expected_by_pk: dict[int, dict[str, Any]] = {}
    for game in denominator_games(schedule):
        if isinstance(game, dict) and isinstance(game.get("game_pk"), int):
            expected.append(game["game_pk"])
            expected_by_pk.setdefault(game["game_pk"], game)
    seen: dict[int, int] = {}
    for entry in reads:
        if isinstance(entry, dict) and isinstance(entry.get("game_pk"), int):
            seen[entry["game_pk"]] = seen.get(entry["game_pk"], 0) + 1

    for game_pk in expected:
        if game_pk not in seen:
            errors.append(f"scheduled game {game_pk} has no game_reads entry")
    for game_pk, count in sorted(seen.items()):
        if game_pk not in expected:
            errors.append(f"game_reads entry {game_pk} is not in slate_denominator")
        if count > 1:
            errors.append(f"game_reads has {count} entries for game {game_pk}")
    # A ``game_pk`` the denominator lists twice cannot say which of its entries
    # a read agrees with, so the corroboration below would be answering an
    # arbitrary question. Ambiguity in the join is itself the defect.
    for game_pk in sorted({pk for pk in expected if expected.count(pk) > 1}):
        errors.append(
            f"slate_denominator lists game {game_pk} more than once; a join on "
            "game_pk cannot say which entry a read belongs to"
        )

    for index, entry in enumerate(reads):
        # ``isinstance`` and not a bare ``.get``: a read whose ``game_pk`` is a
        # list is unhashable and would raise here rather than be reported.
        if not isinstance(entry, dict) or not isinstance(entry.get("game_pk"), int):
            continue
        game = expected_by_pk.get(entry["game_pk"])
        if isinstance(game, dict):
            errors.extend(
                identity_agreement_errors(f"game_reads[{index}]", game, entry)
            )
    return errors


def deferred_watchlist_entries(schedule: dict[str, Any]) -> list[Any]:
    """Watchlist entries the card is still deferring, i.e. not promoted.

    A promoted entry stays on ``lineup_watchlist`` forever — that is its audit
    trail — but the game itself has moved onto the card and owns a
    ``candidates[]`` element. Counting it in both populations is what made the
    ``lineup_watchlist`` half of the reconciliation identity uncheckable: with
    the promoted entry counted here, the read for that game could not say
    ``candidate`` (the candidate half) and ``lineup_watchlist`` (this half) at
    once, so one of the two counts had to be wrong no matter what the recorder
    wrote.

    ``promoted`` and not ``TERMINAL_STATUSES``: ``passed`` is a game the recheck
    dropped, which never reaches ``candidates[]`` and is still a deferral in
    the record, and nothing in this repo writes ``filled_manual`` at all.
    """
    entries = schedule.get("lineup_watchlist")
    if not isinstance(entries, list):
        return []
    # A non-dict entry is malformed, not promoted; it stays in the deferred
    # population so this count never quietly shrinks around junk.
    return [
        entry
        for entry in entries
        if not isinstance(entry, dict) or entry.get("status") != PROMOTED_STATUS
    ]


def card_reconciliation_errors(schedule: dict[str, Any]) -> list[str]:
    """The reads and the card must agree on how many games were taken.

    A read set that says ``candidate`` three times over a schedule carrying one
    candidate is not a recording detail — it means the record of the decision
    and the decision itself have come apart, and the record is what every later
    analysis will read.

    Each half names its own POPULATION rather than a schedule key, because the
    two are not the same list: ``candidates`` includes the games a review gate
    promoted off the watchlist, and the watchlist half must therefore exclude
    exactly those. On 2026-09-03 they were counted as both, and the identity
    reported ``1 game_reads entries say 'candidate' but the schedule carries 2
    candidates`` for a promotion whose read nothing had updated.
    """
    errors: list[str] = []
    reads = schedule.get("game_reads")
    if not isinstance(reads, list):
        return errors
    candidates = schedule.get("candidates")
    populations = (
        ("candidate", "candidates", candidates if isinstance(candidates, list) else []),
        (
            "lineup_watchlist",
            "un-promoted lineup_watchlist entries",
            deferred_watchlist_entries(schedule),
        ),
    )
    for disposition, label, population in populations:
        recorded = sum(
            1
            for entry in reads
            if isinstance(entry, dict) and entry.get("disposition") == disposition
        )
        if recorded != len(population):
            errors.append(
                f"{recorded} game_reads entries say {disposition!r} but the schedule carries "
                f"{len(population)} {label}"
            )
    return errors


def _event_key(value: Any) -> str | None:
    """A comparable ``event_id``, or None when the value addresses nothing.

    ``normalize_event_id`` deliberately returns a blank or non-string value
    UNCHANGED — refusing it belongs to ``_identity_errors``, which must name
    what the producer wrote. A join key cannot inherit that: ``"   "`` is an
    absent id here, not a value to match on, and an integer id must find the
    string one, because the schedule and the read are written by different
    producers that do not agree on the JSON type.
    """
    normalized = normalize_event_id(value)
    if isinstance(normalized, str):
        return normalized.strip() or None
    if isinstance(normalized, int) and not isinstance(normalized, bool):
        return str(normalized)
    return None


def _read_matches(
    reads: list[Any], game_pk: Any, event_id: Any
) -> tuple[list[int], str]:
    """Indices of the reads for one game, and the key that selected them.

    ``game_pk`` first and alone when it is present, because it is the only key
    that separates a doubleheader's two games; ``event_id`` is the fallback for
    a promotion whose entry was never stamped with a ``game_pk`` (the field is
    optional on a watchlist entry). The two are different id spaces and are
    never mixed into one match set: a read matching on one and a different read
    matching on the other is an ambiguity, not two votes.
    """
    if isinstance(game_pk, int) and not isinstance(game_pk, bool):
        return (
            [
                index
                for index, entry in enumerate(reads)
                if isinstance(entry, dict) and entry.get("game_pk") == game_pk
            ],
            f"game_pk {game_pk}",
        )
    wanted = _event_key(event_id)
    if wanted is not None:
        return (
            [
                index
                for index, entry in enumerate(reads)
                if isinstance(entry, dict)
                and _event_key(entry.get("event_id")) == wanted
            ],
            f"event_id {wanted}",
        )
    return [], ""


def record_promotion_as_candidate(
    schedule: dict[str, Any], label: str, game_pk: Any = None, event_id: Any = None
) -> list[str]:
    """Re-label the promoted game's read from ``lineup_watchlist`` to ``candidate``.

    The morning slate owns ``game_reads`` and writes each read once. When the
    review gate promotes a watchlist entry the game moves onto the card and
    nothing had ever updated its read, so the reconciliation identity above
    broke every time a promotion succeeded — and it healed itself when the
    candidate left the card, which is worse: a per-game record that is wrong
    only while anyone would look at it.

    The re-label is a LABEL change and nothing else. Both dispositions are in
    ``ACCEPTING_DISPOSITIONS``, which is what makes that safe — they impose
    exactly the same requirements on the read's numbers and rails, so a read
    that was valid as ``lineup_watchlist`` is valid as ``candidate``. A test
    pins that equivalence rather than trusting this sentence.

    Fails closed, and returns errors instead of raising: no read for the
    promoted game, more than one, or a read that says the card refused the game
    are each a state where the record and the decision disagree, and a
    promotion may not be accepted while they do.
    """
    reads = schedule.get("game_reads")
    if not isinstance(reads, list):
        # A schedule with no read set records nothing to reconcile, and
        # ``card_reconciliation_errors`` is silent on it for the same reason.
        # Refusing here would wedge every pre-recorder schedule.
        return []
    matches, key = _read_matches(reads, game_pk, event_id)
    if not key:
        return [
            f"{label} carries neither a game_pk nor an event_id, so its game_reads "
            "entry cannot be located; the promotion cannot be recorded"
        ]
    if len(matches) != 1:
        return [
            f"{label} matches {len(matches)} game_reads entries on {key}; exactly "
            "one read must record the promoted game"
        ]
    entry = reads[matches[0]]
    disposition = entry.get("disposition")
    if disposition == "candidate":
        # Idempotent: a re-run of the same promotion finds the read already
        # moved. Not an error, and not a second write.
        return []
    if disposition != "lineup_watchlist":
        return [
            f"{label} matched game_reads[{matches[0]}] on {key}, but that read says "
            f"{disposition!r}; a promoted game's read must have said "
            "'lineup_watchlist' before the promotion"
        ]
    entry["disposition"] = "candidate"
    return []


def validate_game_reads(schedule: Any) -> list[str]:
    """Every error in a schedule's per-game refusal record."""
    if not isinstance(schedule, dict):
        return ["schedule must be a JSON object"]
    reads = schedule.get("game_reads")
    if not isinstance(reads, list):
        return ["game_reads must be a list"] + _denominator_errors(schedule)
    errors: list[str] = []
    for index, entry in enumerate(reads):
        errors.extend(validate_read(entry, index))
    errors.extend(coverage_errors(schedule))
    errors.extend(card_reconciliation_errors(schedule))
    return errors


def scan_denominator_errors(schedule: dict[str, Any], scan_rows: Any) -> list[str]:
    """Cross-check the recorded denominator against a fresh scan output.

    Without this, the denominator is only as honest as the run that wrote it —
    a run could record the games it felt like reading and be perfectly
    self-consistent. ``mlb_stage2_scan.py`` output is the independent copy.
    """
    if not isinstance(scan_rows, list):
        return ["scan output must be a JSON list of rows"]
    scanned = {
        row["game_pk"]
        for row in scan_rows
        if isinstance(row, dict) and isinstance(row.get("game_pk"), int)
    }
    unresolved = [
        row
        for row in scan_rows
        if isinstance(row, dict) and not isinstance(row.get("game_pk"), int)
    ]
    recorded = {
        game["game_pk"]
        for game in denominator_games(schedule)
        if isinstance(game, dict) and isinstance(game.get("game_pk"), int)
    }
    errors: list[str] = []
    for game_pk in sorted(scanned - recorded):
        errors.append(f"scan lists game {game_pk} but slate_denominator does not")
    for game_pk in sorted(recorded - scanned):
        errors.append(f"slate_denominator lists game {game_pk} but the scan does not")
    # The same join, the same weakness: two sets of ``game_pk`` agreeing says
    # the rosters are the same SIZE and carry the same ids, not that the entry
    # under an id describes the game the scan found under it. The scan is the
    # independent copy, so it is the expected side.
    scanned_rows: dict[int, dict[str, Any]] = {}
    for row in scan_rows:
        if isinstance(row, dict) and isinstance(row.get("game_pk"), int):
            scanned_rows.setdefault(row["game_pk"], row)
    for game in denominator_games(schedule):
        if not isinstance(game, dict) or not isinstance(game.get("game_pk"), int):
            continue
        row = scanned_rows.get(game["game_pk"])
        if row is not None:
            errors.extend(
                identity_agreement_errors(
                    f"slate_denominator game {game['game_pk']}", row, game
                )
            )
    if unresolved:
        errors.append(
            f"{len(unresolved)} scan row(s) carry no game_pk; the denominator cannot be "
            "verified against a scan that failed to identify every game"
        )
    return errors


DENOMINATOR_DIRNAME = "tmp"
DENOMINATOR_STEM = "stage2"


def conventional_denominator_path(
    schedule_path: Path, schedule: Any = None
) -> Path | None:
    """Where ``mlb_stage2_scan`` leaves the denominator for a given schedule.

    The cross-check that stops a run trimming its own roster used to depend on
    somebody remembering ``--denominator``. An optional flag is not a rail:
    the 2026-09-01 slate ran, reported success, and wrote a schedule with no
    ``game_reads`` and no ``slate_denominator`` at all — and there was no scan
    artifact on disk to check it against either, because nothing had ever been
    asked to persist one. Both halves are fixed by making the location a
    convention that the scan writes and this validator reads, so neither side
    needs an argument to find the other.

    Returns None only when the schedule's date cannot be determined, which is
    itself reported as an error by the caller rather than skipped.
    """
    date = None
    if isinstance(schedule, dict) and isinstance(schedule.get("date"), str):
        date = schedule["date"].strip() or None
    if date is None:
        stem = schedule_path.name
        if stem.endswith("-schedule.json"):
            date = stem[: -len("-schedule.json")]
    if not date:
        return None
    return schedule_path.parent.parent / DENOMINATOR_DIRNAME / f"{DENOMINATOR_STEM}-{date}.json"


class DenominatorCheck:
    """The result of resolving and reading the independent denominator scan.

    One function, three callers: the CLI, the slate receipt, and — since the
    PR #77 review — the scheduled gate itself. Three copies of "find the scan,
    read it, cross-check it" would be three chances for the scheduled run to
    check something weaker than the command an operator types by hand, which
    is the exact asymmetry this lane exists to close.
    """

    __slots__ = ("path", "rows", "errors", "read_failed")

    def __init__(
        self,
        path: Path | None,
        rows: Any,
        errors: list[str],
        read_failed: bool,
    ) -> None:
        self.path = path
        self.rows = rows
        self.errors = errors
        self.read_failed = read_failed


def check_denominator(
    schedule_path: Path, schedule: Any, override: Path | None = None
) -> DenominatorCheck:
    """Locate the scan artifact for this schedule and cross-check against it.

    A MISSING scan is an error, never a skipped check: "nobody ran the scan"
    and "the scan agrees" must not share an outcome. ``override`` is the
    ``--denominator`` flag; ``read_failed`` lets the CLI keep treating an
    explicitly named unreadable file as a usage error.
    """
    errors: list[str] = []
    path = override
    if path is None:
        path = conventional_denominator_path(schedule_path, schedule)
        if path is None:
            errors.append(
                "cannot locate the denominator scan: the schedule carries no "
                "usable date and the filename does not supply one"
            )
            return DenominatorCheck(None, None, errors, False)
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(
            f"denominator scan not readable at {path}: {exc}; the day's size is "
            "unknown, so a zero read count cannot be called honest — run "
            "scripts/mlb_stage2_scan.py for this date"
        )
        return DenominatorCheck(path, None, errors, True)
    if isinstance(schedule, dict):
        errors.extend(scan_denominator_errors(schedule, rows))
    return DenominatorCheck(path, rows, errors, False)


def validate_with_denominator(
    schedule_path: Path, schedule: Any, policy: Any
) -> list[str]:
    """``validate_game_reads`` plus the scan cross-check and the policy rail.

    This is what a caller wants whenever it holds a schedule PATH. The bare
    ``validate_game_reads`` can only see a MISSING record; a run that trimmed
    ``game_reads`` and ``slate_denominator`` together is self-consistent and
    passes it. Only the scan, which the run did not write, can see that.

    ``policy`` is REQUIRED and has no default, deliberately. A default would be
    either a load (making this validator's answer depend on the machine it runs
    on, so the same schedule validates here and fails on the box) or a silent
    None (retiring the price rail for every caller that forgot it — an optional
    rail, which is the exact shape of defect this lane keeps paying for). Pass
    ``mlb_runtime_policy.load_mlb_selection_policy()``, or ``None`` to state
    that no policy was available and take the reported consequence.
    """
    errors = list(validate_game_reads(schedule))
    errors.extend(check_denominator(schedule_path, schedule).errors)
    errors.extend(policy_disposition_errors(schedule, policy))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the per-game refusal record on an MLB schedule."
    )
    parser.add_argument("schedule", type=Path)
    parser.add_argument(
        "--validate", action="store_true", help="validate game_reads (default action)"
    )
    parser.add_argument(
        "--denominator",
        type=Path,
        help="mlb_stage2_scan.py output to cross-check slate_denominator against",
    )
    args = parser.parse_args(argv)

    try:
        schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    errors = validate_game_reads(schedule)
    # The floor comes from the deployed policy, never from a constant here. A
    # missing policy is reported by ``policy_disposition_errors`` rather than
    # silently skipping the rail.
    errors.extend(
        policy_disposition_errors(schedule, mlb_runtime_policy.load_mlb_selection_policy())
    )
    # The scan cross-check is not opt-in. When --denominator is omitted the
    # conventional artifact is required, and its ABSENCE is an error rather
    # than a skipped check: "nobody ran the scan" and "the scan agrees" must
    # never produce the same exit code.
    check = check_denominator(args.schedule, schedule, args.denominator)
    if check.read_failed and args.denominator is not None:
        # A file the operator named by hand and that cannot be read is a usage
        # error, not a finding about the slate.
        parser.error(check.errors[0])
    errors = errors + check.errors
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
