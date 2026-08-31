#!/usr/bin/env python3
"""Deterministic MLB postgame evidence collection and process grading.

Phase 5 of the MLB pick-process hardening plan gives settlement a causal
learning loop. Before reflection, a deterministic collector pulls the
boxscore/game-script evidence for the settled game (starter and bulk-pitcher
lines, expected-vs-actual role, bullpen usage, offense conversion, scoring
sequence) from the MLB Stats API. The reviewer then writes a structured
``process_grade`` that grades each stated thesis pillar
``held | failed | mixed | unknown``, and a hard validator enforces the
contract:

- a loss can never be graded variance without complete pillar evidence;
- pillars the boxscore decides deterministically cannot be overridden;
- missing postgame evidence forces ``insufficient_evidence``, never
  "I would assign it again".

Usage:
  python scripts/mlb_postgame_evidence.py collect --game-pk 824240 --team "Detroit Tigers"
  python scripts/mlb_postgame_evidence.py collect --date 2026-08-11 --team "Detroit Tigers"
  python scripts/mlb_postgame_evidence.py grade --pick pick.json --evidence evidence.json

``collect`` prints the postgame_evidence JSON; it exits non-zero when the
evidence is insufficient so scripted settlement fails loud. ``grade``
validates ``pick["process_grade"]`` against the collected evidence and exits
non-zero with the error list on any violation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from http_util import fetch_json  # noqa: E402

FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"

PILLARS = (
    "starter_role",
    "starter_quality",
    "bullpen_availability",
    "offense_conversion",
    "named_risk",
)
PILLAR_GRADES = frozenset({"held", "failed", "mixed", "unknown"})

BAD_READ_BY_PILLAR = {
    "starter_role": "bad_read_starter_role",
    "starter_quality": "bad_read_starter_quality",
    "bullpen_availability": "bad_read_bullpen_availability",
    "offense_conversion": "bad_read_offense_conversion",
    "named_risk": "bad_read_named_risk",
}

PROCESS_GRADES = frozenset(
    {
        "good_read_bad_variance",
        "good_read_edge_held",
        "good_read_execution_issue",
        "insufficient_evidence",
        *BAD_READ_BY_PILLAR.values(),
    }
)

# Deterministic actual-role thresholds (outs recorded by the first pitcher).
_STARTER_MIN_OUTS = 12  # 4.0 IP
_OPENER_MAX_OUTS = 6  # 2.0 IP
_BULK_MIN_OUTS = 9  # 3.0 IP by a single subsequent reliever

_PITCHER_LINE_STATS = (
    ("hits", "hits"),
    ("runs", "runs"),
    ("earned_runs", "earnedRuns"),
    ("strikeouts", "strikeOuts"),
    ("walks", "baseOnBalls"),
    ("home_runs", "homeRuns"),
    ("pitches", "numberOfPitches"),
    ("batters_faced", "battersFaced"),
)

_OFFENSE_STATS = (
    ("runs", "runs"),
    ("hits", "hits"),
    ("walks", "baseOnBalls"),
    ("strikeouts", "strikeOuts"),
    ("home_runs", "homeRuns"),
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# The expected roles `auto_pillar_grades` can grade `starter_role` against.
# Anything else is a role the grader does not know and grades `unknown`.
EXPECTED_STARTER_ROLES = frozenset({"starter", "opener", "bulk", "piggyback"})


def usable_expected_ip(value: Any) -> bool:
    """Whether `expected_ip` is a value the grader can grade against.

    Exported because two callers need this predicate and a second copy of it
    would agree with the grader exactly until the day one of them moved: the
    grader gates `starter_quality` on it, and the analysis layer's coverage
    counts must not call an input `recorded` in the same breath the grader
    calls the pillar it feeds `unknown`.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def ip_to_outs(innings_pitched: Any) -> int | None:
    """Convert an ``inningsPitched`` string like ``"5.2"`` to outs (17)."""
    if not isinstance(innings_pitched, str):
        return None
    match = re.fullmatch(r"(\d+)\.([012])", innings_pitched)
    if not match:
        return None
    return int(match.group(1)) * 3 + int(match.group(2))


