#!/usr/bin/env python3
"""Read the refusals out of the slate PROSE, for the days that have them.

**This decides nothing.** It is a hypothesis generator, commissioned as an
explicit passenger to the recording change in ``mlb_game_reads.py``, and its
only job is to say where recording would pay. No rule it suggests may change a
gate, a threshold or a policy; any candidate rule would still have to survive
leave-one-month-out, and at this sample size it will not.

**The selection bias is the headline, not a caveat.** The population is "days
whose writeup happened to be verbose enough to parse". That is not a sample of
slates, it is a sample of writing. A refusal rate computed over it measures the
writeup's verbosity as much as the gate's behaviour, and the artifact says so
in its first section rather than its last.

Three rules this scan holds itself to, each learned the expensive way in this
repo:

- **Impute nothing.** A number the parser cannot read is recorded as unreadable
  with the pattern set that failed, never as absent and never as zero.
- **Ship the denominator.** Every count carries the population it came out of,
  and the excluded days are listed with the reason each was excluded.
- **State the scope of the lookup.** The enumeration says which directory it
  walked and how deep, because a blank rendered by a search that never looked
  there reads exactly like a blank that means nothing is there.

The rail vocabulary is IMPORTED from ``mlb_game_reads`` rather than restated,
so the classifier and the recorder cannot drift into speaking different
languages — the whole point of the passenger is to inform the recorder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_game_reads import REFUSAL_RAILS  # noqa: E402
from vig_historical_audit import team_token_match_kind  # noqa: E402

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"

# A slate file is eligible only if it carries per-game sections WITH price
# lines. A verbose narrative with no price line is not a structured read, and
# counting it would be counting the thing this scan exists to measure the
# absence of.
SECTION_RE = re.compile(r"^### (?P<heading>.+)$", re.M)
PRICE_RE = re.compile(r"\*\*Price:\*\*(?P<body>.*)", re.M)
HEADING_TEAMS_RE = re.compile(r"^(?P<away>.+?) at (?P<home>.+?)(?: — .*)?$")

# Every pattern is NAMED and the name travels with the number it produced, so
# the artifact can say how each value was obtained rather than presenting five
# different prose conventions as one clean column.
PCT = r"(\d{1,3}(?:\.\d+)?)%"
DEC = r"(0\.\d+)"
ABBR = r"([A-Z]{2,3})"

FAIR_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("fair_labelled_pct", re.compile(rf"fair\s+{ABBR}\s+{PCT}\s*/\s*{ABBR}\s+{PCT}"), "labelled"),
    ("fair_labelled_dec", re.compile(rf"fair\s+{ABBR}\s+{DEC}\s*/\s*{ABBR}\s+{DEC}"), "labelled"),
    ("fair_positional_pct", re.compile(rf"fair\s+{PCT}\s*/\s*{PCT}"), "positional"),
    ("fair_positional_dec", re.compile(rf"fair\s+{DEC}\s*/\s*{DEC}"), "positional"),
    ("fair_single_side_dec", re.compile(rf"fair\s+(away|home)\s+{DEC}"), "complement"),
    ("fair_single_side_pct", re.compile(rf"fair\s+(away|home)\s+{PCT}"), "complement"),
    ("fair_single_label_dec", re.compile(rf"de-vigged?\s+{ABBR}\s+fair\s+{DEC}"), "complement"),
    ("fair_single_label_pct", re.compile(rf"de-vigged?\s+{ABBR}\s+fair\s+{PCT}"), "complement"),
)
ASK_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("ask_labelled_pct", re.compile(rf"asks?\s+{ABBR}\s+{PCT}\s*/\s*{ABBR}\s+{PCT}"), "labelled"),
    ("ask_labelled_dec", re.compile(rf"asks?\s+{ABBR}\s+{DEC}\s*/\s*{ABBR}\s+{DEC}"), "labelled"),
    ("ask_positional_pct", re.compile(rf"asks?\s+{PCT}\s*/\s*{PCT}"), "positional"),
    ("ask_positional_dec", re.compile(rf"asks?\s+{DEC}\s*/\s*{DEC}"), "positional"),
)

# Phrase -> rail. Deliberately conservative: a phrase that could mean two rails
# is left out, because a misclassified refusal is worse than an unclassified
# one. Everything unmatched is counted and quoted verbatim in the artifact.
RAIL_PHRASES: tuple[tuple[str, str], ...] = (
    ("dk line unavailable", "no_dk_price"),
    ("draftkings unavailable", "no_dk_price"),
    ("no dk price", "no_dk_price"),
    ("no market data", "no_polymarket_market"),
    ("no executable ask", "no_polymarket_market"),
    ("no usable polymarket", "no_polymarket_market"),
    ("already underway", "game_already_started"),
    ("already in progress", "game_already_started"),
    ("market is live", "game_already_started"),
    ("game was already", "game_already_started"),
    ("run factor", "park_environment_cap"),
    ("park factor", "park_environment_cap"),
    ("hitter park", "park_environment_cap"),
    ("coors", "park_environment_cap"),
    ("ask is already below", "price_discipline"),
    ("ask is efficient", "price_discipline"),
    ("market is efficient", "price_discipline"),
    ("price is already beyond", "price_discipline"),
    ("priced through", "price_discipline"),
    ("edge below", "price_discipline"),
    ("no real edge", "price_discipline"),
    ("no clean edge", "price_discipline"),
    ("not enough conservative edge", "price_discipline"),
    ("does not leave enough", "price_discipline"),
    ("no durable conservative edge", "price_discipline"),
    ("edge floor", "price_discipline"),
    ("bullpen", "bullpen_close_game_survival"),
    ("unconfirmed lineup", "lineups_unconfirmed"),
    ("lineups unconfirmed", "lineups_unconfirmed"),
    ("starter was unannounced", "starter_unannounced"),
    ("unannounced", "starter_unannounced"),
    ("unverified opposing starter", "opposing_starter_shutdown_path"),
    ("missing opposing starter", "opposing_starter_shutdown_path"),
    ("small sample", "starter_floor"),
    ("small-sample", "starter_floor"),
    ("tiny sample", "starter_floor"),
    ("sample risk", "starter_floor"),
    ("price discipline", "price_discipline"),
    ("price is efficient", "price_discipline"),
    ("past the edge", "price_discipline"),
    ("below the authoritative", "price_discipline"),
    ("sub-rail edge", "price_discipline"),
    ("no edge", "price_discipline"),
    ("no 2% edge", "price_discipline"),
    ("no provisional 2% edge", "price_discipline"),
    ("no 2% provisional edge", "price_discipline"),
    ("extreme park", "park_environment_cap"),
    ("extreme-pitcher-park", "park_environment_cap"),
    ("extreme hitter", "park_environment_cap"),
    ("park confidence cap", "park_environment_cap"),
    ("lineups_confirmed pending", "lineups_unconfirmed"),
    ("lineup is unpublished", "lineups_unconfirmed"),
    ("waiting on lineups", "lineups_unconfirmed"),
    ("lineup context", "lineups_unconfirmed"),
    ("starter missing", "starter_unannounced"),
    ("without the opposing starter", "opposing_starter_shutdown_path"),
    ("opposing starter", "opposing_starter_shutdown_path"),
    ("missing offense", "incomplete_input_data"),
    ("offense row", "incomplete_input_data"),
    ("offense quality", "incomplete_input_data"),
    ("offense input", "incomplete_input_data"),
    ("incomplete offense", "incomplete_input_data"),
    ("data is incomplete", "incomplete_input_data"),
    ("missing away-offense", "incomplete_input_data"),
    ("no conviction", "real_winner_conviction"),
    ("not enough conviction", "real_winner_conviction"),
    ("conviction", "real_winner_conviction"),
)


def _fraction(text: str, percent: bool) -> float | None:
    """A percentage or decimal string as a probability, or None."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if percent:
        value /= 100.0
    return value if 0 < value < 1 else None


