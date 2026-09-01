#!/usr/bin/env python3
"""Read-only replay of the MLB gate over a window of slate documents.

Every corpus this repo has studied takes a PRICED CANDIDATE as its unit, and
over 2026-08-11..08-31 the runs produced two of them. So the question "would a
different rail have bet anything" has never had a population to ask of. The
inputs are not missing — they are written into the slate prose, one block per
game, and the gate arithmetic that would have judged them is a pure function of
four numbers. This module reads that prose, reconstructs the four numbers where
the prose supports it, runs the REAL gate arithmetic over them, and grades the
hypothetical selections against the cached finals.

What makes this trustworthy is entirely in what it refuses to do.

**A wrong parse is worse than a missing one.** A game this module cannot read is
visibly absent; a game it reads WRONG becomes a graded hypothetical selection
that looks exactly like a real one. So every field carries a provenance label,
every unparsed block is reported with its raw text retained, and per-day parse
coverage is printed beside every rate rather than under it. A rate over a
denominator nobody stated is the drought report's own collapsed-class defect.

**Nothing is imputed.** The four fields are sourced in a fixed order of
preference and the source is recorded:

- ``reconstructed`` — de-vigged from the two-sided American line by
  ``mlb_stage2_scan.devig``, the same function the live scan uses, with each
  side resolved to a club by name/nickname/abbreviation.
- ``recorded`` — stated verbatim in the prose and side-labelled.
- ``inferred_order`` — stated verbatim but NOT side-labelled ("fair 36.7% /
  63.3%"), so the away/home assignment rests on a writing convention rather than
  on the text. Carried, labelled, and excluded from the faithful totals.
- ``unavailable`` — with a reason and the raw line.

**The traded price was never recorded; only the ask was.** Two-sided ask sums in
this window run near 1.005, so the mid is a real quantity we do not have. Every
price here is an ASK and the arithmetic is run at the ask, which is the
conservative direction for a buyer: a replay run at an imagined mid would
manufacture edge that no order could have taken. The ask sum is reported per
game so the size of the unrecorded spread is visible.

**Recorded handicaps and the market-only fallback never blend.** The
``game_reads`` recorder shipped after this window closed, so a game's raw
probability exists only where the prose happened to state one. Everywhere else
the only reachable configuration is the market-only fallback — raw equals
``dk_fair_prob``, haircut zero, per ``mlb_probability_model``'s deployment gate
— under which the conservative edge is exactly ``dk_fair - ask``. Those two
populations answer different questions and are reported in separate columns with
separate totals. A blended rate would measure neither.

This module places no order, changes no gate, writes no file, and reads no live
state. Output is stdout.

Usage:
  python scripts/vig_slate_gate_replay.py \\
      --picks-dir ~/projects/sports-picks-runtime/.picks \\
      --also-picks-dir ~/projects/sports-picks-skill/.picks \\
      --since 2026-08-11 --until 2026-08-31
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_final_scores import final_scores  # noqa: E402
from mlb_runtime_policy import (  # noqa: E402
    DEFAULT_MAX_MLB_OFFICIAL_BETS_PER_DAY,
    DEFAULT_MIN_CONSERVATIVE_EDGE,
    MlbSelectionPolicy,
    enforce_daily_candidate_limit,
    live_conservative_edge,
)
from mlb_stage2_scan import devig  # noqa: E402
from vig_drought_diagnostic import (  # noqa: E402
    enumerate_day,
    is_mlb_lane,
    picks_roots,
    portable,
    scheduled_games,
)
from vig_historical_audit import TEAM_ABBREVIATIONS, team_token_match_kind  # noqa: E402

# The 30 official club names, derived from the repo's abbreviation table rather
# than listed again here. A second copy of a club list is how the side-selection
# lane already lost a review round; deriving it means a rename lands in one file.
CANONICAL_CLUBS = tuple(sorted(set(TEAM_ABBREVIATIONS.values())))


class ReplayError(Exception):
    """A caller mistake — a missing directory, an unusable date range."""


# Section headings whose body holds one block per game. The spelling drifted six
# ways across the window because each run wrote its own; the set is closed and
# exact rather than a substring test, so a NEW spelling is a visible zero in the
# per-document block count instead of prose silently vanishing from the corpus.
READ_SECTION_HEADINGS = (
    "Full-slate pass notes",
    "Full-slate game-by-game read",
    "Full-slate read",
    "Game-by-game read",
    "Full game-by-game scan",
    "Evening game-by-game read",
)

# Sections deliberately NOT read for per-game blocks, listed so the scope
# boundary is a stated fact rather than an omission a reader has to notice.
# "Sticks out, but pass" narrates a near-miss for a game the read section
# already covers, so parsing it would double-count the same game under a second
# provenance; the others carry no per-game price line at all.
EXCLUDED_SECTION_HEADINGS = (
    "Official card right now",
    "Lineup watchlist",
    "Sticks out, but pass",
    "Clean read",
)

# Where a number came from, in the order the resolver prefers. The label travels
# with the value everywhere, because a probability with no provenance cannot be
# excluded from a total later — and excluding the inferred ones from the
# faithful totals is the entire contract of this module.
PROVENANCE = ("reconstructed", "recorded", "inferred_order", "unavailable")
FAITHFUL_PROVENANCE = frozenset({"reconstructed", "recorded"})
# Preference order for a usable reading: a side-labelled source outranks one
# whose orientation rests on a writing convention, whichever pattern matched it.
FAIR_AGREEMENT_PREFERENCE = FAITHFUL_PROVENANCE

# The two populations, never summed together. `market_only` is the fallback the
# deployment gate forces when no model is deployed: raw == dk_fair, haircut 0.
POPULATIONS = ("recorded_handicap", "market_only")

# Agreement tolerance between a de-vigged line and a fair probability stated in
# the same block. The prose rounds to three or four decimals and sometimes to a
# tenth of a percent, so 0.005 is rounding slack; anything wider is a genuine
# disagreement between the two readings and the block is refused rather than
# resolved by preference. A cross-check that quietly picks a winner is not a
# cross-check.
FAIR_AGREEMENT_TOLERANCE = 0.005

# The gate's own comparison slack, copied from `mlb_execution_gate` so a replay
# verdict cannot sit on the wrong side of a float wobble the live gate forgives.
EDGE_COMPARISON_EPSILON = 1e-9

_ODDS = r"[+-]\d{3,4}"
_TOKEN = r"[A-Za-z][A-Za-z.À-ɏ ]{1,24}?"
_NUMBER = r"\d{1,3}(?:\.\d+)?%|0?\.\d+"
# The haircut alone accepts a bare integer, because ZERO is its most common legal
# value and `0` carries neither a decimal point nor a percent sign. The
# probability token deliberately does NOT: a bare integer there would be a
# percentage with a dropped sign as often as a fraction.
_HAIRCUT_NUMBER = r"\d{1,3}(?:\.\d+)?%?|0?\.\d+"

# `DK ATL +147 / MIL -158`, `DraftKings -109/+102`, `DK is +130 / -140`. The
# club tokens are optional because roughly a sixth of the lines omit them; a
# line without tokens yields `inferred_order`, never a silent away/home guess.
AMERICAN_LINE_RE = re.compile(
    rf"(?:DK|DraftKings)(?:\s+is)?\s*:?\s*"
    rf"(?:(?P<away_team>{_TOKEN})\s+)?(?P<away_odds>{_ODDS})\s*/\s*"
    rf"(?:(?P<home_team>{_TOKEN})\s+)?(?P<home_odds>{_ODDS})"
)

# `de-vig fair BOS 0.585 / MIA 0.415`, `de-vigged ATL 39.8% / MIL 60.2%`,
# `fair 36.7% / 63.3%`, `fair is 56.7% / 43.3%`.
STATED_FAIR_RE = re.compile(
    rf"(?:de-?vig(?:ged|s)?\s+)?fair(?:\s+is)?\s*"
    rf"(?:(?P<away_team>{_TOKEN})\s+)?(?P<away_prob>{_NUMBER})\s*/\s*"
    rf"(?:(?P<home_team>{_TOKEN})\s+)?(?P<home_prob>{_NUMBER})"
)

# `Polymarket asks BOS 0.585 / MIA 0.420`, `Polymarket US ask ATL 46.0% /
# MIL 54.5%`, `PM asks NYY 0.550/SEA 0.455`.
ASK_RE = re.compile(
    rf"(?:Polymarket(?:\s+US)?|PM)\s+ask(?:s)?\s*"
    rf"(?:(?P<away_team>{_TOKEN})\s+)?(?P<away_prob>{_NUMBER})\s*/\s*"
    rf"(?:(?P<home_team>{_TOKEN})\s+)?(?P<home_prob>{_NUMBER})"
)

# `win probability NYY 0.600`, `provisional win probability 0.510`. One side
# only: the runs state the side they liked, not both.
HANDICAP_RE = re.compile(
    rf"(?:provisional\s+)?win probability\s+(?:(?P<team>{_TOKEN})\s+)?(?P<prob>{_NUMBER})"
)

# `0.020 uncertainty haircut`, `uncertainty haircut of 0.02`.
HAIRCUT_RE = re.compile(
    rf"(?:(?P<pre>{_HAIRCUT_NUMBER})\s+uncertainty haircut"
    rf"|uncertainty haircut\s+(?:of\s+)?(?P<post>{_HAIRCUT_NUMBER}))"
)

# `### Boston Red Sox at Miami Marlins — 5:40 PM CT`
SUBSECTION_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$")
# `- **Seattle Mariners at New York Yankees (6:05 PM CT):** ...`
BULLET_RE = re.compile(r"^-\s+\*\*(?P<title>[^*]+?)\*\*\s*:?\s*(?P<body>.*)$")
HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
MATCHUP_RE = re.compile(r"^(?P<away>.+?)\s+at\s+(?P<home>.+)$")


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ReplayError(f"not a date: {value!r}") from exc


def date_range(since: dt.date, until: dt.date) -> list[dt.date]:
    if until < since:
        raise ReplayError(f"--until {until} precedes --since {since}")
    return [since + dt.timedelta(days=n) for n in range((until - since).days + 1)]


def parse_probability(text: str | None) -> float | None:
    """Read one probability token, refusing anything that needs a guess.

    ``58.5%`` is a percentage and ``0.585`` is a fraction. A bare number above 1
    is NOT silently divided by a hundred: ``fair 58.5`` could be a percentage
    with a dropped sign or a typo, and choosing for it is exactly the imputation
    this module exists to avoid. Returns None, and the caller records the raw
    text with a reason.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    token = text.strip()
    if token.endswith("%"):
        try:
            value = float(token[:-1]) / 100.0
        except ValueError:
            return None
    else:
        try:
            value = float(token)
        except ValueError:
            return None
    return value if 0.0 < value < 1.0 else None


