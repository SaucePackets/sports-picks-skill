#!/usr/bin/env python3
"""Read-only historical audit of dated MLB pick schedules.

`scripts/vig_calibration_report.py` answers "how did the money do", reading the
settled ledger. This answers a different and older question: for every dated
`.picks/execute/<date>-schedule.json` this repo has ever written, what did the
slate propose, what actually happened on the field, and — separately — how much
of the current selection contract was even present to judge it by.

Three things this deliberately does NOT do.

1. It never backfills. The schedules span at least four incompatible candidate
   shapes, and most of them predate the probability contract in
   `mlb_runtime_policy.REQUIRED_EXECUTION_FIELDS`. A missing
   `conservative_probability` makes the 5-point floor UNEVALUABLE for that
   candidate; it does not make it a candidate that failed the floor. The two
   are different findings and are reported as different findings.
2. It never fetches a score outside the repo's own interface. Official results
   come from `mlb_final_scores.final_scores` over an MLB Stats API schedule
   payload — the same function the settlement path uses — and the payload is
   read from a cache directory. `--fetch` populates that cache through
   `http_util.fetch_json`; without it the tool is entirely offline and says so
   for every date it could not reconcile.
3. It never touches the execution path, a betting rail, or any file. Output is
   stdout. `min_conservative_edge` is READ, as a constant, so that the floor
   this audit reports against cannot drift from the floor the gate enforces —
   but the live `risk_limits.json` is not consulted, because a rail's value
   today is not the rail that was in force on 2026-05-26.

Separating side correctness from data quality is the whole point. A pick can be
right about the baseball and unjudgeable as a bet (no price on the card), or
wrong about the baseball while having been a perfectly well-formed wager. Days
with no candidates at all are CONTROLS: they are counted, named, and excluded
from every accuracy denominator, because a day on which the model declined to
bet is not a day on which it went 0-for-0.

Usage:
  python scripts/vig_historical_audit.py --picks-dir ~/projects/sports-picks-runtime/.picks
  python scripts/vig_historical_audit.py --results-dir /tmp/mlb-results --fetch
  python scripts/vig_historical_audit.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_final_scores import SCHEDULE_URL, final_scores  # noqa: E402
from mlb_runtime_policy import (  # noqa: E402
    DEFAULT_MIN_CONSERVATIVE_EDGE,
    REQUIRED_EXECUTION_FIELDS,
)
from vig_calibration_report import wilson_ci  # noqa: E402

# Deliberately NOT importing resolve_root from vig_review_gate_common: that
# module imports the execution gate, the watchlist, and the evidence stack, so
# a read-only audit would acquire the entire betting import graph to find one
# directory. The root comes from the flag or the same env var instead.
PICKS_ROOT_ENV = "SPORTS_PICKS_ROOT"

SCHEDULE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-schedule\.json$")

# Strict American-odds form. Anything else — notably the 2026-05-29/30 prose
# prices like "DK/ESPN CLE -131; Polymarket ask 0.56" — is retained verbatim
# and reported unparsed. Scraping the first signed integer out of a sentence
# that also contains a decimal price is a guess, and a guess here silently
# becomes an implied probability in a calibration bucket.
AMERICAN_ODDS_RE = re.compile(r"^[+-]\d{2,5}$")

# Candidate keys that have, at some point, carried the picked team.
SIDE_FIELDS = ("side", "pick_side", "pick_team")

# 2026-07-08/09 wrote the side as "Detroit Tigers ML". The market is already
# known — every one of these cards is a moneyline — so the suffix is stripped
# rather than treated as part of the club name. Anchored and explicit: this
# removes a named market token, not "whatever trails the team".
MARKET_SUFFIX_RE = re.compile(r"\s+(ML|moneyline|money\s+line)$", re.IGNORECASE)

# Polymarket price fields in priority order. `fill_price` is the price actually
# paid and wins when present; the rest are the price on the card at slate time.
ENTRY_PRICE_FIELDS = ("fill_price", "execution_price", "polymarket_ask_executed")
SLATE_PRICE_FIELDS = (
    "polymarket_ask",
    "polymarket_price",
    "polymarket_price_at_scan",
    "approved_polymarket_ask",
    "orderbook_price",
)

# Stated-probability fields, in the order of preference for the calibration
# population. `conservative_probability` is NOT here: it is the gate's input,
# not a forecast, and it is handled separately by the floor arithmetic.
STATED_PROBABILITY_FIELDS = ("win_probability", "raw_probability")

# The 30 clubs, keyed by every abbreviation the slates have used.
#
# What the surrounding cross-check does and does not buy, stated exactly. Every
# caller requires the resolved name to be one of the two teams the official row
# names, so an entry that is wrong in the ordinary way — stale after a rename or
# relocation, a typo'd city — resolves to a team that is not playing and the
# candidate is reported unresolved. What it CANNOT catch is an entry that maps
# an abbreviation to the OPPONENT in some game: that name is in the row, so it
# resolves, and it resolves wrongly. `test_the_cross_check_cannot_catch_a_table
# _entry_that_names_the_opponent` demonstrates that gap rather than claiming it
# away. Two things narrow it: abbreviations are tried only after every
# full-name and nickname form has failed, and the table is checked for
# self-consistency below.
TEAM_ABBREVIATIONS: dict[str, str] = {
    "ARI": "Arizona Diamondbacks", "AZ": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CHW": "Chicago White Sox", "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC": "Kansas City Royals", "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "ATH": "Athletics", "OAK": "Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres", "SDP": "San Diego Padres",
    "SEA": "Seattle Mariners",
    "SF": "San Francisco Giants", "SFG": "San Francisco Giants",
    "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals", "WSN": "Washington Nationals",
}

# Process classification vocabularies. Closed sets, validated on construction,
# so a typo in a classifier becomes a test failure rather than a silent new
# bucket that aggregates to zero.
SIDE_OUTCOMES = ("win", "loss", "push", "side_unresolved", "unreconciled")
SCHEMA_VARIANTS = ("current", "legacy_object", "legacy_bare_list", "unreadable")
DATA_QUALITY = ("full_contract", "partial_contract", "no_contract_fields")
PRICE_QUALITY = ("market_price", "book_price_only", "prose_price_unparsed", "no_price")
FLOOR_VERDICTS = ("cleared", "below_floor", "unevaluable")
DISPOSITIONS = ("executed", "skipped", "review_rejected", "proposed_no_bet")


class AuditError(Exception):
    """A caller mistake — a missing directory, an unusable date range."""


# ---------------------------------------------------------------------------
# Schedule loading and normalization
# ---------------------------------------------------------------------------


def load_schedule(path: Path) -> dict[str, Any]:
    """Read one dated schedule file into a normalized envelope.

    Handles every top-level shape on disk: the current object with a
    `candidates` list, older objects carrying extra bookkeeping keys, and the
    bare `[]` list that 2026-07-17 was written as. An unreadable file is
    reported as unreadable — never as an empty day, which would otherwise be
    indistinguishable from a genuine no-pick control.
    """
    match = SCHEDULE_FILE_RE.match(path.name)
    file_date = match.group(1) if match else None
    envelope: dict[str, Any] = {
        "date": file_date,
        "path": str(path),
        "schema_variant": "unreadable",
        "candidates": [],
        "errors": [],
    }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        envelope["errors"].append(f"unreadable: {exc}")
        return envelope

    if isinstance(raw, list):
        envelope["schema_variant"] = "legacy_bare_list"
        candidates = raw
    elif isinstance(raw, dict):
        stated = raw.get("date")
        if isinstance(stated, str) and stated:
            if file_date and stated != file_date:
                envelope["errors"].append(
                    f"date field {stated!r} disagrees with filename date {file_date!r}"
                )
            envelope["date"] = envelope["date"] or stated
        candidates = raw.get("candidates")
        if candidates is None:
            envelope["errors"].append("no `candidates` key")
            candidates = []
        elif not isinstance(candidates, list):
            envelope["errors"].append("`candidates` is not a list")
            candidates = []
        # The current writer always emits sport+market_type alongside the
        # candidates; older days carried status/daily_cap/exposure keys instead.
        envelope["schema_variant"] = (
            "current" if raw.get("sport") and "market_type" in raw else "legacy_object"
        )
    else:
        envelope["errors"].append(f"top level is {type(raw).__name__}, not object or list")
        return envelope

    kept = []
    for index, candidate in enumerate(candidates):
        if isinstance(candidate, dict):
            kept.append(candidate)
        else:
            envelope["errors"].append(f"candidate {index} is {type(candidate).__name__}, not object")
    envelope["candidates"] = kept
    if envelope["date"] is None:
        envelope["errors"].append("no date in filename or document")
    return envelope


def parse_american_odds(value: Any) -> tuple[int | None, str]:
    """Strict American-odds parse. Returns (odds, status)."""
    if isinstance(value, bool) or value is None:
        return None, "absent" if value is None else "not_a_price"
    if isinstance(value, int):
        return (value, "parsed") if abs(value) >= 100 else (None, "not_a_price")
    if isinstance(value, float):
        return (int(value), "parsed") if float(value).is_integer() and abs(value) >= 100 else (None, "not_a_price")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "absent"
        if AMERICAN_ODDS_RE.match(text):
            return int(text), "parsed"
        return None, "prose_unparsed"
    return None, "not_a_price"


def american_implied_probability(odds: int) -> float:
    """Break-even win probability for American odds, vig included (no de-vig)."""
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _probability(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and 0 < number < 1 else None


def contract_field_present(field: str, value: Any) -> bool:
    """Is `field` usably present, by the same rule the execution gate applies?

    This mirrors `mlb_runtime_policy.stale_probability_field_errors` field by
    field on purpose: "present" in this audit has to mean "would have satisfied
    the gate", or a candidate could be reported as carrying a complete contract
    that the gate would still have refused. The rule is duplicated rather than
    called because the gate returns a fused error list for the whole candidate
    and this needs the per-field answer; `test_vig_historical_audit` pins the
    two against each other so they cannot drift apart silently.
    """
    if field == "model_version":
        return isinstance(value, str) and bool(value.strip())
    if field == "uncertainty_haircut":
        number = _number(value)
        return number is not None and number >= 0
    return _probability(value) is not None


def _first_present(candidate: dict[str, Any], fields: tuple[str, ...]) -> tuple[str | None, Any]:
    for field in fields:
        if field in candidate and candidate[field] is not None:
            return field, candidate[field]
    return None, None


def split_matchup(game: Any) -> tuple[str | None, str | None]:
    """`"Away at Home"` -> (away, home). Anything else -> (None, None).

    Both separators the slates have used are accepted: the usual `" at "` and
    the terse `"BOS @ LAA"` of 2026-07-05. The halves are returned as written —
    full names, bare nicknames, and abbreviations all occur, and resolving them
    is `team_token_matches`' job, against the teams that actually played.
    """
    if not isinstance(game, str):
        return None, None
    parts = re.split(r"\s+at\s+|\s+@\s+", game.strip())
    if len(parts) != 2 or not all(part.strip() for part in parts):
        return None, None
    return parts[0].strip(), parts[1].strip()


def team_token_matches(token: str, full_name: str) -> bool:
    """Does `token` name `full_name`, under any form the slates have used?

    Three forms, each exact in its own way and none of them fuzzy: the full
    club name, an abbreviation from the table, and the bare nickname as a
    whole-word suffix of the full name ("Tigers" for "Detroit Tigers"). A
    nickname is a suffix match rather than a substring one, so "Sox" cannot
    quietly become "Red Sox" — and every caller additionally requires the
    resolved team to be one that actually played, which is what keeps a wrong
    table entry from ever resolving to the opponent.
    """
    return team_token_match_kind(token, full_name) is not None


def team_token_match_kind(token: str, full_name: str) -> str | None:
    """`"name"`, `"nickname"`, `"abbreviation"`, or None. The kind is load-bearing:
    resolution prefers the two forms that need no lookup table."""
    text = token.strip()
    if not text:
        return None
    lowered, target = text.casefold(), full_name.casefold()
    if lowered == target:
        return "name"
    if target.endswith(" " + lowered):
        return "nickname"
    mapped = TEAM_ABBREVIATIONS.get(text.upper())
    return "abbreviation" if mapped and mapped.casefold() == target else None


def normalize_candidate(raw: dict[str, Any], date: str | None) -> dict[str, Any]:
    """Flatten one candidate into the audit's explicit schema.

    Every field records where it came from, and absence is recorded as absence.
    Nothing is derived from a field that is not there.
    """
    # Every side field present is kept, not just the first: 2026-05-26 carries
    # `side: "TB"` alongside `pick_side: "Tampa Bay Rays"`, and the full name is
    # the stronger key. Resolution tries them in order and takes the first that
    # lands on a team in the official row.
    side_candidates = [
        (field, raw[field]) for field in SIDE_FIELDS
        if isinstance(raw.get(field), str) and raw[field].strip()
    ]
    side_field, side_value = side_candidates[0] if side_candidates else (None, None)
    away, home = split_matchup(raw.get("game"))

    entry_field, entry_raw = _first_present(raw, ENTRY_PRICE_FIELDS)
    slate_field, slate_raw = _first_present(raw, SLATE_PRICE_FIELDS)
    entry_price = _probability(entry_raw)
    slate_price = _probability(slate_raw)

    book_odds, book_status = parse_american_odds(raw.get("price"))

    stated_field, stated_raw = _first_present(raw, STATED_PROBABILITY_FIELDS)
    stated_probability = _probability(stated_raw)

    contract_present = [f for f in REQUIRED_EXECUTION_FIELDS if contract_field_present(f, raw.get(f))]
    contract_missing = [f for f in REQUIRED_EXECUTION_FIELDS if f not in contract_present]

    executed = raw.get("executed") is True
    if executed:
        disposition = "executed"
    elif raw.get("skipped") is True:
        disposition = "skipped"
    elif raw.get("vig_review_needed") is True and raw.get("vig_approved") is False:
        disposition = "review_rejected"
    else:
        disposition = "proposed_no_bet"

    stake = _number(raw.get("fill_notional"))
    if stake is None:
        quantity = _number(raw.get("fill_quantity")) or _number(raw.get("fill_shares")) or _number(raw.get("execution_qty"))
        if entry_price is not None and quantity is not None:
            stake = round(entry_price * quantity, 6)
    pnl = _number(raw.get("pnl"))
    if pnl is None:
        pnl = _number(raw.get("settlement_pnl"))

    return {
        "date": date,
        "event_id": raw.get("event_id"),
        "game": raw.get("game"),
        "away_team": away,
        "home_team": home,
        "game_pk": raw.get("game_pk"),
        "side_raw": side_value,
        "side_field": side_field,
        "side_candidates": side_candidates,
        "opponent_raw": raw.get("opponent"),
        "confidence": raw.get("confidence"),
        "entry_price": entry_price,
        "entry_price_field": entry_field if entry_price is not None else None,
        "slate_price": slate_price,
        "slate_price_field": slate_field if slate_price is not None else None,
        "book_odds": book_odds,
        "book_odds_status": book_status,
        "book_price_raw": raw.get("price"),
        "stated_probability": stated_probability,
        "stated_probability_field": stated_field if stated_probability is not None else None,
        "dk_fair_prob": _probability(raw.get("dk_fair_prob")),
        "conservative_probability": _probability(raw.get("conservative_probability")),
        "current_ask": _probability(raw.get("current_ask")),
        "stored_net_edge": _number(raw.get("net_edge")),
        "stored_projected_edge": _number(raw.get("projected_edge_at_current_ask")),
        "contract_fields_present": contract_present,
        "contract_fields_missing": contract_missing,
        "disposition": disposition,
        "skip_reason": raw.get("skip_reason"),
        "stake_usd": stake,
        "commission_usd": _number(raw.get("commission")),
        "pnl_usd": pnl,
        "recorded_result": raw.get("result") or raw.get("settlement_result"),
        "recorded_final_score": raw.get("final_score") or raw.get("score"),
    }


# ---------------------------------------------------------------------------
# Official result reconciliation
# ---------------------------------------------------------------------------


def load_official_rows(
    results_dir: Path, date: str
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], dict[str, Any]]:
    """Official Final rows for `date` from the cached Stats API payload.

    The payload is the raw MLB Stats API schedule response; the rows come from
    `mlb_final_scores.final_scores`, which is the function the settlement path
    already uses. Provenance travels with the rows so a reconciled result can
    always be traced back to the document it came from.
    """
    path = results_dir / f"{date}.json"
    provenance = {
        "source": "mlb-statsapi-schedule",
        "url": SCHEDULE_URL.format(date=date),
        "cache_path": str(path),
        "status": "missing",
    }
    if not path.is_file():
        return None, [], provenance
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        provenance["status"] = f"unreadable: {exc}"
        return None, [], provenance
    if not isinstance(payload, dict):
        provenance["status"] = "payload is not an object"
        return None, [], provenance
    fetched_at = payload.get("_audit_fetched_at_utc")
    if isinstance(fetched_at, str):
        provenance["fetched_at_utc"] = fetched_at
    rows = final_scores(payload)
    provenance["status"] = "ok"
    provenance["final_games"] = len(rows)
    unfinished = unfinished_games(payload)
    provenance["unfinished_games"] = len(unfinished)
    return rows, unfinished, provenance


def unfinished_games(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Games in the payload that `final_scores` correctly refuses to return.

    Only so that "this game never happened" and "this game had not finished
    when the results were cached" stop looking alike. `final_scores` is the
    single source of a RESULT; nothing below it produces one, and a game found
    here still counts as unreconciled.
    """
    rows = []
    for date_block in payload.get("dates", []) or []:
        if not isinstance(date_block, dict):
            continue
        for game in date_block.get("games", []) or []:
            if not isinstance(game, dict):
                continue
            status = game.get("status", {}).get("detailedState")
            if status == "Final":
                continue
            teams = game.get("teams", {})
            rows.append({
                "away": teams.get("away", {}).get("team", {}).get("name"),
                "home": teams.get("home", {}).get("team", {}).get("name"),
                "status": status,
            })
    return rows