def _match_pair(
    body: str, patterns: Iterable[tuple[str, re.Pattern[str], str]]
) -> dict[str, Any] | None:
    """First named pattern that matches, with its provenance attached."""
    for name, pattern, layout in patterns:
        match = pattern.search(body)
        if not match:
            continue
        groups = list(match.groups())
        percent = name.endswith("pct")
        if layout == "labelled":
            first_label, first_raw, second_label, second_raw = groups
        elif layout == "complement":
            # A de-vigged pair sums to 1 by construction — that is what
            # de-vigging IS — so one recorded side determines the other. The
            # pattern name says the second number was derived, so no reader can
            # mistake it for something the writeup stated.
            first_label, first_raw = groups
            value = _fraction(first_raw, percent)
            if value is None:
                continue
            return {
                "pattern": name,
                "layout": "complement",
                "end": match.end(),
                "first": {"label": first_label, "value": value},
                "second": None,
            }
        else:
            first_label = second_label = None
            first_raw, second_raw = groups
        first = _fraction(first_raw, percent)
        second = _fraction(second_raw, percent)
        if first is None or second is None:
            continue
        return {
            "pattern": name,
            "layout": layout,
            "end": match.end(),
            "first": {"label": first_label, "value": first},
            "second": {"label": second_label, "value": second},
        }
    return None