def resolve_side(token: str | None, away: str, home: str) -> str | None:
    """Map a club token to ``"away"`` or ``"home"``, or None.

    Resolution is against the two clubs the block's own title names, via
    ``vig_historical_audit.team_token_match_kind`` — the repo's existing
    resolver, so an abbreviation table does not get restated here and drift.

    A token matching BOTH sides resolves to neither. That is not a hypothetical:
    a title can name two clubs sharing a city, and a token that could be either
    is ambiguous evidence, which is a refusal. Returning a preference would put
    the probability on a coin flip and the row would still be counted.
    """
    if not isinstance(token, str) or not token.strip():
        return None
    text = token.strip()
    matches_away = team_token_match_kind(text, away) is not None
    matches_home = team_token_match_kind(text, home) is not None
    if matches_away and not matches_home:
        return "away"
    if matches_home and not matches_away:
        return "home"
    return None


def _oriented_pair(
    match: re.Match[str] | None,
    away: str,
    home: str,
    convert,
) -> tuple[dict[str, float] | None, str, str | None]:
    """Turn one matched two-sided pair into ``{away, home}`` plus a provenance.

    Returns ``(values, provenance, reason)``. The orientation question is the
    whole function: PR #74 paid a review round for a row that joined a read to a
    final on the game id and never compared the two side labels, and this is the
    same join one level down. So when the pair carries club tokens the sides are
    resolved from the TOKENS and the written order is ignored; the written order
    is used only when there are no tokens at all, and that case is labelled
    ``inferred_order`` so it can be excluded from the faithful totals rather
    than passing as read text.

    Tokens that are present but do not resolve, or that resolve to the same
    side, are a refusal — a pair whose two halves both claim the away club is a
    transposition or a typo, and either way the orientation is unknown.
    """
    if match is None:
        return None, "unavailable", "no matching price phrase in the block"
    first = convert(match.group("away_prob"))
    second = convert(match.group("home_prob"))
    if first is None or second is None:
        return None, "unavailable", "probability token outside (0, 1) or unreadable"
    away_token = match.groupdict().get("away_team")
    home_token = match.groupdict().get("home_team")
    if away_token is None and home_token is None:
        return {"away": first, "home": second}, "inferred_order", None
    away_side = resolve_side(away_token, away, home)
    home_side = resolve_side(home_token, away, home)
    if away_side is None or home_side is None:
        return None, "unavailable", (
            f"club token did not resolve to either side ({away_token!r}, {home_token!r})"
        )
    if away_side == home_side:
        return None, "unavailable", (
            f"both club tokens resolved to the {away_side} side ({away_token!r}, {home_token!r})"
        )
    values = {away_side: first, home_side: second}
    return {"away": values["away"], "home": values["home"]}, "recorded", None


def parse_american_line(body: str, away: str, home: str) -> dict[str, Any]:
    """De-vig the two-sided American line into a fair probability per side.

    The de-vig itself is ``mlb_stage2_scan.devig``, imported rather than
    reimplemented: this replay's whole claim is that it runs the arithmetic the
    pipeline runs, and a second copy of a formula is how two answers to one
    question start disagreeing.
    """
    match = AMERICAN_LINE_RE.search(body)
    if match is None:
        return {"values": None, "provenance": "unavailable", "reason": "no DK American line"}
    away_odds, home_odds = match.group("away_odds"), match.group("home_odds")
    away_token, home_token = match.group("away_team"), match.group("home_team")
    if away_token is None and home_token is None:
        orientation, provenance = ("away", "home"), "inferred_order"
    else:
        away_side = resolve_side(away_token, away, home)
        home_side = resolve_side(home_token, away, home)
        if away_side is None or home_side is None or away_side == home_side:
            return {
                "values": None,
                "provenance": "unavailable",
                "reason": (
                    "American line club token did not resolve to a distinct side "
                    f"({away_token!r}, {home_token!r})"
                ),
                "raw": match.group(0),
            }
        orientation, provenance = (away_side, home_side), "reconstructed"
    first, second = devig(away_odds, home_odds)
    if first is None or second is None:
        return {
            "values": None,
            "provenance": "unavailable",
            "reason": "American odds did not de-vig",
            "raw": match.group(0),
        }
    values = {orientation[0]: first, orientation[1]: second}
    return {
        "values": {"away": values["away"], "home": values["home"]},
        "provenance": provenance,
        "reason": None,
        "raw": match.group(0),
        "american": {"away": away_odds, "home": home_odds},
    }