def resolve_side(candidate: dict[str, Any], away: str, home: str) -> tuple[str | None, str]:
    """Resolve the picked side to one of the two teams in the official row.

    Every route must land on a team the game actually contains. That
    cross-check is what makes the abbreviation table safe: a wrong entry
    resolves to a team that is not playing, and the candidate is reported
    unresolved rather than credited to the wrong side.
    """
    tokens = [
        MARKET_SUFFIX_RE.sub("", value.strip()).strip()
        for _field, value in candidate.get("side_candidates") or []
    ]
    tokens = [token for token in tokens if token]
    if not tokens:
        return None, "no_side_on_card"
    # Table-free forms first, across ALL side fields, before any abbreviation is
    # consulted. 2026-05-26 carries `side: "TB"` and `pick_side: "Tampa Bay
    # Rays"`; taking the full name means the table is never on that path at all.
    for kinds in (("name", "nickname"), ("abbreviation",)):
        for token in tokens:
            hits = [team for team in (away, home) if team_token_match_kind(token, team) in kinds]
            if len(hits) == 1:
                return hits[0], f"matched_by_{team_token_match_kind(token, hits[0])}"
            if len(hits) > 1:
                # Both teams answer to the same token. Nothing here can break
                # the tie honestly, so nothing here tries to.
                return None, "side_token_matches_both_teams"
    return None, "side_names_a_team_not_in_this_game"