def _assign_sides(pair: dict[str, Any], away: str, home: str) -> dict[str, Any] | None:
    """Turn a (first, second) pair into an away/home pair, or refuse.

    Positional lines are away-first, which is a claim this file makes and a test
    pins against the real 2026-08 corpus. A labelled line is resolved through
    the shared team-token matcher; a label that resolves to neither team, or to
    both, produces None rather than a guess. Getting a side backwards would
    invert every edge downstream, so an unresolvable label is a refusal.
    """
    if pair["layout"] == "positional":
        return {"away": pair["first"]["value"], "home": pair["second"]["value"]}

    def side_of(label: Any) -> str | None:
        if label in ("away", "home"):
            return label
        if not isinstance(label, str):
            return None
        matches = [
            name
            for name, full in (("away", away), ("home", home))
            if team_token_match_kind(label, full)
        ]
        return matches[0] if len(matches) == 1 else None

    if pair["layout"] == "complement":
        side = side_of(pair["first"]["label"])
        if side is None:
            return None
        value = pair["first"]["value"]
        other = "home" if side == "away" else "away"
        return {side: value, other: 1.0 - value}

    resolved: dict[str, float] = {}
    for key in ("first", "second"):
        side = side_of(pair[key]["label"])
        if side is None or side in resolved:
            return None
        resolved[side] = pair[key]["value"]
    return resolved if set(resolved) == {"away", "home"} else None


VERDICT_FIELD_RE = re.compile(r"\*\*(?:Pass|Gate|Verdict|Action):\*\*(?P<body>[^\n]*)")


def refusal_clause(body: str, price_body: str) -> tuple[str, str]:
    """The text that states WHY, and where it was taken from.

    Scoping this is not a nicety. Classifying over the whole section made the
    ``bullpen`` rail fire on 109 of 109 reads — because every section carries a
    ``**Bullpen:**`` STATISTICS line, not because every game was refused on the
    bullpen. That is the "measuring the carrier instead of the subject" failure
    this repo already hit in the loss-evidence slice, and a rail table where one
    rail is 100% is the shape it takes.

    So the clause is the verdict fields (``**Pass:**`` / ``**Gate:**``) plus the
    ``**Price:**`` line, and nothing else. Both of those carry refusal
    reasoning; the starter/form/bullpen lines carry statistics that happen to
    contain rail words. The price line goes in WHOLE rather than trimmed to the
    prose after the numbers, because "DK line unavailable" is stated before the
    numbers on the days where it is the whole reason.

    The source travels with the answer.
    """
    verdicts = [match.group("body").strip() for match in VERDICT_FIELD_RE.finditer(body)]
    parts = [part for part in [" ".join(verdicts).strip(), price_body.strip()] if part]
    if not parts:
        return "", "none"
    source = "verdict_field+price_line" if verdicts else "price_line"
    return " ".join(parts), source