def _pitcher_outs(stats: dict[str, Any]) -> int | None:
    outs = stats.get("outs")
    if _is_int(outs) and outs >= 0:
        return outs
    return ip_to_outs(stats.get("inningsPitched"))


def _pitcher_line(player: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(player, dict):
        return None
    stats = player.get("stats", {}).get("pitching")
    if not isinstance(stats, dict) or not stats:
        return None
    line: dict[str, Any] = {
        "name": player.get("person", {}).get("fullName"),
        "innings_pitched": stats.get("inningsPitched"),
        "outs": _pitcher_outs(stats),
    }
    for key, source in _PITCHER_LINE_STATS:
        value = stats.get(source)
        line[key] = value if _is_int(value) else None
    return line


def classify_actual_role(pitcher_lines: list[dict[str, Any]]) -> str:
    """Classify how the first pitcher's outing actually played out.

    Returns one of ``starter | opener_bulk | short_start | unknown``:
    - ``starter``: first pitcher recorded >= 12 outs (4+ IP)
    - ``opener_bulk``: first pitcher <= 6 outs and a single subsequent
      reliever recorded >= 9 outs (the bulk pattern)
    - ``short_start``: anything shorter than a conventional start that does
      not match the opener/bulk pattern
    - ``unknown``: no usable pitching line
    """
    if not pitcher_lines:
        return "unknown"
    first_outs = pitcher_lines[0].get("outs")
    if not _is_int(first_outs):
        return "unknown"
    if first_outs >= _STARTER_MIN_OUTS:
        return "starter"
    if first_outs <= _OPENER_MAX_OUTS:
        for line in pitcher_lines[1:]:
            outs = line.get("outs")
            if _is_int(outs) and outs >= _BULK_MIN_OUTS:
                return "opener_bulk"
    return "short_start"


def _collect_side(
    box_side: Any, side: str, reasons: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract pitching lines and offense totals for one boxscore side."""
    pitching: dict[str, Any] = {
        "starter": None,
        "relievers": [],
        "actual_role": "unknown",
    }
    offense: dict[str, Any] = {}
    if not isinstance(box_side, dict):
        reasons.append(f"boxscore missing {side} team")
        return pitching, offense

    pitcher_ids = box_side.get("pitchers")
    players = box_side.get("players")
    lines: list[dict[str, Any]] = []
    if not isinstance(pitcher_ids, list) or not pitcher_ids or not isinstance(players, dict):
        reasons.append(f"boxscore missing {side} pitcher appearances")
    else:
        for pid in pitcher_ids:
            line = _pitcher_line(players.get(f"ID{pid}", {}))
            if line is None:
                reasons.append(f"boxscore missing pitching line for {side} pitcher {pid}")
                lines = []
                break
            lines.append(line)
    if lines:
        pitching["starter"] = lines[0]
        pitching["relievers"] = lines[1:]
        pitching["actual_role"] = classify_actual_role(lines)
        if pitching["actual_role"] == "unknown":
            reasons.append(f"{side} starter outs unavailable; actual role unknown")

    batting = box_side.get("teamStats", {}).get("batting")
    if not isinstance(batting, dict):
        reasons.append(f"boxscore missing {side} team batting totals")
    else:
        for key, source in _OFFENSE_STATS:
            value = batting.get(source)
            if _is_int(value):
                offense[key] = value
            else:
                reasons.append(f"boxscore missing {side} batting {source}")
    return pitching, offense


def _scoring_plays(plays: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(plays, dict) or not isinstance(plays.get("allPlays"), list):
        return [], False
    rows: list[dict[str, Any]] = []
    for play in plays["allPlays"]:
        if not isinstance(play, dict):
            continue
        about = play.get("about", {})
        if not about.get("isScoringPlay"):
            continue
        result = play.get("result", {})
        rows.append(
            {
                "inning": about.get("inning"),
                "half": about.get("halfInning"),
                "event": result.get("event"),
                "description": result.get("description"),
                "away_score": result.get("awayScore"),
                "home_score": result.get("homeScore"),
            }
        )
    return rows, True


def collect_postgame_evidence(feed: Any) -> dict[str, Any]:
    """Build the structured postgame_evidence object from a feed/live payload.

    Fail-loud contract: any missing or malformed required input adds a reason
    and flips ``evidence_status`` to ``insufficient``. The scoring sequence is
    optional context (``scoring_plays_available``) and never blocks grading on
    its own.
    """
    reasons: list[str] = []
    if not isinstance(feed, dict):
        return {
            "evidence_status": "insufficient",
            "insufficient_reasons": ["feed/live payload is not an object"],
        }

    game_data = feed.get("gameData", {}) if isinstance(feed.get("gameData"), dict) else {}
    live_data = feed.get("liveData", {}) if isinstance(feed.get("liveData"), dict) else {}

    status = game_data.get("status", {}).get("detailedState")
    if status != "Final":
        reasons.append(f"game status is {status!r}, not Final")

    teams = game_data.get("teams", {}) if isinstance(game_data.get("teams"), dict) else {}
    away_name = teams.get("away", {}).get("name")
    home_name = teams.get("home", {}).get("name")
    if not away_name or not home_name:
        reasons.append("gameData missing team names")

    box_teams = live_data.get("boxscore", {}).get("teams", {})
    if not isinstance(box_teams, dict):
        box_teams = {}
        reasons.append("feed missing boxscore")
    away_pitching, away_offense = _collect_side(box_teams.get("away"), "away", reasons)
    home_pitching, home_offense = _collect_side(box_teams.get("home"), "home", reasons)

    linescore_teams = live_data.get("linescore", {}).get("teams", {})
    away_score = linescore_teams.get("away", {}).get("runs")
    home_score = linescore_teams.get("home", {}).get("runs")
    if not _is_int(away_score):
        away_score = away_offense.get("runs")
    if not _is_int(home_score):
        home_score = home_offense.get("runs")
    winner: str | None = None
    if _is_int(away_score) and _is_int(home_score):
        if away_score > home_score:
            winner = away_name
        elif home_score > away_score:
            winner = home_name
    else:
        reasons.append("final score unavailable")

    scoring_plays, plays_available = _scoring_plays(live_data.get("plays"))

    return {
        "evidence_status": "insufficient" if reasons else "complete",
        "insufficient_reasons": reasons,
        "game_pk": feed.get("gamePk"),
        "date": game_data.get("datetime", {}).get("officialDate"),
        "status": status,
        "away": away_name,
        "home": home_name,
        "away_score": away_score if _is_int(away_score) else None,
        "home_score": home_score if _is_int(home_score) else None,
        "winner": winner,
        "pitching": {"away": away_pitching, "home": home_pitching},
        "offense": {"away": away_offense, "home": home_offense},
        "scoring_plays_available": plays_available,
        "scoring_plays": scoring_plays,
    }


def side_for_team(evidence: dict[str, Any], team: Any) -> str | None:
    """Return 'away'/'home' for an exact team-name match, else None."""
    if not isinstance(team, str) or not team.strip():
        return None
    if team == evidence.get("away"):
        return "away"
    if team == evidence.get("home"):
        return "home"
    return None


def _grade(grade: str, evidence: str) -> dict[str, str]:
    return {"grade": grade, "evidence": evidence, "source": "deterministic"}


def auto_pillar_grades(
    baseball_evidence: Any, postgame: dict[str, Any], team: str
) -> dict[str, dict[str, str]]:
    """Deterministic pillar grades from bet-time evidence + postgame numbers.

    ``held`` and ``failed`` results are authoritative — the reviewer cannot
    override them. ``mixed`` invites reviewer refinement (with written
    evidence) and ``unknown`` requires reviewer judgment.

    Raises ValueError when ``team`` does not exactly match either side of the
    collected evidence.
    """
    side = side_for_team(postgame, team)
    if side is None:
        raise ValueError(
            f"team {team!r} does not match either side of the postgame evidence "
            f"({postgame.get('away')!r} / {postgame.get('home')!r})"
        )
    if postgame.get("evidence_status") != "complete":
        reason = "postgame evidence is insufficient: " + "; ".join(
            postgame.get("insufficient_reasons") or ["no reasons recorded"]
        )
        return {pillar: _grade("unknown", reason) for pillar in PILLARS}

    bet_evidence = baseball_evidence if isinstance(baseball_evidence, dict) else {}
    pitching = postgame["pitching"][side]
    offense = postgame["offense"][side]
    starter = pitching.get("starter") or {}
    relievers = pitching.get("relievers") or []
    grades: dict[str, dict[str, str]] = {}

    # --- starter_role: expected role at bet time vs the actual game script ---
    expected_role = bet_evidence.get("starter_role")
    actual_role = pitching.get("actual_role", "unknown")
    role_detail = (
        f"expected {expected_role!r}; actual first pitcher recorded "
        f"{starter.get('outs')} outs, classified {actual_role!r}"
    )
    if actual_role == "unknown" or expected_role not in EXPECTED_STARTER_ROLES:
        grades["starter_role"] = _grade("unknown", role_detail)
    elif expected_role == "starter":
        if actual_role == "starter":
            grades["starter_role"] = _grade("held", role_detail)
        elif actual_role == "opener_bulk":
            grades["starter_role"] = _grade(
                "failed", f"opener/bulk misclassification: {role_detail}"
            )
        else:
            grades["starter_role"] = _grade("mixed", f"early exit: {role_detail}")
    else:  # expected opener/bulk/piggyback
        if actual_role == "opener_bulk":
            grades["starter_role"] = _grade("held", role_detail)
        else:
            grades["starter_role"] = _grade("mixed", role_detail)

    # --- starter_quality: actual line vs the expected-innings floor ---
    expected_ip = bet_evidence.get("expected_ip")
    outs = starter.get("outs")
    earned_runs = starter.get("earned_runs")
    if (
        not usable_expected_ip(expected_ip)
        or not _is_int(outs)
        or not _is_int(earned_runs)
    ):
        grades["starter_quality"] = _grade(
            "unknown", "expected_ip or the actual starter line is unavailable"
        )
    else:
        expected_outs = round(expected_ip * 3)
        quality_detail = (
            f"expected {expected_ip} IP; actual {starter.get('innings_pitched')} IP "
            f"({outs} outs), {earned_runs} ER"
        )
        if outs < 0.6 * expected_outs or earned_runs >= 5:
            grades["starter_quality"] = _grade("failed", quality_detail)
        elif outs >= expected_outs - 3 and earned_runs <= 3:
            grades["starter_quality"] = _grade("held", quality_detail)
        else:
            grades["starter_quality"] = _grade("mixed", quality_detail)

    # --- bullpen_availability: runs allowed by our relievers ---
    reliever_runs = [line.get("runs") for line in relievers]
    if any(not _is_int(runs) for runs in reliever_runs):
        grades["bullpen_availability"] = _grade("unknown", "reliever runs unavailable")
    else:
        total = sum(reliever_runs)
        usage = ", ".join(
            f"{line.get('name')} {line.get('innings_pitched')} IP/{line.get('runs')} R"
            for line in relievers
        )
        bullpen_detail = (
            f"{len(relievers)} relievers allowed {total} runs ({usage})"
            if relievers
            else "no relievers used"
        )
        if total <= 1:
            grades["bullpen_availability"] = _grade("held", bullpen_detail)
        elif total >= 4:
            grades["bullpen_availability"] = _grade("failed", bullpen_detail)
        else:
            grades["bullpen_availability"] = _grade("mixed", bullpen_detail)

    # --- offense_conversion: did our side actually score? ---
    runs = offense.get("runs")
    if not _is_int(runs):
        grades["offense_conversion"] = _grade("unknown", "team runs unavailable")
    else:
        offense_detail = (
            f"scored {runs} runs on {offense.get('hits')} hits, "
            f"{offense.get('walks')} walks, {offense.get('home_runs')} HR"
        )
        if runs >= 4:
            grades["offense_conversion"] = _grade("held", offense_detail)
        elif runs <= 2:
            grades["offense_conversion"] = _grade("failed", offense_detail)
        else:
            grades["offense_conversion"] = _grade("mixed", offense_detail)

    # --- named_risk: whether a bet-time named risk materialized is judgment ---
    named_risks = bet_evidence.get("named_risks")
    if isinstance(named_risks, list) and not named_risks:
        grades["named_risk"] = _grade("held", "no named risks recorded at bet time")
    else:
        names = (
            ", ".join(str(r.get("name")) for r in named_risks if isinstance(r, dict))
            if isinstance(named_risks, list)
            else "unrecorded"
        )
        grades["named_risk"] = _grade(
            "unknown",
            f"reviewer must grade whether the bet-time named risks materialized ({names})",
        )
    return grades


def derive_process_grade(
    result: str, pillar_grades: dict[str, Any], evidence_status: str
) -> str:
    """Derive the process_grade from fully resolved pillar grades."""
    if evidence_status != "complete":
        return "insufficient_evidence"
    grades = {pillar: (pillar_grades.get(pillar) or {}).get("grade") for pillar in PILLARS}
    if any(grade == "mixed" for grade in grades.values()):
        raise ValueError(
            "resolve mixed pillar grades to held or failed (with evidence) before deriving"
        )
    if any(grade == "unknown" for grade in grades.values()):
        return "insufficient_evidence"
    for pillar in PILLARS:
        if grades[pillar] == "failed":
            return BAD_READ_BY_PILLAR[pillar]
    return "good_read_bad_variance" if result == "loss" else "good_read_edge_held"


def validate_process_grade(
    grade_obj: Any,
    *,
    result: Any,
    baseball_evidence: Any,
    postgame_evidence: Any,
    team: Any,
) -> list[str]:
    """Return hard-failure messages for a reviewer-written process_grade."""
    errors: list[str] = []
    if not isinstance(grade_obj, dict):
        return ["process_grade must be an object"]
    if not isinstance(postgame_evidence, dict):
        return ["postgame_evidence must be an object (run the collector first)"]

    process_grade = grade_obj.get("process_grade")
    if process_grade not in PROCESS_GRADES:
        errors.append(f"process_grade must be one of {sorted(PROCESS_GRADES)}")

    if result not in {"win", "loss"}:
        errors.append("result must be 'win' or 'loss' to grade the process")

    pillars = grade_obj.get("pillars")
    if not isinstance(pillars, dict):
        errors.append("pillars must be an object grading every thesis pillar")
        return errors
    missing = [pillar for pillar in PILLARS if pillar not in pillars]
    if missing:
        errors.append(f"pillars missing required entries: {', '.join(missing)}")
    unexpected = [key for key in pillars if key not in PILLARS]
    if unexpected:
        errors.append(f"pillars contains unknown entries: {', '.join(sorted(unexpected))}")
    if errors:
        return errors

    for pillar in PILLARS:
        entry = pillars[pillar]
        if not isinstance(entry, dict) or entry.get("grade") not in PILLAR_GRADES:
            errors.append(f"pillars.{pillar}.grade must be one of {sorted(PILLAR_GRADES)}")
            continue
        evidence_text = entry.get("evidence")
        if not isinstance(evidence_text, str) or not evidence_text.strip():
            errors.append(f"pillars.{pillar} requires non-empty written evidence")

    # Deterministic postgame grades are authoritative for held/failed.
    try:
        auto = auto_pillar_grades(baseball_evidence, postgame_evidence, team)
    except ValueError as exc:
        errors.append(str(exc))
        auto = {}
    for pillar, auto_entry in auto.items():
        auto_grade = auto_entry["grade"]
        entry = pillars.get(pillar)
        reviewer_grade = entry.get("grade") if isinstance(entry, dict) else None
        if auto_grade in {"held", "failed"} and reviewer_grade in PILLAR_GRADES:
            if reviewer_grade != auto_grade:
                errors.append(
                    f"pillars.{pillar}: deterministic postgame grade is '{auto_grade}' "
                    f"({auto_entry['evidence']}); reviewer grade '{reviewer_grade}' is not allowed"
                )

    final = {pillar: pillars[pillar].get("grade") for pillar in PILLARS if isinstance(pillars.get(pillar), dict)}

    if postgame_evidence.get("evidence_status") != "complete":
        if process_grade != "insufficient_evidence":
            errors.append(
                "postgame evidence is insufficient; process_grade must be "
                "'insufficient_evidence' — a loss cannot be graded variance without evidence"
            )
        return errors

    if process_grade == "insufficient_evidence":
        if not any(grade == "unknown" for grade in final.values()):
            errors.append(
                "insufficient_evidence is not allowed when postgame evidence is complete "
                "and every pillar is graded"
            )
    elif process_grade in {"good_read_bad_variance", "good_read_edge_held", "good_read_execution_issue"}:
        not_held = [pillar for pillar, grade in final.items() if grade != "held"]
        if not_held:
            errors.append(
                f"{process_grade} requires every pillar graded 'held' with evidence; "
                f"not held: {', '.join(not_held)}"
            )
        if process_grade == "good_read_bad_variance" and result == "win":
            errors.append("good_read_bad_variance is a loss grade; use good_read_edge_held for a win")
        if process_grade == "good_read_edge_held" and result == "loss":
            errors.append("good_read_edge_held is a win grade; use good_read_bad_variance for a loss")
        if process_grade == "good_read_execution_issue":
            issue = grade_obj.get("execution_issue")
            if not isinstance(issue, str) or not issue.strip():
                errors.append("good_read_execution_issue requires a written execution_issue")
    else:  # bad_read_*
        pillar = next(p for p, g in BAD_READ_BY_PILLAR.items() if g == process_grade)
        if final.get(pillar) != "failed":
            errors.append(
                f"{process_grade} requires pillars.{pillar} graded 'failed' "
                f"(got '{final.get(pillar)}')"
            )
    return errors


def postgame_prompt_section() -> str:
    """Process-grading contract text for the Vig settlement prompt."""
    return """\
POSTGAME EVIDENCE + PROCESS GRADE (required for EVERY settled MLB pick):
Before writing any reflection, collect deterministic game-script evidence:
  python3 scripts/mlb_postgame_evidence.py collect --game-pk <gamePk> --team "<backed team>" > /tmp/postgame_<pick_id>.json
(or --date YYYY-MM-DD --team "<backed team>" to resolve the gamePk; a doubleheader fails
loud and lists both gamePks). The collector exits non-zero when evidence is insufficient.

Then write a structured `process_grade` object on the ledger row:
- process_grade: good_read_bad_variance | good_read_edge_held | good_read_execution_issue |
  bad_read_starter_role | bad_read_starter_quality | bad_read_bullpen_availability |
  bad_read_offense_conversion | bad_read_named_risk | insufficient_evidence
- pillars: an object grading ALL of starter_role, starter_quality, bullpen_availability,
  offense_conversion, named_risk — each {grade: held|failed|mixed|unknown, evidence: "<why,
  citing the collected numbers>"}.
- execution_issue: required text when process_grade is good_read_execution_issue.

Validate before finishing (write the ledger row to a temp file first):
  python3 scripts/mlb_postgame_evidence.py grade --pick /tmp/pick_<pick_id>.json --evidence /tmp/postgame_<pick_id>.json
A non-zero exit means the grade violates the contract — fix the grade, never the evidence.
Hard rules the validator enforces:
- Grades the boxscore decides deterministically (held/failed) cannot be overridden.
- A loss is 'good_read_bad_variance' ONLY when every pillar is graded held with evidence.
- If the collector reports insufficient evidence, the ONLY valid grade is
  insufficient_evidence: mark the review pending, do NOT settle the reflection as variance,
  and do NOT write "I would assign it again".
- Wins are graded too: good_read_edge_held only if every pillar held; a win carried by
  variance still gets its bad_read_* grade.
Promote a durable rule to PROCESS.md only after a repeated or structural failure; match-
specific detail stays in REFLECTIONS.md. The one-line Telegram reflection must cite the
process_grade verbatim."""


def _resolve_game_pk(date: str, team: str) -> int:
    schedule = fetch_json(SCHEDULE_URL.format(date=date), timeout=30, attempts=3)
    matches: list[int] = []
    for date_block in schedule.get("dates", []) if isinstance(schedule, dict) else []:
        for game in date_block.get("games", []):
            teams = game.get("teams", {})
            names = {
                teams.get("away", {}).get("team", {}).get("name"),
                teams.get("home", {}).get("team", {}).get("name"),
            }
            if team in names:
                matches.append(game.get("gamePk"))
    if not matches:
        raise SystemExit(f"no game found for {team!r} on {date}")
    if len(matches) > 1:
        raise SystemExit(
            f"multiple games for {team!r} on {date} (gamePks: {matches}); pass --game-pk"
        )
    return matches[0]


def _cmd_collect(args: argparse.Namespace) -> int:
    game_pk = args.game_pk
    if game_pk is None:
        game_pk = _resolve_game_pk(args.date, args.team)
    feed = fetch_json(FEED_URL.format(game_pk=game_pk), timeout=30, attempts=3)
    evidence = collect_postgame_evidence(feed)
    if args.team:
        evidence["team"] = args.team
        evidence["our_side"] = side_for_team(evidence, args.team)
        if evidence["our_side"] is None:
            evidence["evidence_status"] = "insufficient"
            evidence.setdefault("insufficient_reasons", []).append(
                f"--team {args.team!r} does not match either side"
            )
    print(json.dumps(evidence, indent=2))
    return 0 if evidence.get("evidence_status") == "complete" else 1


def _cmd_grade(args: argparse.Namespace) -> int:
    pick = json.loads(Path(args.pick).read_text())
    evidence = json.loads(Path(args.evidence).read_text())
    team = args.team or pick.get("team") or pick.get("side") or evidence.get("team")
    errors = validate_process_grade(
        pick.get("process_grade"),
        result=pick.get("result"),
        baseball_evidence=pick.get("baseball_evidence"),
        postgame_evidence=evidence,
        team=team,
    )
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic MLB postgame evidence collection and process grading"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="collect postgame evidence for a game")
    collect.add_argument("--game-pk", type=int, help="MLB Stats API gamePk")
    collect.add_argument("--date", help="game date YYYY-MM-DD (with --team)")
    collect.add_argument("--team", help="exact backed team name, e.g. 'Detroit Tigers'")
    collect.set_defaults(func=_cmd_collect)

    grade = sub.add_parser("grade", help="validate a process_grade against collected evidence")
    grade.add_argument("--pick", required=True, help="path to the ledger-row JSON")
    grade.add_argument("--evidence", required=True, help="path to the collected evidence JSON")
    grade.add_argument("--team", help="exact backed team name override")
    grade.set_defaults(func=_cmd_grade)

    args = parser.parse_args(argv)
    if args.command == "collect":
        if args.game_pk is None and not (args.date and args.team):
            collect.error("provide --game-pk, or --date with --team")
        if args.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
            collect.error("--date must be YYYY-MM-DD")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