def match_official_game(candidate: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Find the official row for a candidate's game.

    `event_id` on these cards is an ESPN id, not a Stats API `gamePk`, so it is
    not a join key. Matching is on the team pair, with `game_pk` used when the
    card happens to carry one. A doubleheader produces two rows for the same
    pair; without a `game_pk` that is genuinely ambiguous and fails closed.
    """
    game_pk = candidate.get("game_pk")
    if game_pk is not None:
        for row in rows:
            if row.get("gamePk") == game_pk or str(row.get("gamePk")) == str(game_pk):
                return row, "game_pk"
    away, home = candidate.get("away_team"), candidate.get("home_team")
    if not away or not home:
        return None, "no_matchup_on_card"
    hits = [
        row for row in rows
        if isinstance(row.get("away"), str) and isinstance(row.get("home"), str)
        and team_token_matches(away, row["away"])
        and team_token_matches(home, row["home"])
    ]
    if len(hits) == 1:
        return hits[0], "team_pair"
    if len(hits) > 1:
        return None, "ambiguous_doubleheader"
    return None, "no_official_game"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_data_quality(candidate: dict[str, Any]) -> str:
    if not candidate["contract_fields_missing"]:
        return "full_contract"
    return "partial_contract" if candidate["contract_fields_present"] else "no_contract_fields"


def classify_price_quality(candidate: dict[str, Any]) -> str:
    if candidate["entry_price"] is not None or candidate["slate_price"] is not None:
        return "market_price"
    if candidate["book_odds"] is not None:
        return "book_price_only"
    if candidate["book_odds_status"] == "prose_unparsed":
        return "prose_price_unparsed"
    return "no_price"


def effective_price(candidate: dict[str, Any]) -> tuple[float | None, str | None]:
    """The price the audit reasons about: paid if paid, else asked."""
    if candidate["entry_price"] is not None:
        return candidate["entry_price"], "entry"
    if candidate["slate_price"] is not None:
        return candidate["slate_price"], "slate"
    return None, None


def evaluate_floor(candidate: dict[str, Any], floor: float) -> dict[str, Any]:
    """Would the current conservative edge floor have cleared this candidate?

    The gate's edge is `conservative_probability - current_ask`, and nothing
    else. When either term is absent the verdict is `unevaluable` — the honest
    answer, and the one that fails closed. A stated-probability edge is
    computed alongside it when possible and labelled advisory, because a model
    number that never passed through the uncertainty haircut is not the
    quantity the floor was set against.
    """
    result: dict[str, Any] = {
        "floor": floor,
        "verdict": "unevaluable",
        "basis": None,
        "conservative_edge": None,
        "advisory_stated_edge": None,
        "advisory_basis": None,
        "reason": None,
    }
    price, price_basis = effective_price(candidate)
    stated = candidate["stated_probability"]
    if stated is not None and price is not None:
        result["advisory_stated_edge"] = round(stated - price, 6)
        result["advisory_basis"] = f"{candidate['stated_probability_field']} - {price_basis}_price"

    conservative = candidate["conservative_probability"]
    ask = candidate["current_ask"]
    if conservative is None or ask is None:
        missing = [
            name for name, value in (("conservative_probability", conservative), ("current_ask", ask))
            if value is None
        ]
        result["reason"] = "missing " + ", ".join(missing)
        return result
    edge = round(conservative - ask, 6)
    result["conservative_edge"] = edge
    result["basis"] = "conservative_probability - current_ask"
    result["verdict"] = "cleared" if edge >= floor else "below_floor"
    return result


def audit_candidate(
    raw: dict[str, Any], date: str | None, rows: list[dict[str, Any]] | None,
    unfinished: list[dict[str, Any]], floor: float,
) -> dict[str, Any]:
    """One fully classified audit record."""
    candidate = normalize_candidate(raw, date)
    record = dict(candidate)
    record["data_quality"] = classify_data_quality(candidate)
    record["price_quality"] = classify_price_quality(candidate)
    record["floor"] = evaluate_floor(candidate, floor)

    price, price_basis = effective_price(candidate)
    record["price_used"] = price
    record["price_basis"] = price_basis
    record["implied_breakeven"] = price
    record["book_implied_breakeven"] = (
        round(american_implied_probability(candidate["book_odds"]), 6)
        if candidate["book_odds"] is not None else None
    )

    record["official"] = None
    record["match_method"] = None
    record["resolved_side"] = None
    record["side_resolution"] = None
    record["side_outcome"] = "unreconciled"
    record["unreconciled_reason"] = None

    if rows is None:
        record["unreconciled_reason"] = "no cached official results for this date"
        return record

    official, method = match_official_game(candidate, rows)
    record["match_method"] = method
    if official is None:
        # "the game is not in the Final rows" has two very different causes: the
        # card names a game that never existed, or the payload was cached before
        # the game ended. Both stay unreconciled — neither invents a result —
        # but only one of them is a defect in the card.
        if method == "no_official_game":
            pending = [
                game for game in unfinished
                if isinstance(game.get("away"), str) and isinstance(game.get("home"), str)
                and team_token_matches(candidate["away_team"], game["away"])
                and team_token_matches(candidate["home_team"], game["home"])
            ]
            if pending:
                method = f"not_final: {pending[0]['status']}"
                record["match_method"] = method
        record["unreconciled_reason"] = method
        return record

    record["official"] = {
        "gamePk": official.get("gamePk"),
        "away": official.get("away"),
        "home": official.get("home"),
        "away_score": official.get("away_score"),
        "home_score": official.get("home_score"),
        "winner": official.get("winner"),
    }
    resolved, how = resolve_side(candidate, official["away"], official["home"])
    record["resolved_side"] = resolved
    record["side_resolution"] = how
    if resolved is None:
        record["side_outcome"] = "side_unresolved"
        record["unreconciled_reason"] = how
        return record

    winner = official.get("winner")
    if winner is None:
        record["side_outcome"] = "push"
    elif winner.casefold() == resolved.casefold():
        record["side_outcome"] = "win"
    else:
        record["side_outcome"] = "loss"

    recorded = record["recorded_result"]
    if isinstance(recorded, str) and recorded.strip().lower() in ("win", "loss"):
        record["recorded_result_agrees"] = recorded.strip().lower() == record["side_outcome"]
    return record


# ---------------------------------------------------------------------------
# Days and aggregates
# ---------------------------------------------------------------------------


def audit_day(schedule_path: Path, results_dir: Path, floor: float) -> dict[str, Any]:
    envelope = load_schedule(schedule_path)
    date = envelope["date"]
    if date is None:
        rows, unfinished, provenance = None, [], {"status": "no date on the schedule"}
    else:
        rows, unfinished, provenance = load_official_rows(results_dir, date)
    records = [audit_candidate(raw, date, rows, unfinished, floor) for raw in envelope["candidates"]]
    is_control = envelope["schema_variant"] != "unreadable" and not records and not envelope["errors"]
    return {
        "date": date,
        "path": envelope["path"],
        "schema_variant": envelope["schema_variant"],
        "errors": envelope["errors"],
        "results_provenance": provenance,
        # A control is a day the slate ran and proposed nothing. An unreadable
        # or malformed file is NOT a control, however empty it looks.
        "no_pick_control": is_control,
        "candidates": records,
    }


def calibration_buckets(records: list[dict[str, Any]], width: float, min_sample: int) -> list[dict[str, Any]]:
    """Stated-probability calibration over decided candidates only.

    Population is stated explicitly on every bucket, and a bucket below
    `min_sample` carries `sufficient: false`. Small buckets are still shown —
    hiding them would misrepresent coverage — but they are marked so no reader
    takes a 1-of-2 bucket for a calibration finding.
    """
    decided = [
        r for r in records
        if r["side_outcome"] in ("win", "loss") and r["stated_probability"] is not None
    ]
    grouped: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for record in decided:
        edge = min(math.floor(record["stated_probability"] / width) * width, 1.0 - width)
        grouped[round(edge, 6)].append(record)
    buckets = []
    for low in sorted(grouped):
        members = grouped[low]
        wins = sum(1 for m in members if m["side_outcome"] == "win")
        n = len(members)
        lo, hi = wilson_ci(wins, n)
        buckets.append({
            "bucket": f"[{low:.2f},{min(low + width, 1.0):.2f})",
            "n": n,
            "wins": wins,
            "actual_rate": round(wins / n, 6),
            "mean_stated": round(sum(m["stated_probability"] for m in members) / n, 6),
            "wilson_95": [round(lo, 6), round(hi, 6)],
            "sufficient": n >= min_sample,
        })
    return buckets


def aggregate(days: list[dict[str, Any]], floor: float, width: float, min_sample: int) -> dict[str, Any]:
    """Roll days up without ever letting a control day enter an accuracy rate."""
    records = [record for day in days for record in day["candidates"]]
    control_days = [day for day in days if day["no_pick_control"]]
    malformed_days = [day for day in days if day["errors"] or day["schema_variant"] == "unreadable"]
    pick_days = [day for day in days if day["candidates"]]
    wager_days = [d for d in pick_days if any(r["disposition"] == "executed" for r in d["candidates"])]

    decided = [r for r in records if r["side_outcome"] in ("win", "loss")]
    wins = sum(1 for r in decided if r["side_outcome"] == "win")
    lo, hi = wilson_ci(wins, len(decided)) if decided else (0.0, 1.0)

    executed = [r for r in records if r["disposition"] == "executed"]
    staked = sum(r["stake_usd"] for r in executed if r["stake_usd"] is not None)
    priced = [r for r in executed if r["stake_usd"] is not None]
    with_pnl = [r for r in executed if r["pnl_usd"] is not None]
    pnl = sum(r["pnl_usd"] for r in with_pnl)
    pnl_staked = sum(r["stake_usd"] for r in with_pnl if r["stake_usd"] is not None)

    disagreements = [
        {"date": r["date"], "game": r["game"], "recorded": r["recorded_result"], "official": r["side_outcome"]}
        for r in records if r.get("recorded_result_agrees") is False
    ]

    return {
        "edge_floor": floor,
        "days": {
            "total": len(days),
            "no_pick_controls": len(control_days),
            "control_dates": [d["date"] for d in control_days],
            "days_with_candidates": len(pick_days),
            "days_with_a_wager": len(wager_days),
            "malformed_or_unreadable": len(malformed_days),
            "malformed_dates": [d["date"] for d in malformed_days],
            "note": (
                "Control days proposed no candidate. They are excluded from every "
                "accuracy denominator below: declining to bet is not an 0-for-0 day."
            ),
        },
        "candidates": {
            "total": len(records),
            "reconciled_to_official": sum(1 for r in records if r["official"] is not None),
            "unreconciled": sum(1 for r in records if r["official"] is None),
            "unreconciled_reasons": dict(Counter(
                r["unreconciled_reason"] for r in records if r["official"] is None
            )),
        },
        "side_correctness": {
            "population": "candidates reconciled to an official Final with a resolved side",
            "decided": len(decided),
            "wins": wins,
            "losses": len(decided) - wins,
            "win_rate": round(wins / len(decided), 6) if decided else None,
            "wilson_95": [round(lo, 6), round(hi, 6)],
            "pushes": sum(1 for r in records if r["side_outcome"] == "push"),
            "side_unresolved": sum(1 for r in records if r["side_outcome"] == "side_unresolved"),
            # How much of the mapping rests on the abbreviation table, stated
            # rather than left for a reader to assume is zero.
            "side_resolution": dict(Counter(
                r["side_resolution"] for r in records if r["side_resolution"]
            )),
            "match_method": dict(Counter(r["match_method"] for r in records if r["match_method"])),
        },
        "process": {
            "schema_variant": dict(Counter(d["schema_variant"] for d in days)),
            "data_quality": dict(Counter(r["data_quality"] for r in records)),
            "price_quality": dict(Counter(r["price_quality"] for r in records)),
            "disposition": dict(Counter(r["disposition"] for r in records)),
            "floor_verdict": dict(Counter(r["floor"]["verdict"] for r in records)),
            "floor_unevaluable_reasons": dict(Counter(
                r["floor"]["reason"] for r in records if r["floor"]["verdict"] == "unevaluable"
            )),
        },
        "economics": {
            "population": "candidates marked executed",
            "executed": len(executed),
            "executed_with_stake": len(priced),
            "staked_usd": round(staked, 2),
            "executed_with_pnl": len(with_pnl),
            "pnl_usd": round(pnl, 2),
            "roi": round(pnl / pnl_staked, 6) if pnl_staked else None,
            # Same rule as the calibration buckets, for the same reason: a
            # headline ROI over a handful of settled cards is a number, not a
            # finding, and printing it unqualified is how it gets quoted.
            "roi_sufficient_for_a_claim": len(with_pnl) >= min_sample,
            "roi_population_note": (
                "ROI divides P&L by the stake of the SAME candidates that carry a "
                "P&L, not by total staked; the two populations differ whenever a "
                "wager is unsettled on the card."
            ),
        },
        "recorded_vs_official_disagreements": disagreements,
        "calibration": {
            "population": "decided candidates carrying a stated win probability",
            "bucket_width": width,
            "min_sample_for_a_claim": min_sample,
            "buckets": calibration_buckets(records, width, min_sample),
        },
    }


# ---------------------------------------------------------------------------
# Fetch + CLI
# ---------------------------------------------------------------------------


def fetch_missing_results(dates: list[str], results_dir: Path) -> list[str]:
    """Populate the results cache through the repo's own HTTP helper.

    Writes into the cache directory and nowhere else. Everything the audit
    itself does is read-only; this is the one explicitly-requested side effect,
    behind `--fetch`.
    """
    from http_util import fetch_json  # imported here so the audit path stays offline
    from datetime import datetime, timezone

    written = []
    results_dir.mkdir(parents=True, exist_ok=True)
    for date in dates:
        target = results_dir / f"{date}.json"
        if target.is_file():
            continue
        payload = fetch_json(SCHEDULE_URL.format(date=date), timeout=30, attempts=3)
        if not isinstance(payload, dict):
            print(f"WARN {date}: schedule response was not an object", file=sys.stderr)
            continue
        payload["_audit_fetched_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target.write_text(json.dumps(payload), encoding="utf-8")
        written.append(date)
    return written


def schedule_paths(execute_dir: Path, since: str | None, until: str | None) -> list[Path]:
    if not execute_dir.is_dir():
        raise AuditError(f"no schedule directory at {execute_dir}")
    paths = []
    for path in sorted(execute_dir.iterdir()):
        match = SCHEDULE_FILE_RE.match(path.name)
        if not match:
            continue
        date = match.group(1)
        if since and date < since:
            continue
        if until and date > until:
            continue
        paths.append(path)
    return paths


def render(report: dict[str, Any]) -> str:
    a = report["aggregate"]
    out: list[str] = []
    out.append(f"# MLB historical pick audit — {a['days']['total']} dated schedules")
    out.append(f"Schedules: {report['execute_dir']}")
    out.append(f"Official results cache: {report['results_dir']} (MLB Stats API schedule payloads)")
    out.append(f"Edge floor applied: {a['edge_floor']:.3f} conservative probability minus ask")
    out.append("")

    d = a["days"]
    out.append("## Days")
    out.append(f"- {d['days_with_candidates']} proposed at least one candidate; {d['days_with_a_wager']} produced a wager")
    out.append(f"- {d['no_pick_controls']} no-pick CONTROL days (slate ran, proposed nothing)")
    out.append(f"- {d['malformed_or_unreadable']} malformed/unreadable: {', '.join(str(x) for x in d['malformed_dates']) or 'none'}")
    out.append(f"- {d['note']}")
    out.append("")

    c = a["candidates"]
    out.append("## Reconciliation to official finals")
    out.append(f"- {c['reconciled_to_official']}/{c['total']} candidates matched an official Final")
    for reason, count in sorted(c["unreconciled_reasons"].items(), key=lambda kv: -kv[1]):
        out.append(f"  - {count} unreconciled: {reason}")
    out.append("")

    s = a["side_correctness"]
    out.append("## Side correctness (baseball only — says nothing about price)")
    out.append(f"- population: {s['population']}")
    if s["decided"]:
        out.append(
            f"- {s['wins']}-{s['losses']} = {s['win_rate'] * 100:.1f}% "
            f"(95% CI {s['wilson_95'][0] * 100:.1f}–{s['wilson_95'][1] * 100:.1f}%, n={s['decided']})"
        )
    else:
        out.append("- no decided candidates; no rate is reportable")
    out.append(f"- pushes {s['pushes']}, side unresolved {s['side_unresolved']}")
    out.append(f"- game matched by: {', '.join(f'{k}={v}' for k, v in sorted(s['match_method'].items()))}")
    out.append(f"- side resolved by: {', '.join(f'{k}={v}' for k, v in sorted(s['side_resolution'].items()))}")
    out.append("")

    p = a["process"]
    out.append("## Process quality (independent of whether the side won)")
    for label, counts in (
        ("schema variant (days)", p["schema_variant"]),
        ("selection-contract completeness", p["data_quality"]),
        ("price quality", p["price_quality"]),
        ("disposition", p["disposition"]),
        ("current 5-point floor verdict", p["floor_verdict"]),
    ):
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        out.append(f"- {label}: {rendered or 'none'}")
    for reason, count in sorted(p["floor_unevaluable_reasons"].items(), key=lambda kv: -kv[1]):
        out.append(f"  - unevaluable because {reason}: {count}")
    out.append("")

    e = a["economics"]
    out.append("## Economics")
    out.append(f"- population: {e['population']} ({e['executed']} candidates)")
    out.append(f"- staked ${e['staked_usd']:.2f} across {e['executed_with_stake']} with a recorded stake")
    if e["roi"] is None:
        out.append(f"- P&L ${e['pnl_usd']:+.2f} over {e['executed_with_pnl']}; ROI not computable")
    else:
        flag = "" if e["roi_sufficient_for_a_claim"] else (
            f"  [INSUFFICIENT SAMPLE — n<{a['calibration']['min_sample_for_a_claim']}, no claim]"
        )
        out.append(
            f"- P&L ${e['pnl_usd']:+.2f} over {e['executed_with_pnl']}; "
            f"ROI {e['roi'] * 100:+.1f}%{flag}"
        )
    out.append(
        f"- {e['executed'] - e['executed_with_pnl']} executed candidates carry no P&L on the "
        "card; their outcome is in the ledger, not here, so the economics population is a "
        "strict subset of the side-correctness one"
    )
    out.append(f"- {e['roi_population_note']}")
    out.append("")

    cal = a["calibration"]
    out.append("## Calibration")
    out.append(f"- population: {cal['population']}")
    if not cal["buckets"]:
        out.append("- no decided candidate carries a stated probability; calibration is not measurable")
    for bucket in cal["buckets"]:
        flag = "" if bucket["sufficient"] else f"  [INSUFFICIENT SAMPLE — n<{cal['min_sample_for_a_claim']}, no claim]"
        out.append(
            f"- stated {bucket['bucket']} (mean {bucket['mean_stated'] * 100:.1f}%): "
            f"actual {bucket['wins']}/{bucket['n']} = {bucket['actual_rate'] * 100:.1f}% "
            f"(95% CI {bucket['wilson_95'][0] * 100:.1f}–{bucket['wilson_95'][1] * 100:.1f}%){flag}"
        )
    if a["recorded_vs_official_disagreements"]:
        out.append("")
        out.append("## Card result disagrees with the official final")
        for row in a["recorded_vs_official_disagreements"]:
            out.append(f"- {row['date']} {row['game']}: card says {row['recorded']}, official says {row['official']}")
    return "\n".join(out)


def build_report(
    execute_dir: Path, results_dir: Path, floor: float, since: str | None,
    until: str | None, width: float, min_sample: int,
) -> dict[str, Any]:
    paths = schedule_paths(execute_dir, since, until)
    days = [audit_day(path, results_dir, floor) for path in paths]
    return {
        "execute_dir": str(execute_dir),
        "results_dir": str(results_dir),
        "days": days,
        "aggregate": aggregate(days, floor, width, min_sample),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only historical MLB pick audit")
    parser.add_argument("--picks-dir", help="the .picks directory (default: $SPORTS_PICKS_ROOT/.picks)")
    parser.add_argument("--results-dir", help="cache of MLB Stats API schedule payloads (default: <picks-dir>/audit-results)")
    parser.add_argument("--since", help="earliest schedule date YYYY-MM-DD")
    parser.add_argument("--until", help="latest schedule date YYYY-MM-DD")
    parser.add_argument("--edge-floor", type=float, default=DEFAULT_MIN_CONSERVATIVE_EDGE,
                        help=f"conservative edge floor (default {DEFAULT_MIN_CONSERVATIVE_EDGE}, the repo's declared default)")
    parser.add_argument("--bucket-width", type=float, default=0.05)
    parser.add_argument("--min-sample", type=int, default=20, help="calibration bucket size below which no claim is made")
    parser.add_argument("--fetch", action="store_true", help="populate missing official results into the cache")
    parser.add_argument("--json", action="store_true", help="emit the full machine-readable report")
    args = parser.parse_args(argv)

    for flag, value in (("--since", args.since), ("--until", args.until)):
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parser.error(f"{flag} must be YYYY-MM-DD")
    if not 0 < args.edge_floor < 1:
        parser.error("--edge-floor must be between 0 and 1")
    if not 0 < args.bucket_width <= 0.5:
        parser.error("--bucket-width must be in (0, 0.5]")

    if args.picks_dir:
        picks_dir = Path(args.picks_dir).expanduser().resolve()
    else:
        root = os.environ.get(PICKS_ROOT_ENV)
        if not root:
            parser.error(f"--picks-dir is required when {PICKS_ROOT_ENV} is unset")
        picks_dir = (Path(root).expanduser() / ".picks").resolve()
    execute_dir = picks_dir / "execute"
    results_dir = (
        Path(args.results_dir).expanduser().resolve() if args.results_dir else picks_dir / "audit-results"
    )

    try:
        paths = schedule_paths(execute_dir, args.since, args.until)
        if args.fetch:
            dates = [SCHEDULE_FILE_RE.match(p.name).group(1) for p in paths]
            written = fetch_missing_results(dates, results_dir)
            print(f"fetched {len(written)} of {len(dates)} dates into {results_dir}", file=sys.stderr)
        report = build_report(
            execute_dir, results_dir, args.edge_floor, args.since, args.until,
            args.bucket_width, args.min_sample,
        )
    except AuditError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