def classify_rails(text: str) -> tuple[list[str], bool]:
    """Rails named by a refusal, and whether anything was recognised at all."""
    lowered = text.casefold()
    rails: list[str] = []
    for phrase, rail in RAIL_PHRASES:
        if phrase in lowered and rail not in rails:
            rails.append(rail)
    unknown = [rail for rail in rails if rail not in REFUSAL_RAILS]
    if unknown:  # pragma: no cover - a typo in the table, caught by its test
        raise ValueError(f"classifier emitted rails outside the shared vocabulary: {unknown}")
    return sorted(rails), bool(rails)


def parse_slate(text: str) -> list[dict[str, Any]]:
    """One record per ``###`` game section, with extraction provenance."""
    sections: list[dict[str, Any]] = []
    matches = list(SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end]
        heading = match.group("heading").strip()
        teams = HEADING_TEAMS_RE.match(heading)
        price = PRICE_RE.search(body)
        record: dict[str, Any] = {
            "heading": heading,
            "away": teams.group("away").strip() if teams else None,
            "home": teams.group("home").strip() if teams else None,
            "unreadable": [],
        }
        if not teams:
            record["unreadable"].append("teams: heading is not '<away> at <home>'")
        if not price:
            record["unreadable"].append("price: section carries no **Price:** line")
            sections.append(record)
            continue

        price_body = price.group("body")
        record["price_line"] = price_body.strip()
        for field, patterns in (("dk_fair_prob", FAIR_PATTERNS), ("polymarket_ask", ASK_PATTERNS)):
            pair = _match_pair(price_body, patterns)
            if pair is None:
                record["unreadable"].append(
                    f"{field}: no two-sided value matched "
                    f"{[name for name, _, _ in patterns]}"
                )
                continue
            record[f"{field}_pattern"] = pair["pattern"]
            sides = (
                _assign_sides(pair, record["away"], record["home"])
                if record["away"] and record["home"]
                else None
            )
            if sides is None:
                record["unreadable"].append(
                    f"{field}: matched {pair['pattern']} but the sides could not be assigned"
                )
                continue
            record[field] = sides

        clause, source = refusal_clause(body, price_body)
        record["refusal_clause"] = clause
        record["refusal_clause_source"] = source
        rails, recognised = classify_rails(clause)
        record["refusing_rails"] = rails
        if not recognised:
            record["unreadable"].append("refusing_rails: no phrase in the table matched")
            record["unclassified_text"] = " ".join(clause.split())[:400] or "(no refusal clause found)"
        sections.append(record)
    return sections


def value_side(record: dict[str, Any]) -> dict[str, Any] | None:
    """The side a price-discipline read would look at, defined mechanically.

    ``dk_fair_prob - polymarket_ask``, larger side wins, and a tie produces
    None. This is a DEFINITION chosen so it can be computed the same way every
    time, not a reconstruction of what the run was actually thinking. The
    artifact says so; treating it as the run's own chosen side would be
    inventing a decision nobody recorded.
    """
    fair = record.get("dk_fair_prob")
    ask = record.get("polymarket_ask")
    if not isinstance(fair, dict) or not isinstance(ask, dict):
        return None
    edges = {side: fair[side] - ask[side] for side in ("away", "home") if side in fair and side in ask}
    if len(edges) != 2 or edges["away"] == edges["home"]:
        return None
    side = max(edges, key=lambda key: edges[key])
    return {"side": side, "edge": edges[side], "ask": ask[side]}