def resolve_dk_fair(body: str, away: str, home: str) -> dict[str, Any]:
    """Pick the de-vigged fair probability per side, and cross-check it.

    Two readings of the same fact are available in most blocks — the American
    line, and a fair probability the run already wrote out — and they are used
    against each other rather than in preference order alone. When both exist
    and disagree by more than rounding, the block is refused: a disagreement
    means one of the two patterns matched something it should not have, and
    resolving it by preferring the reconstruction would hide the parser's own
    error behind the more authoritative-looking source. This is the only
    measurement in the module that can catch a systematically wrong pattern, so
    its agreement rate is reported as a first-class number.
    """
    reconstructed = parse_american_line(body, away, home)
    stated_values, stated_provenance, stated_reason = _oriented_pair(
        STATED_FAIR_RE.search(body), away, home, parse_probability
    )
    stated_match = STATED_FAIR_RE.search(body)
    result: dict[str, Any] = {
        "reconstructed": reconstructed,
        "stated": {
            "values": stated_values,
            "provenance": stated_provenance,
            "reason": stated_reason,
            "raw": stated_match.group(0) if stated_match else None,
        },
        "cross_check": "not_comparable",
    }
    if reconstructed["values"] and stated_values:
        deltas = [abs(reconstructed["values"][s] - stated_values[s]) for s in ("away", "home")]
        if max(deltas) <= FAIR_AGREEMENT_TOLERANCE:
            result["cross_check"] = "agree"
        else:
            result["cross_check"] = "disagree"
            result["values"] = None
            result["provenance"] = "unavailable"
            result["reason"] = (
                "de-vigged line and stated fair disagree by "
                f"{max(deltas):.4f} (> {FAIR_AGREEMENT_TOLERANCE})"
            )
            return result
    for source in ("reconstructed", "stated"):
        block = result[source] if source == "stated" else reconstructed
        values = block["values"]
        if values and block["provenance"] in FAIR_AGREEMENT_PREFERENCE:
            result["values"] = values
            result["provenance"] = block["provenance"]
            result["reason"] = None
            return result
    for source in ("reconstructed", "stated"):
        block = result[source] if source == "stated" else reconstructed
        if block["values"]:
            result["values"] = block["values"]
            result["provenance"] = block["provenance"]
            result["reason"] = None
            return result
    result["values"] = None
    result["provenance"] = "unavailable"
    result["reason"] = reconstructed.get("reason") or stated_reason or "no fair probability"
    return result


def resolve_ask(body: str, away: str, home: str) -> dict[str, Any]:
    """Polymarket ASK per side. Never a mid, never a traded price.

    No quote receipt exists for any game in this window — the three in
    ``receipts/polymarket`` all belong to the last executed pick, on 2026-08-10 —
    so the ask is the only price that was ever recorded and the arithmetic runs
    at it. The two-sided sum is carried alongside because it is the only visible
    measure of the spread we do not have: a sum of 1.005 says the unrecorded mid
    sits about a quarter-point inside each ask, and a reader who wants to know
    how much of a refusal was the spread can read it off this number instead of
    being handed an average nobody recorded.
    """
    match = ASK_RE.search(body)
    values, provenance, reason = _oriented_pair(match, away, home, parse_probability)
    result: dict[str, Any] = {
        "values": values,
        "provenance": provenance,
        "reason": reason,
        "raw": match.group(0) if match else None,
        "two_sided_sum": None,
    }
    if values:
        result["two_sided_sum"] = round(values["away"] + values["home"], 6)
    return result


def resolve_handicap(body: str, away: str, home: str) -> dict[str, Any]:
    """The run's own win probability for one side, where it stated one.

    This is the field that decides which population a game lands in, so it is
    read strictly. A stated probability with no club token is NOT accepted: the
    two-sided patterns can fall back on away/home order because the writing
    convention orders them, but a single number has no order to fall back on and
    guessing its side would put a handicap on the wrong club half the time —
    and a wrongly-sided handicap is precisely a counted, anti-correlated row.
    """
    match = HANDICAP_RE.search(body)
    if match is None:
        return {"side": None, "value": None, "provenance": "unavailable",
                "reason": "no stated win probability", "raw": None}
    value = parse_probability(match.group("prob"))
    if value is None:
        return {"side": None, "value": None, "provenance": "unavailable",
                "reason": "win probability token outside (0, 1)", "raw": match.group(0)}
    side = resolve_side(match.group("team"), away, home)
    if side is None:
        return {"side": None, "value": None, "provenance": "unavailable",
                "reason": "stated win probability carries no resolvable side",
                "raw": match.group(0)}
    return {"side": side, "value": value, "provenance": "recorded",
            "reason": None, "raw": match.group(0)}


def resolve_haircut(body: str) -> dict[str, Any]:
    """The uncertainty haircut, when the prose stated one.

    Checked as a non-negative number and never with the probability rule. Zero
    is legal and is the market-only fallback's own contract value, so a
    ``0 < x < 1`` test would make the field unreadable in exactly the
    configuration that dominates this window. That mistake cost a review round
    in PR #74 and is not repeated here.
    """
    match = HAIRCUT_RE.search(body)
    if match is None:
        return {"value": None, "provenance": "unavailable",
                "reason": "no stated uncertainty haircut", "raw": None}
    token = match.group("pre") or match.group("post")
    text = token.strip()
    try:
        value = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
    except ValueError:
        return {"value": None, "provenance": "unavailable",
                "reason": "haircut token unreadable", "raw": match.group(0)}
    if value < 0 or value >= 1:
        return {"value": None, "provenance": "unavailable",
                "reason": "haircut outside [0, 1)", "raw": match.group(0)}
    return {"value": value, "provenance": "recorded", "reason": None, "raw": match.group(0)}


def gate_edge(conservative: float, ask: float) -> float | None:
    """The gate's edge, computed by the gate's own function.

    ``mlb_runtime_policy.live_conservative_edge`` is called on a candidate-shaped
    dict rather than subtracting here, so this replay cannot report an edge the
    live gate would not compute — including its refusal of a probability outside
    (0, 1), which is why the return is Optional.
    """
    return live_conservative_edge(
        {"conservative_probability": conservative, "current_ask": ask}
    )


def replay_sides(
    dk_fair: dict[str, Any],
    ask: dict[str, Any],
    handicap: dict[str, Any],
    haircut: dict[str, Any],
    edge_floor: float,
) -> dict[str, Any]:
    """Run the gate arithmetic for both sides of one game, in both populations.

    ``market_only`` is the configuration the deployment gate forces when no
    model version is deployed: ``raw == dk_fair`` and ``uncertainty_haircut ==
    0``, so the conservative probability IS the market fair and the edge is
    ``dk_fair - ask``. It is computed for every game with both prices, because
    that is what the gate would actually have done all window.

    ``recorded_handicap`` is computed only where the prose stated both a win
    probability and a haircut for the same side. A stated handicap with no
    stated haircut is NOT completed with a zero: zero is the fallback's value,
    and borrowing it would silently relabel a handicapped game as a market-only
    one and inflate the very population this split exists to isolate.
    """
    out: dict[str, Any] = {"market_only": {}, "recorded_handicap": {}}
    fair_values, ask_values = dk_fair.get("values"), ask.get("values")
    for side in ("away", "home"):
        if not fair_values or not ask_values:
            out["market_only"][side] = {
                "evaluable": False,
                "reason": "dk_fair" if not fair_values else "polymarket_ask",
            }
            continue
        conservative = fair_values[side]
        side_ask = ask_values[side]
        edge = gate_edge(conservative, side_ask)
        out["market_only"][side] = _verdict(
            raw=conservative,
            haircut=0.0,
            conservative=conservative,
            ask=side_ask,
            edge=edge,
            edge_floor=edge_floor,
        )
    side = handicap.get("side")
    if side is None:
        out["recorded_handicap"] = {"evaluable": False, "reason": "no recorded handicap"}
    elif haircut.get("value") is None:
        out["recorded_handicap"] = {
            "evaluable": False,
            "reason": "recorded handicap without a recorded uncertainty haircut",
            "side": side,
        }
    elif not ask_values:
        out["recorded_handicap"] = {"evaluable": False, "reason": "polymarket_ask", "side": side}
    else:
        raw = handicap["value"]
        cut = haircut["value"]
        conservative = round(raw - cut, 10)
        side_ask = ask_values[side]
        verdict = _verdict(
            raw=raw,
            haircut=cut,
            conservative=conservative,
            ask=side_ask,
            edge=gate_edge(conservative, side_ask),
            edge_floor=edge_floor,
        )
        verdict["side"] = side
        out["recorded_handicap"] = verdict
    return out