def refetch_schedule(day: dt.date) -> dict[str, Any]:
    """One day's public MLB schedule, in memory, never written to the corpus."""
    import urllib.request

    url = SCHEDULE_URL.format(date=day.isoformat())
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def outcomes_for(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Final games in a schedule payload, as away/home names plus a winner."""
    finals = []
    for date_block in payload.get("dates", []):
        for game in date_block.get("games", []):
            if (game.get("status") or {}).get("detailedState") != "Final":
                continue
            teams = game.get("teams") or {}
            away, home = teams.get("away") or {}, teams.get("home") or {}
            away_score, home_score = away.get("score"), home.get("score")
            if not isinstance(away_score, int) or not isinstance(home_score, int):
                continue
            if away_score == home_score:
                continue
            finals.append(
                {
                    "game_pk": game.get("gamePk"),
                    "away": ((away.get("team") or {}).get("name")),
                    "home": ((home.get("team") or {}).get("name")),
                    "away_score": away_score,
                    "home_score": home_score,
                    "winner": "away" if away_score > home_score else "home",
                }
            )
    return finals


def attach_outcome(record: dict[str, Any], finals: list[dict[str, Any]]) -> None:
    """Match a parsed section to a final game, or say why it could not be."""
    if not record.get("away") or not record.get("home"):
        record["outcome_known"] = False
        record["outcome_reason"] = "the section's teams could not be read"
        return
    matches = [
        game
        for game in finals
        if team_token_match_kind(record["away"], game["away"] or "")
        and team_token_match_kind(record["home"], game["home"] or "")
    ]
    if len(matches) != 1:
        record["outcome_known"] = False
        record["outcome_reason"] = (
            f"{len(matches)} final games match {record['away']!r} at {record['home']!r}; "
            "a doubleheader or a name mismatch cannot be resolved from the writeup alone"
        )
        return
    game = matches[0]
    record["outcome_known"] = True
    record["outcome_winner"] = game["winner"]
    record["outcome_score"] = (
        f"{game['away']} {game['away_score']} at {game['home']} {game['home_score']}"
    )


def home_relative(path: Path) -> str:
    """``str(path)`` with the running account's home replaced by ``~``.

    A committed artifact that bakes in an absolute home path is caught by this
    repo's own ``test_deployed_scripts_and_docs_have_no_baked_in_home``, and it
    caught the drought lane's first artifact. Honour the guard rather than
    widen it; the substitution is declared in the report.
    """
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def eligible_files(slate_dir: Path, start: dt.date, end: dt.date) -> dict[str, Any]:
    """Which slate files are in the window, which are eligible, and why not.

    Scope, stated because a blank from a search that never looked there reads
    like a blank that means nothing is there: this walks ``slate_dir`` ONE level
    (``*.md``), because a file nested under a lane subdirectory belongs to
    another sport's lane and classifying it as an MLB read would be the mirror
    of the drought report's own defect. Nested files are listed as skipped, with
    their paths, rather than being invisible.
    """
    considered: list[dict[str, Any]] = []
    for path in sorted(slate_dir.rglob("*.md")):
        stem = path.stem
        date_text = stem[:10]
        try:
            day = dt.date.fromisoformat(date_text)
        except ValueError:
            continue
        if not start <= day <= end:
            continue
        entry: dict[str, Any] = {
            "path": path.relative_to(slate_dir).as_posix(),
            "date": day.isoformat(),
            "nested": path.parent != slate_dir,
        }
        if entry["nested"]:
            entry["eligible"] = False
            entry["reason"] = "nested under a lane subdirectory; not the MLB slate lane"
            considered.append(entry)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        sections = len(SECTION_RE.findall(text))
        priced = len(PRICE_RE.findall(text))
        entry["sections"] = sections
        entry["price_lines"] = priced
        entry["eligible"] = priced > 0
        if not entry["eligible"]:
            entry["reason"] = (
                f"{sections} game section(s) and {priced} price line(s): the day was narrated, "
                "not read out per game"
            )
        considered.append(entry)
    return {
        "scope": (
            f"{home_relative(slate_dir)} walked recursively for <date>*.md; only files at the "
            "top level are treated as the MLB slate lane. Paths are rendered with the running "
            "account's home directory replaced by ~."
        ),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "files": considered,
    }


def scan(
    slate_dir: Path,
    start: dt.date,
    end: dt.date,
    fetch_outcomes: bool = False,
    corpus_description: str | None = None,
) -> dict[str, Any]:
    enumeration = eligible_files(slate_dir, start, end)
    eligible = [entry for entry in enumeration["files"] if entry.get("eligible")]
    days: list[dict[str, Any]] = []
    for entry in eligible:
        path = slate_dir / entry["path"]
        records = parse_slate(path.read_text(encoding="utf-8", errors="replace"))
        if fetch_outcomes:
            finals = outcomes_for(refetch_schedule(dt.date.fromisoformat(entry["date"])))
            for record in records:
                attach_outcome(record, finals)
        days.append({"date": entry["date"], "file": entry["path"], "reads": records})

    reads = [record for day in days for record in day["reads"]]
    field_failures: dict[str, int] = {}
    for record in reads:
        for note in record["unreadable"]:
            field = note.split(":", 1)[0]
            field_failures[field] = field_failures.get(field, 0) + 1

    by_rail: dict[str, dict[str, Any]] = {}
    for rail in sorted(REFUSAL_RAILS):
        by_rail[rail] = {
            "reads_naming_this_rail": 0,
            "value_side_resolved": 0,
            "value_side_won": 0,
            "value_side_outcome_unknown": 0,
        }
    for record in reads:
        side = value_side(record)
        for rail in record.get("refusing_rails", []):
            bucket = by_rail[rail]
            bucket["reads_naming_this_rail"] += 1
            if side is None:
                continue
            bucket["value_side_resolved"] += 1
            if not record.get("outcome_known"):
                bucket["value_side_outcome_unknown"] += 1
            elif record.get("outcome_winner") == side["side"]:
                bucket["value_side_won"] += 1

    # The base rate, shipped next to the per-rail numbers on purpose. A rail
    # table read without it invites exactly the base-rate fallacy this lane
    # already committed once (PR #63's withdrawn "favorites >=0.55 leak"): a
    # bucket where the value side won 14 of 26 is only interesting relative to
    # how often the value side won at all.
    resolved_reads = [r for r in reads if value_side(r) and r.get("outcome_known")]
    overall = {
        "reads_with_value_side_and_outcome": len(resolved_reads),
        "value_side_won": sum(
            1 for r in resolved_reads if r.get("outcome_winner") == value_side(r)["side"]
        ),
    }

    unclassified = [
        {"date": day["date"], "heading": record["heading"], "text": record.get("unclassified_text")}
        for day in days
        for record in day["reads"]
        if record.get("unclassified_text")
    ]
    return {
        "what_this_is": (
            "A hypothesis generator over the days whose slate prose happened to be verbose "
            "enough to parse. It decides nothing: no rule it suggests may change a gate, a "
            "threshold or a policy, and any candidate rule would still have to survive "
            "leave-one-month-out."
        ),
        "selection_bias": (
            f"{len(eligible)} of {len(enumeration['files'])} slate files in the window carry "
            "per-game price lines. The population is days that were WRITTEN a certain way, not "
            "a sample of slates, so every rate below is confounded with how tersely a given "
            "run wrote. This is the blind spot the game_reads recording change exists to close, "
            "and it cannot be corrected for here."
        ),
        "corpus": corpus_description
        or "not stated on the command line; the reader cannot check what was scanned",
        "enumeration": enumeration,
        "counts": {
            "files_in_window": len(enumeration["files"]),
            "files_eligible": len(eligible),
            "game_sections_parsed": len(reads),
            "reads_with_a_classified_rail": sum(1 for r in reads if r.get("refusing_rails")),
            "reads_with_no_classified_rail": len(unclassified),
            "reads_with_a_resolvable_value_side": sum(1 for r in reads if value_side(r)),
            "outcomes_attached": sum(1 for r in reads if r.get("outcome_known")),
        },
        "extraction_failures_by_field": dict(sorted(field_failures.items())),
        "overall": overall,
        "rails_are_not_mutually_exclusive": (
            "A read may name more than one rail, so the per-rail counts sum to more than the "
            "number of reads. They are not a partition."
        ),
        "by_rail": by_rail,
        "unclassified_refusals": unclassified,
        "days": days,
    }


def render(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# MLB refusal hypothesis scan — slate prose",
        "",
        "## This decides nothing",
        "",
        report["what_this_is"],
        "",
        "## Selection bias",
        "",
        report["selection_bias"],
        "",
        f"Enumeration scope: {report['enumeration']['scope']}",
        "",
        f"Corpus: {report['corpus']}",
        "",
        "## Population",
        "",
        f"- slate files in window: **{counts['files_in_window']}**",
        f"- eligible (carry per-game price lines): **{counts['files_eligible']}**",
        f"- game sections parsed: **{counts['game_sections_parsed']}**",
        f"- sections with a classified rail: **{counts['reads_with_a_classified_rail']}**",
        f"- sections with no classified rail: **{counts['reads_with_no_classified_rail']}**",
        f"- sections with a resolvable value side: "
        f"**{counts['reads_with_a_resolvable_value_side']}**",
        f"- sections with an attached outcome: **{counts['outcomes_attached']}**",
        "",
        "## Extraction failures, by field",
        "",
        "Nothing here is imputed. A field the parser could not read is counted, not filled in.",
        "",
        "| field | sections it could not be read from |",
        "|---|---|",
    ]
    for field, count in report["extraction_failures_by_field"].items():
        lines.append(f"| `{field}` | {count} |")
    if not report["extraction_failures_by_field"]:
        lines.append("| — | 0 |")
    lines += [
        "",
        "## By rail",
        "",
        "The value side is defined mechanically as the larger of "
        "`dk_fair_prob - polymarket_ask`. It is NOT a reconstruction of the side the run was "
        "considering — nobody recorded that — and every zero below is a real zero over the "
        "population above, not a missing number.",
        "",
        f"Base rate over the same population: the value side won "
        f"**{report['overall']['value_side_won']} of "
        f"{report['overall']['reads_with_value_side_and_outcome']}** reads that have both a "
        f"resolvable value side and a known outcome. Compare every row below against that, not "
        f"against 50%.",
        "",
        report["rails_are_not_mutually_exclusive"],
        "",
        "| rail | sections naming it | value side resolved | value side won | outcome unknown |",
        "|---|---|---|---|---|",
    ]
    for rail, bucket in report["by_rail"].items():
        lines.append(
            f"| `{rail}` | {bucket['reads_naming_this_rail']} | {bucket['value_side_resolved']} "
            f"| {bucket['value_side_won']} | {bucket['value_side_outcome_unknown']} |"
        )
    lines += ["", "## Refusals the classifier did not recognise", ""]
    if report["unclassified_refusals"]:
        lines.append(
            "Quoted verbatim so the reader can see what the vocabulary missed, rather than "
            "having it disappear into an `other` bucket."
        )
        lines.append("")
        for item in report["unclassified_refusals"]:
            lines.append(f"- **{item['date']} — {item['heading']}**: {item['text']}")
    else:
        lines.append("None over this population.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slate-dir", type=Path, required=True)
    parser.add_argument("--start", required=True, help="first date in the window, YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="last date in the window, YYYY-MM-DD")
    parser.add_argument(
        "--fetch-outcomes",
        action="store_true",
        help="fetch final scores from the public MLB schedule (network, in memory only)",
    )
    parser.add_argument(
        "--corpus-description",
        help="where the slate files came from, carried verbatim into the artifact",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)

    try:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.until)
    except ValueError as exc:
        parser.error(str(exc))
    if end < start:
        parser.error("--until must not be before --start")

    report = scan(
        args.slate_dir,
        start,
        end,
        fetch_outcomes=args.fetch_outcomes,
        corpus_description=args.corpus_description,
    )
    markdown = render(report)
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if not args.json_out and not args.markdown_out:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