def _verdict(
    raw: float, haircut: float, conservative: float, ask: float,
    edge: float | None, edge_floor: float,
) -> dict[str, Any]:
    """One side's gate result, with the arithmetic spelled out.

    ``stop_reason`` is a sentence of numbers, not a category. "No edge" is the
    finding the drought report already has; what nobody could check was BY HOW
    MUCH, and a shortfall of 0.002 and one of 0.06 are different facts about
    whether a floor is the binding constraint.
    """
    if edge is None:
        return {
            "evaluable": False,
            "reason": "gate refused the probability pair",
            "raw_probability": raw,
            "conservative_probability": conservative,
            "current_ask": ask,
        }
    cleared = edge + EDGE_COMPARISON_EPSILON >= edge_floor
    shortfall = None if cleared else round(edge_floor - edge, 6)
    return {
        "evaluable": True,
        "raw_probability": round(raw, 6),
        "uncertainty_haircut": round(haircut, 6),
        "conservative_probability": round(conservative, 6),
        "current_ask": round(ask, 6),
        "conservative_edge": round(edge, 6),
        "edge_floor": edge_floor,
        "cleared": cleared,
        "shortfall": shortfall,
        "stop_reason": (
            f"conservative {conservative:.4f} - ask {ask:.4f} = {edge:+.4f}"
            + (
                f", clears the {edge_floor:.4f} floor"
                if cleared
                else f", short of the {edge_floor:.4f} floor by {shortfall:.4f}"
            )
        ),
    }


def select_hypothetical(sides: dict[str, Any]) -> dict[str, Any] | None:
    """The one side a population would have taken, or None.

    Best evaluable edge wins and it must clear the floor. A tie between the two
    sides of one game is impossible in practice (it needs the asks to sum to
    exactly the fair pair) but is refused rather than broken by key order: PR
    #63 shipped a rule "chosen" by name-order tiebreak and the artifact narrated
    a selection that never happened.
    """
    candidates = [
        (info["conservative_edge"], side, info)
        for side, info in sides.items()
        if isinstance(info, dict) and info.get("evaluable") and info.get("cleared")
    ]
    if not candidates:
        return None
    best = max(edge for edge, _, _ in candidates)
    tied = [item for item in candidates if item[0] == best]
    if len(tied) > 1:
        return {"tie": True, "sides": sorted(side for _, side, _ in tied), "edge": best}
    _, side, info = tied[0]
    return {"tie": False, "side": side, **info}


def grade(selection: dict[str, Any] | None, outcome: dict[str, Any] | None) -> dict[str, Any]:
    """Grade one hypothetical selection against the cached final.

    Synthetic units, at the ask: a win returns ``1/ask - 1`` and a loss returns
    ``-1``. Nothing is coerced — an unfinished game or a missing final grades as
    ``unreconciled`` with no units, because "no evidence" and "broke even" are
    different facts and summing the first as zero is how a replay reports a
    result it never observed.
    """
    if selection is None or selection.get("tie"):
        return {"graded": False, "reason": "no hypothetical selection"}
    if outcome is None:
        return {"graded": False, "reason": "no cached final for this matchup"}
    winner_side = outcome.get("winner_side")
    if winner_side not in ("away", "home"):
        return {"graded": False, "reason": f"no decided winner (status {outcome.get('status')!r})"}
    ask = selection["current_ask"]
    won = winner_side == selection["side"]
    return {
        "graded": True,
        "won": won,
        "units": round((1.0 / ask) - 1.0, 6) if won else -1.0,
        "winner_side": winner_side,
    }


def canonical_club(token: str) -> str | None:
    """Resolve one club token to its official name, or None.

    The window's titles name a club four ways — full name, nickname ("Astros"),
    abbreviation, and one run's "Athletics Athletics" — and the cached finals use
    the official name only. Resolving here means a matchup key is one string per
    game instead of one per spelling, which is what the first run of this module
    got wrong: three spellings of Red Sox at Yankees counted as three games and
    pushed 2026-08-29 to 112% of its own schedule.

    Exactly one club must match. A token matching none is a spelling nothing in
    the table covers, and a token matching several is ambiguous; both refuse. The
    resolution runs against every club rather than against the block's own pair,
    because at title level there is no pair yet — that is what is being read.
    """
    if not isinstance(token, str) or not token.strip():
        return None
    text = token.strip()
    matches = [club for club in CANONICAL_CLUBS if team_token_match_kind(text, club)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    # City-only titles ("Atlanta at Milwaukee") are a whole day's worth of blocks
    # on 2026-08-21 and the shared resolver does not cover them: it matches a
    # name, a nickname SUFFIX, or an abbreviation, and a city is a PREFIX. The
    # widening is applied here and not pushed into the shared resolver, because
    # in-body price tokens are abbreviations and giving that lookup a new way to
    # succeed is a new way for it to succeed wrongly.
    #
    # Ambiguity still refuses, which is the point: "Chicago", "New York" and
    # "Los Angeles" each prefix two clubs, and every title in this window that
    # uses one of those cities spells the club out.
    lowered = text.casefold() + " "
    prefixed = [club for club in CANONICAL_CLUBS if club.casefold().startswith(lowered)]
    if len(prefixed) == 1:
        return prefixed[0]
    if prefixed:
        return None
    # "Athletics Athletics" — one club has no city in its official name, and a
    # run templating "{city} {nickname}" doubled the nickname instead. Collapsing
    # an exactly-repeated token sequence recovers four blocks whose cause is
    # fully understood; it is retried through the resolver rather than special-
    # cased to a club, so it can only ever produce a name the table already has.
    halves = text.split()
    if len(halves) % 2 == 0:
        half = len(halves) // 2
        if halves[:half] == halves[half:]:
            return canonical_club(" ".join(halves[:half]))
    return None


def looks_like_matchup(title: str) -> bool:
    """Shape test for "is this line the start of a game block".

    Deliberately looser than ``split_matchup``: extraction must not depend on
    resolution. The first version used the resolver here, so a title naming a
    club the table could not resolve stopped being a block boundary at all and
    its text was absorbed into the PREVIOUS game's body — turning one unreadable
    title into two corrupted games. Finding a block and reading it are separate
    jobs and separate failure reports.
    """
    return bool(re.search(r"\s+at\s+", title))


def split_matchup(title: str) -> dict[str, Any] | None:
    """Split a block title into canonical away/home clubs, or None.

    Titles carry a dash-suffix ("— 5:40 PM CT", "— pass"), a parenthesised time,
    a trailing colon from the bullet shape, and a comma-suffix that is sometimes
    a start time and sometimes ``DH1``/``DH2``. Every one is stripped, IN THAT
    ORDER — the first version stripped the parenthetical before the colon, so a
    bullet title kept "(6:05 PM CT):" on the home club and every side lookup in
    the block failed. The stripped suffix is RETAINED rather than discarded,
    because a doubleheader marker is the one piece of it that decides whether a
    later grade is even possible.
    """
    text = title.strip()
    for dash in ("—", "–"):
        if dash in text:
            text = text.split(dash, 1)[0]
    text = text.strip().rstrip(":").strip()
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip().rstrip(":").strip()
    suffix = None
    if "," in text:
        text, _, suffix = (part.strip() for part in text.partition(","))
    parts = re.split(r"\s+at\s+", text)
    if len(parts) != 2:
        return None
    # A doubleheader marker sits on either side of the comma depending on the
    # run ("Giants DH1, 20:05Z" and "at Giants, DH2" both occur), so it is
    # stripped from the club token as well as read from the suffix. Losing it
    # here would not just mislabel the game — it would leave "Giants DH1" as the
    # club token, which resolves to nothing and drops the block entirely.
    marker = None
    cleaned = []
    for part in parts:
        found = re.search(r"\bDH\s?\d\b", part, re.I)
        if found:
            marker = found.group(0)
            part = part[: found.start()] + part[found.end():]
        cleaned.append(part.strip())
    parts = cleaned
    away, home = canonical_club(parts[0]), canonical_club(parts[1])
    if away is None or home is None:
        return None
    if marker is None and suffix:
        found = re.search(r"\bDH\s?\d\b", suffix, re.I)
        marker = found.group(0) if found else None
    return {
        "away": away,
        "home": home,
        "suffix": suffix or None,
        "doubleheader_marker": marker,
    }


def extract_blocks(text: str) -> dict[str, Any]:
    """Pull every per-game block out of one slate document.

    Both shapes the window used are read — ``### Away at Home`` subsections and
    ``- **Away at Home (time):**`` bullets — and only inside the closed set of
    read-section headings. Sections are counted whether or not they yielded
    blocks, and headings outside both the read set and the excluded set are
    reported as ``unknown_sections``: a seventh spelling of "game-by-game read"
    would otherwise cost the corpus a whole document in silence, which is the
    failure mode this window has already produced twice.
    """
    blocks: list[dict[str, Any]] = []
    sections: list[str] = []
    unknown: list[str] = []
    current: str | None = None
    pending: dict[str, Any] | None = None
    for line in text.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            if pending:
                blocks.append(pending)
                pending = None
            title = heading.group("title")
            base = re.split(r"\s+[—–]\s+", title)[0].strip()
            if base in READ_SECTION_HEADINGS:
                current = base
                sections.append(base)
            else:
                current = None
                if base not in EXCLUDED_SECTION_HEADINGS:
                    unknown.append(base)
            continue
        if current is None:
            continue
        sub = SUBSECTION_RE.match(line)
        if sub:
            if pending:
                blocks.append(pending)
            pending = {"section": current, "title": sub.group("title"), "lines": []}
            continue
        bullet = BULLET_RE.match(line)
        if bullet and looks_like_matchup(bullet.group("title")):
            if pending:
                blocks.append(pending)
            pending = {
                "section": current,
                "title": bullet.group("title"),
                "lines": [bullet.group("body")],
            }
            continue
        if pending is not None:
            pending["lines"].append(line)
    if pending:
        blocks.append(pending)
    return {
        "blocks": [
            {"section": b["section"], "title": b["title"], "body": "\n".join(b["lines"]).strip()}
            for b in blocks
        ],
        "sections": sections,
        "unknown_sections": sorted(set(unknown)),
    }


def replay_block(block: dict[str, Any], edge_floor: float) -> dict[str, Any]:
    """Reconstruct, replay, and label one game block.

    Everything the block could not yield is reported with a reason and the raw
    text is retained on the record, so an ``unavailable`` field can be checked
    against the prose that produced it without going back to the document.
    """
    matchup = split_matchup(block["title"])
    if matchup is None:
        return {
            "section": block["section"],
            "title": block["title"],
            "parsed": False,
            "reason": (
                "block title is not an 'Away at Home' matchup of two resolvable clubs"
            ),
            "raw": block["body"],
        }
    away, home = matchup["away"], matchup["home"]
    body = block["body"]
    dk_fair = resolve_dk_fair(body, away, home)
    ask = resolve_ask(body, away, home)
    handicap = resolve_handicap(body, away, home)
    haircut = resolve_haircut(body)
    record: dict[str, Any] = {
        "section": block["section"],
        "title": block["title"],
        "away_team": away,
        "home_team": home,
        "matchup": f"{away} at {home}",
        "title_suffix": matchup["suffix"],
        "doubleheader_marker": matchup["doubleheader_marker"],
        "parsed": True,
        "inputs": {
            "dk_fair_prob": {
                "values": dk_fair.get("values"),
                "provenance": dk_fair.get("provenance", "unavailable"),
                "reason": dk_fair.get("reason"),
                "cross_check": dk_fair["cross_check"],
                "american": dk_fair["reconstructed"].get("american"),
                "raw": dk_fair["reconstructed"].get("raw") or dk_fair["stated"].get("raw"),
            },
            "polymarket_ask": ask,
            "raw_probability": handicap,
            "uncertainty_haircut": haircut,
        },
    }
    faithful = (
        record["inputs"]["dk_fair_prob"]["provenance"] in FAITHFUL_PROVENANCE
        and ask["provenance"] in FAITHFUL_PROVENANCE
    )
    record["faithful_inputs"] = faithful
    record["replay"] = replay_sides(dk_fair, ask, handicap, haircut, edge_floor)
    if not dk_fair.get("values") or not ask.get("values"):
        record["raw"] = body
    return record


def replay_policy(edge_floor: float, max_bets: int) -> MlbSelectionPolicy:
    """The rails this replay runs against, constructed rather than loaded.

    The live ``risk_limits.json`` is deliberately NOT read. A rail's value today
    is not necessarily the rail that was in force during the window, and a
    report whose meaning changes when someone edits live state is not a
    reproducible artifact — the same reasoning ``vig_historical_audit`` states
    for reading the floor as a constant. The values used are recorded in the
    report so a rerun under different rails is a visibly different run.
    """
    return MlbSelectionPolicy(
        min_conservative_edge=edge_floor,
        max_mlb_official_bets_per_day=max_bets,
        starter_pending_promotions_enabled=False,
        max_small_bets_per_day_probation=0,
        policy_version="replay-constructed",
        effective_at="n/a",
    )


def apply_daily_cap(
    selections: list[dict[str, Any]], policy: MlbSelectionPolicy
) -> dict[str, Any]:
    """Rank one card's clearing selections and keep only what the cap allows.

    Clearing the edge floor is not the same as being bet: the gate also caps the
    day at ``max_mlb_official_bets_per_day``, and on 2026-08-22 this replay finds
    five games clearing the floor on one card. Reporting five would describe a
    day the gate could not have had. ``enforce_daily_candidate_limit`` does the
    ranking, imported rather than reimplemented, so the replay's cap is the
    gate's cap including its edge-descending order and its stable tie handling.
    """
    candidates = [
        {
            "conservative_probability": s["conservative_probability"],
            "current_ask": s["current_ask"],
            "_selection": s,
        }
        for s in selections
    ]
    kept, dropped = enforce_daily_candidate_limit(candidates, policy)
    return {
        "kept": [c["_selection"] for c in kept],
        "dropped": [c["_selection"] for c in dropped],
        "cap": policy.max_mlb_official_bets_per_day,
    }


def _document_records(
    roots: list[dict[str, Any]], day: dt.date, listing: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Every MLB-lane slate document for one date, in EVERY root, kept separate.

    Two roots holding a byte-identical document is a duplicate; two roots holding
    DIFFERENT documents for the same date is a conflict, and 2026-08-22 is one —
    different sections, different game coverage. Silently preferring the primary
    root would drop a document that a run genuinely wrote, so both are carried as
    separate source records and the conflict is named in the report. The content
    digest is what tells the two cases apart; a path comparison cannot.
    """
    docs: list[dict[str, Any]] = []
    for root in roots:
        for rel in listing[root["label"]]:
            if not rel.startswith("slate/") or not rel.endswith(".md"):
                continue
            if not is_mlb_lane(rel):
                continue
            path = root["_path"] / rel
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                docs.append({
                    "root": root["label"], "relative_path": rel, "path": portable(path),
                    "readable": False, "reason": str(exc),
                })
                continue
            docs.append({
                "root": root["label"],
                "relative_path": rel,
                "path": portable(path),
                "readable": True,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
            })
    digests = Counter(d["sha256"] for d in docs if d.get("readable"))
    by_name: dict[str, set[str]] = {}
    for doc in docs:
        if doc.get("readable"):
            by_name.setdefault(doc["relative_path"], set()).add(doc["sha256"])
    for doc in docs:
        if not doc.get("readable"):
            continue
        same_name = by_name[doc["relative_path"]]
        doc["duplicate_of_other_root"] = digests[doc["sha256"]] > 1
        doc["conflicting_copies"] = len(same_name) > 1
    return docs


def _population_selection(game: dict[str, Any], population: str) -> dict[str, Any] | None:
    """The side one population would have taken for one game, or None."""
    if not game.get("parsed"):
        return None
    if population == "market_only":
        return game.get("market_only_selection")
    replay = game["replay"]["recorded_handicap"]
    if not replay.get("evaluable") or not replay.get("cleared"):
        return None
    return {"tie": False, **replay}


def _mark_within_cap(games: list[dict[str, Any]], policy: MlbSelectionPolicy) -> None:
    """Flag, per population, which of a card's clearing games the cap kept.

    The cap is applied per DOCUMENT, because a document is one run's card and
    the cap bounds one card. Applying it across a day's two documents would
    charge the morning and evening runs a single shared allowance they never
    shared, and applying nothing at all would let the replay report five bets on
    a day whose ceiling is two.
    """
    for population in POPULATIONS:
        clearing = []
        for game in games:
            selection = _population_selection(game, population)
            game.setdefault("within_daily_cap", {})[population] = False
            if selection and not selection.get("tie"):
                clearing.append({"game": game, "selection": selection})
        result = apply_daily_cap([c["selection"] for c in clearing], policy)
        kept = {id(s) for s in result["kept"]}
        for entry in clearing:
            entry["game"]["within_daily_cap"][population] = id(entry["selection"]) in kept


def analyze_day(
    roots: list[dict[str, Any]],
    day: dt.date,
    results_dir: Path,
    edge_floor: float,
    policy: MlbSelectionPolicy,
) -> dict[str, Any]:
    """Replay one calendar day across every document that day produced."""
    listing = enumerate_day(roots, day)
    docs = _document_records(roots, day, listing)
    schedule_path = results_dir / f"{day.isoformat()}.json"
    try:
        scheduled, _ = scheduled_games(results_dir, day)
        payload = (
            json.loads(schedule_path.read_text(encoding="utf-8"))
            if schedule_path.exists() else None
        )
        finals_status = "cached" if scheduled is not None else "absent"
    except json.JSONDecodeError as exc:
        scheduled, payload, finals_status = None, None, f"corrupt: {exc}"
    index = outcome_index(payload)

    documents: list[dict[str, Any]] = []
    for doc in docs:
        if not doc.get("readable"):
            documents.append({k: v for k, v in doc.items() if k != "text"})
            continue
        extracted = extract_blocks(doc["text"])
        games = []
        for block in extracted["blocks"]:
            record = replay_block(block, edge_floor)
            record["outcome"] = _join_outcome(record, index)
            record["market_only_selection"] = select_hypothetical(
                record.get("replay", {}).get("market_only", {})
            ) if record.get("parsed") else None
            record["market_only_grade"] = grade(
                record["market_only_selection"], record["outcome"].get("record")
            )
            games.append(record)
        _mark_within_cap(games, policy)
        parsed = [g for g in games if g.get("parsed")]
        documents.append({
            "root": doc["root"],
            "relative_path": doc["relative_path"],
            "path": doc["path"],
            "sha256": doc["sha256"],
            "duplicate_of_other_root": doc["duplicate_of_other_root"],
            "conflicting_copies": doc["conflicting_copies"],
            "readable": True,
            "read_sections": extracted["sections"],
            "unknown_sections": extracted["unknown_sections"],
            "block_count": len(games),
            "parsed_count": len(parsed),
            "faithful_count": sum(1 for g in parsed if g["faithful_inputs"]),
            "games": games,
        })

    replayable = [d for d in documents if d.get("readable")]
    return {
        "date": day.isoformat(),
        "files_present": listing,
        "empty_in_every_root": not any(listing.values()),
        "scheduled_games": scheduled,
        "finals": finals_status,
        "documents": documents,
        "document_count": len(replayable),
        "block_count": sum(d["block_count"] for d in replayable),
        "parsed_count": sum(d["parsed_count"] for d in replayable),
        "faithful_count": sum(d["faithful_count"] for d in replayable),
        "distinct_matchups": sorted({
            g["matchup"] for d in replayable for g in d["games"] if g.get("parsed")
        }),
    }


def outcome_index(payload: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Index the cached finals by matchup, keeping DOUBLEHEADERS as a list.

    Rows come from ``mlb_final_scores.final_scores`` — the function the
    settlement path itself uses — so a replay grade and a real settlement read
    the same source. The value is a LIST because two games in one day can share
    a matchup, and a dict keyed on the matchup silently keeps only the last one:
    a doubleheader would then grade one game's hypothetical selection against
    the OTHER game's result, producing a clean, counted, wrong row. The prose
    titles do carry ``DH1``/``DH2`` on some days and nothing on others, so the
    marker cannot be relied on to disambiguate; refusing the join when the key
    is not unique is the only answer that is right on every day.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(payload, dict):
        return index
    for row in final_scores(payload):
        away, home = row.get("away"), row.get("home")
        if not away or not home:
            continue
        index.setdefault(f"{away} at {home}", []).append(row)
    return index


def _join_outcome(
    record: dict[str, Any], index: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    """Join a parsed block to its cached final, refusing every ambiguous match.

    Three refusals, and each of them would otherwise produce a graded row rather
    than a visible gap:

    - the key is not unique (a doubleheader), so no single result belongs to it;
    - the key misses but its TRANSPOSE hits, which means the prose and the API
      disagree about which club is home. Detection, not correction — flipping it
      would assume the API is right and the prose wrong about a game we then
      grade. PR #74 paid a review round for the version of this join that
      compared ids and never compared sides;
    - the key is simply absent, which on this window usually means the game had
      not gone final when the schedule was cached.
    """
    if not record.get("parsed"):
        return {"joined": False, "reason": "block not parsed"}
    key = f"{record['away_team']} at {record['home_team']}"
    rows = index.get(key, [])
    if len(rows) > 1:
        return {
            "joined": False,
            "key": key,
            "reason": f"{len(rows)} cached finals share this matchup (doubleheader)",
        }
    if len(rows) == 1:
        row = rows[0]
        winner_side = None
        if row.get("winner") == row.get("away"):
            winner_side = "away"
        elif row.get("winner") == row.get("home"):
            winner_side = "home"
        return {"joined": True, "key": key, "record": {**row, "winner_side": winner_side}}
    swapped = f"{record['home_team']} at {record['away_team']}"
    if swapped in index:
        return {
            "joined": False,
            "key": key,
            "reason": "matchup matches the cached final only when the sides are swapped",
        }
    return {"joined": False, "key": key, "reason": "no cached final for this matchup"}


def build_report(
    picks_dir: Path,
    extra_picks_dirs: list[Path],
    since: dt.date,
    until: dt.date,
    results_dir: Path | None,
    edge_floor: float,
    max_bets_per_day: int,
    repo_revision: str | None,
) -> dict[str, Any]:
    if not picks_dir.is_dir():
        raise ReplayError(f"no such .picks directory: {picks_dir}")
    for extra in extra_picks_dirs:
        if not extra.is_dir():
            raise ReplayError(f"no such .picks directory: {extra}")
    if max_bets_per_day < 1:
        raise ReplayError("--max-bets-per-day must be at least 1")
    if not 0 < edge_floor < 1:
        raise ReplayError("--edge-floor must be between 0 and 1")
    roots = picks_roots(picks_dir.resolve(), [p.resolve() for p in extra_picks_dirs])
    results = (results_dir or (picks_dir / "audit-results")).resolve()
    policy = replay_policy(edge_floor, max_bets_per_day)
    days = [
        analyze_day(roots, day, results, edge_floor, policy)
        for day in date_range(since, until)
    ]
    return {
        "schema": "vig-slate-gate-replay-v1",
        "generated_for": {"since": since.isoformat(), "until": until.isoformat()},
        "repo_revision": repo_revision,
        "edge_floor": edge_floor,
        "max_bets_per_day": max_bets_per_day,
        "roots": [{k: v for k, v in r.items() if not k.startswith("_")} for r in roots],
        "results_dir": portable(results),
        "days": days,
        "coverage": coverage_summary(days),
        "populations": population_summary(days),
        "price_uncertainty": price_uncertainty(days),
        "best_side_edge": best_side_edge_distribution(days),
        "cross_document_disagreement": cross_document_disagreement(days),
    }


def _parsed_games(days: list[dict[str, Any]]):
    for day in days:
        for doc in day["documents"]:
            if not doc.get("readable"):
                continue
            for game in doc["games"]:
                if game.get("parsed"):
                    yield day, doc, game


def coverage_summary(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse coverage, per day and in total, as a first-class number.

    ``scheduled_games`` is the denominator wherever a cached schedule exists,
    because the count of blocks a run happened to write is a measure of that
    run's verbosity — the failure the refusal recorder was built to end. Days
    with no cached schedule report a null denominator rather than falling back
    to the block count, which would make coverage read 100% exactly where it is
    least known.
    """
    per_day = []
    for day in days:
        scheduled = day["scheduled_games"]
        per_day.append({
            "date": day["date"],
            "empty_in_every_root": day["empty_in_every_root"],
            "documents": day["document_count"],
            "scheduled_games": scheduled,
            "distinct_matchups": len(day["distinct_matchups"]),
            "blocks": day["block_count"],
            "parsed": day["parsed_count"],
            "faithful": day["faithful_count"],
            "coverage_of_schedule": (
                round(len(day["distinct_matchups"]) / scheduled, 4)
                if isinstance(scheduled, int) and scheduled > 0 else None
            ),
        })
    cross = Counter()
    for _, _, game in _parsed_games(days):
        cross[game["inputs"]["dk_fair_prob"]["cross_check"]] += 1
    return {
        "days_in_window": len(days),
        "days_empty_in_every_root": sum(1 for d in days if d["empty_in_every_root"]),
        "days_with_documents": sum(1 for d in days if d["document_count"] > 0),
        "documents": sum(d["document_count"] for d in days),
        "blocks": sum(d["block_count"] for d in days),
        "parsed": sum(d["parsed_count"] for d in days),
        "faithful_inputs": sum(d["faithful_count"] for d in days),
        "scheduled_games_total": sum(
            d["scheduled_games"] for d in days if isinstance(d["scheduled_games"], int)
        ),
        "days_without_cached_schedule": sum(
            1 for d in days if not isinstance(d["scheduled_games"], int)
        ),
        "fair_cross_check": dict(sorted(cross.items())),
        "per_day": per_day,
    }


def population_summary(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Gate outcomes and grades, one bucket per population, never summed.

    The two buckets answer different questions — "what would the deployed
    configuration have done" and "what would our own handicap have done" — over
    populations of wildly different size and selection. A combined rate would
    describe neither, and this window's recorded-handicap bucket is small enough
    that reporting it beside the market-only bucket is the only honest framing.
    """
    out: dict[str, Any] = {}
    for population in POPULATIONS:
        evaluable = cleared = ties = 0
        graded = wins = 0
        units = 0.0
        shortfalls: list[float] = []
        evaluable_by_fidelity: Counter = Counter()
        ungraded: Counter = Counter()
        selections: list[dict[str, Any]] = []
        for day, doc, game in _parsed_games(days):
            replay = game["replay"][population]
            sides = replay if population == "market_only" else (
                {"single": replay} if replay.get("evaluable") else {}
            )
            usable = [s for s in sides.values() if isinstance(s, dict) and s.get("evaluable")]
            if not usable:
                continue
            evaluable += 1
            evaluable_by_fidelity["faithful" if game["faithful_inputs"] else "inferred"] += 1
            shortfalls += [s["shortfall"] for s in usable if s.get("shortfall") is not None]
            selection = _population_selection(game, population)
            if selection is None:
                continue
            if selection.get("tie"):
                ties += 1
                continue
            cleared += 1
            within_cap = game["within_daily_cap"][population]
            grading = (
                grade(selection, game["outcome"].get("record")) if within_cap
                else {"graded": False, "reason": "dropped by the daily candidate cap"}
            )
            selections.append({
                "date": day["date"],
                "document": doc["relative_path"],
                "root": doc["root"],
                "matchup": game["matchup"],
                "side": selection["side"],
                "conservative_probability": selection["conservative_probability"],
                "current_ask": selection["current_ask"],
                "conservative_edge": selection["conservative_edge"],
                "faithful_inputs": game["faithful_inputs"],
                "within_daily_cap": within_cap,
                "grade": grading,
            })
            if not within_cap:
                continue
            if grading.get("graded"):
                graded += 1
                wins += 1 if grading["won"] else 0
                units += grading["units"]
            else:
                ungraded[grading.get("reason", "unknown")] += 1
        out[population] = {
            "games_evaluable": evaluable,
            # Split, not summed. An evaluable game whose price orientation rests
            # on a writing convention is weaker evidence than one whose sides
            # were labelled, and a single "129 games" headline hides that 78 of
            # them are the weaker kind.
            "games_evaluable_faithful": evaluable_by_fidelity["faithful"],
            "games_evaluable_inferred_order": evaluable_by_fidelity["inferred"],
            "games_clearing_floor": cleared,
            "games_within_daily_cap": sum(1 for s in selections if s["within_daily_cap"]),
            "ties_refused": ties,
            "graded": graded,
            "wins": wins,
            "losses": graded - wins,
            "units": round(units, 4) if graded else None,
            "ungraded_reasons": dict(sorted(ungraded.items())),
            "median_shortfall": _median(shortfalls),
            "p90_shortfall": _quantile(shortfalls, 0.90),
            "selections": selections,
        }
    out["recorded_handicap"]["why_empty"] = handicap_population_gap(days)
    return out


def handicap_population_gap(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Why the recorded-handicap population is the size it is.

    An empty population reads as "we checked and there was nothing", which is
    only one of the things it can mean. This counts the two steps a game has to
    survive to enter it — a stated win probability with a resolvable side, and a
    stated uncertainty haircut — so a reader can see whether the population is
    empty because the runs did not handicap or because they did not write the
    haircut down. In this window it is overwhelmingly the second, and that is a
    finding about the record rather than about the model.
    """
    handicaps = haircuts = both = 0
    for _, _, game in _parsed_games(days):
        has_handicap = game["inputs"]["raw_probability"]["provenance"] == "recorded"
        has_haircut = game["inputs"]["uncertainty_haircut"]["provenance"] == "recorded"
        handicaps += 1 if has_handicap else 0
        haircuts += 1 if has_haircut else 0
        both += 1 if has_handicap and has_haircut else 0
    return {
        "blocks_with_a_recorded_handicap": handicaps,
        "blocks_with_a_recorded_haircut": haircuts,
        "blocks_with_both": both,
        "note": (
            "a recorded handicap without a recorded haircut is NOT completed with "
            "zero: zero is the market-only fallback's own value, and borrowing it "
            "would relabel a handicapped game as a market-only one"
        ),
    }


def cross_document_disagreement(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Where two documents for one date price the SAME game differently.

    This is why both copies of a date are carried instead of one being preferred,
    and on this window it is not a technicality. 2026-08-22 has a 10:30 CT card
    and a 12:59 CT card; they cover the same slate and their Polymarket asks
    differ by up to nine points, and EVERY hypothetical selection this replay
    makes on that date comes from one of the two. A report that silently picked
    a root would have reported either five clearing games or none, with no way
    for a reader to tell which document they were reading.

    Reported as a disagreement, not resolved. The later capture is not obviously
    the better one — it disagrees with the DraftKings line recorded beside it in
    the same document, while the earlier capture matches its own — and deciding
    between them needs a quote receipt, which this window does not have.
    """
    rows: list[dict[str, Any]] = []
    for day in days:
        seen: dict[str, list[dict[str, Any]]] = {}
        for doc in day["documents"]:
            if not doc.get("readable"):
                continue
            for game in doc["games"]:
                if not game.get("parsed"):
                    continue
                ask = game["inputs"]["polymarket_ask"]["values"]
                if ask:
                    seen.setdefault(game["matchup"], []).append(
                        {"document": doc["relative_path"], "root": doc["root"], "ask": ask}
                    )
        for matchup, entries in sorted(seen.items()):
            if len(entries) < 2:
                continue
            spread = max(e["ask"]["away"] for e in entries) - min(
                e["ask"]["away"] for e in entries
            )
            if spread <= FAIR_AGREEMENT_TOLERANCE:
                continue
            rows.append({
                "date": day["date"],
                "matchup": matchup,
                "away_ask_spread": round(spread, 6),
                "sources": entries,
            })
    return {
        "games_priced_by_more_than_one_document": sum(
            1
            for day in days
            for matchup, count in Counter(
                game["matchup"]
                for doc in day["documents"] if doc.get("readable")
                for game in doc["games"]
                if game.get("parsed") and game["inputs"]["polymarket_ask"]["values"]
            ).items()
            if count > 1
        ),
        "games_where_the_documents_disagree": len(rows),
        "max_spread": round(max((r["away_ask_spread"] for r in rows), default=0.0), 6),
        "disagreements": rows,
    }


def best_side_edge_distribution(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Distribution of the best available market-only edge, one value per game.

    This is the module's outside check on its own parser. The drought diagnostic
    reached the same quantity from the structured schedule records rather than
    from prose, and if this report's prose reading is systematically wrong the
    two distributions will not line up. Reported as a distribution rather than a
    single rate because the question the window actually raises is about the
    TAIL: the median says the book is efficient, and it is the handful of games
    out at the maximum that decide whether any floor is reachable at all.
    """
    best: list[float] = []
    for _, _, game in _parsed_games(days):
        edges = [
            side["conservative_edge"]
            for side in game["replay"]["market_only"].values()
            if isinstance(side, dict) and side.get("evaluable")
        ]
        if edges:
            best.append(max(edges))
    return {
        "games": len(best),
        "median": _median(best),
        "p90": _quantile(best, 0.90),
        "max": round(max(best), 6) if best else None,
        "measures": "max over the two sides of (dk_fair - polymarket_ask), at the ask",
    }


def price_uncertainty(days: list[dict[str, Any]]) -> dict[str, Any]:
    """What the ask-versus-traded-price gap is worth, stated as a band.

    The traded price is unavailable for every game in this window, so the gap is
    reported as the observable two-sided ask sum rather than modelled away. A
    sum of 1.005 bounds how much a mid could have improved one side: at most the
    excess over 1.0, and only if the whole spread fell on that side.
    """
    sums = [
        game["inputs"]["polymarket_ask"]["two_sided_sum"]
        for _, _, game in _parsed_games(days)
        if game["inputs"]["polymarket_ask"]["two_sided_sum"] is not None
    ]
    excess = [round(s - 1.0, 6) for s in sums]
    return {
        "traded_price": "unavailable — no Polymarket quote receipt exists in this window",
        "prices_are": "ask",
        "two_sided_ask_sums": len(sums),
        "median_sum": _median(sums),
        "median_excess_over_one": _median(excess),
        "max_excess_over_one": max(excess) if excess else None,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 6)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 6)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return round(ordered[index], 6)


def render(report: dict[str, Any]) -> str:
    window = report["generated_for"]
    lines = [
        f"MLB gate replay — {window['since']} to {window['until']} (read-only)",
        f"edge floor {report['edge_floor']:.4f}, daily cap {report['max_bets_per_day']} "
        f"per card; prices are ASKS; traded price unavailable",
        "",
        "Coverage",
        "--------",
    ]
    cov = report["coverage"]
    lines += [
        f"days {cov['days_in_window']}, empty in every root {cov['days_empty_in_every_root']}, "
        f"with documents {cov['days_with_documents']}",
        f"documents {cov['documents']}, blocks {cov['blocks']}, parsed {cov['parsed']}, "
        f"faithful inputs {cov['faithful_inputs']}",
        f"scheduled games (cached) {cov['scheduled_games_total']}; "
        f"days without a cached schedule {cov['days_without_cached_schedule']}",
        f"fair cross-check {cov['fair_cross_check']}",
        "",
        f"{'date':<12}{'docs':>5}{'sched':>7}{'games':>7}{'blocks':>8}{'parsed':>8}"
        f"{'faithful':>10}{'cover':>8}",
    ]
    for row in cov["per_day"]:
        share = row["coverage_of_schedule"]
        cover = "—" if share is None else f"{share:.0%}"
        sched = "—" if row["scheduled_games"] is None else row["scheduled_games"]
        note = "  (no artifact in any root)" if row["empty_in_every_root"] else ""
        lines.append(
            f"{row['date']:<12}{row['documents']:>5}{sched:>7}{row['distinct_matchups']:>7}"
            f"{row['blocks']:>8}{row['parsed']:>8}{row['faithful']:>10}{cover:>8}{note}"
        )
    lines += ["", "Populations (never summed)", "--------------------------"]
    for population in POPULATIONS:
        stats = report["populations"][population]
        lines += [
            f"[{population}]",
            f"  evaluable games {stats['games_evaluable']} "
            f"({stats['games_evaluable_faithful']} faithful, "
            f"{stats['games_evaluable_inferred_order']} inferred order), clearing the floor "
            f"{stats['games_clearing_floor']}, kept by the daily cap "
            f"{stats['games_within_daily_cap']}, ties refused {stats['ties_refused']}",
            f"  graded {stats['graded']} ({stats['wins']}-{stats['losses']}), "
            f"units {stats['units'] if stats['units'] is not None else '—'}",
            f"  shortfall below the floor: median "
            f"{stats['median_shortfall']}, p90 {stats['p90_shortfall']}",
        ]
        if stats.get("why_empty"):
            gap = stats["why_empty"]
            lines.append(
                f"  blocks with a recorded handicap {gap['blocks_with_a_recorded_handicap']}, "
                f"with a recorded haircut {gap['blocks_with_a_recorded_haircut']}, "
                f"with both {gap['blocks_with_both']}"
            )
        if stats["ungraded_reasons"]:
            lines.append(f"  ungraded: {stats['ungraded_reasons']}")
        for pick in stats["selections"]:
            flags = "" if pick["faithful_inputs"] else "  [inferred inputs]"
            if not pick["within_daily_cap"]:
                flags += "  [over the daily cap]"
            result = pick["grade"]
            verdict = (
                ("WIN" if result["won"] else "LOSS") if result.get("graded")
                else f"ungraded ({result.get('reason')})"
            )
            lines.append(
                f"    {pick['date']} [{pick['root']}] {pick['matchup']} — {pick['side']} at "
                f"{pick['current_ask']:.4f}, edge {pick['conservative_edge']:+.4f} "
                f"-> {verdict}{flags}"
            )
        lines.append("")
    cross = report["cross_document_disagreement"]
    lines += [
        "Same game, two documents, different price",
        "-----------------------------------------",
        f"games priced by more than one document "
        f"{cross['games_priced_by_more_than_one_document']}, of which the documents "
        f"disagree on {cross['games_where_the_documents_disagree']} "
        f"(max away-ask spread {cross['max_spread']})",
    ]
    for row in cross["disagreements"]:
        sources = "; ".join(
            f"{s['root']}:{s['document'].removeprefix('slate/').removesuffix('.md')} "
            f"{s['ask']['away']:.3f}/{s['ask']['home']:.3f}"
            for s in row["sources"]
        )
        lines.append(f"    {row['date']} {row['matchup']} — {sources}")
    lines.append("")
    dist = report["best_side_edge"]
    lines += [
        "Best available market-only edge, one value per game",
        "---------------------------------------------------",
        f"games {dist['games']}, median {dist['median']}, p90 {dist['p90']}, max {dist['max']}",
        "",
    ]
    band = report["price_uncertainty"]
    lines += [
        "Price uncertainty",
        "-----------------",
        f"traded price: {band['traded_price']}",
        f"two-sided ask sums {band['two_sided_ask_sums']}, median {band['median_sum']}, "
        f"median excess over 1.0 {band['median_excess_over_one']}, "
        f"max {band['max_excess_over_one']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only replay of the MLB gate over a window of slate documents"
    )
    parser.add_argument("--picks-dir", required=True, help="the primary .picks directory")
    parser.add_argument(
        "--also-picks-dir", action="append", default=[], metavar="DIR",
        help=(
            "an additional .picks root, repeatable. Every date is read in every "
            "root and documents are kept SEPARATE, so a date written to two "
            "checkouts contributes both copies rather than whichever one a "
            "precedence rule happened to prefer."
        ),
    )
    parser.add_argument("--since", required=True, help="first day YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="last day YYYY-MM-DD")
    parser.add_argument(
        "--results-dir", help="MLB schedule cache (default: <picks-dir>/audit-results)"
    )
    parser.add_argument(
        "--edge-floor", type=float, default=DEFAULT_MIN_CONSERVATIVE_EDGE,
        help=(
            "conservative edge floor to replay against. Defaults to the repo "
            "constant rather than the live risk_limits.json, so a rerun of this "
            "report is reproducible and cannot silently change meaning when a "
            "rail is edited."
        ),
    )
    parser.add_argument(
        "--max-bets-per-day", type=int, default=DEFAULT_MAX_MLB_OFFICIAL_BETS_PER_DAY,
        help=(
            "daily candidate cap to replay against, applied per card by the "
            "gate's own enforce_daily_candidate_limit. Clearing the edge floor "
            "is not the same as being bet."
        ),
    )
    parser.add_argument("--repo-revision", help="analysis code revision, recorded verbatim")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = parser.parse_args(argv)

    try:
        report = build_report(
            picks_dir=Path(args.picks_dir).expanduser(),
            extra_picks_dirs=[Path(p).expanduser() for p in args.also_picks_dir],
            since=parse_date(args.since),
            until=parse_date(args.until),
            results_dir=Path(args.results_dir).expanduser() if args.results_dir else None,
            edge_floor=args.edge_floor,
            max_bets_per_day=args.max_bets_per_day,
            repo_revision=args.repo_revision,
        )
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
