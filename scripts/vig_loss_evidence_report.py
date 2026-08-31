#!/usr/bin/env python3
"""Read-only postgame thesis-pillar analysis over the executed MLB pick corpus.

The merged replay (`vig_pick_replay`) established WHAT happened to every
executed pick — 51 wins, 38 losses, every loss classified
`evidence_process_miss` from recorded state. This module asks the next
question: on the field, WHICH thesis pillars actually failed in those losses,
and — the denominator the loss numbers are meaningless without — how often the
same pillars failed in the wins.

It is built ON the audit and the postgame collector, not beside them:

1. Corpus selection, reconciliation, and official rows come from
   `vig_historical_audit.build_report` — the same code path the merged audit
   and replay run. This module never re-derives an outcome or re-reads a
   schedule shape, and the executed/decided cohort here is definitionally the
   replay's 51/38.
2. Pillar grades come from `mlb_postgame_evidence.auto_pillar_grades` — the
   same deterministic grader the settlement contract enforces. Nothing here
   invents a grading rule; if the collector cannot decide a pillar, the grade
   is `unknown` and is reported as `unknown`, never dropped.
3. It is read-only end to end. Output is stdout. The one opt-in side effect
   (`--fetch`) writes only the explicit postgame-evidence cache, mirroring the
   audit's results cache: fetch the feed once, keep the collector's output,
   and every later run is offline and byte-deterministic.

Hindsight rails, stated because this report exists to look at outcomes:

- Bet-time evidence handed to the grader is built from a fixed ALLOWLIST of
  pregame rationale fields (`PREGAME_EVIDENCE_FIELDS`). Outcome fields cannot
  reach the grader by construction, and a test pins that flipping every
  outcome label changes cohort membership only — never a single pillar grade.
- The corpus on disk records NO structured bet-time evidence (no
  `starter_role`, no `expected_ip`, no `named_risks`), so the pillars those
  inputs decide grade `unknown` across the board today. That is a coverage
  finding, reported with its denominator — not a reason to substitute a guess.
- Nothing here proposes a threshold. Descriptive pillar-failure rates are
  labelled descriptive; any model change mined from them must be graded
  leave-one-period-out by the replay's grader before it is a candidate.

Usage:
  python scripts/vig_loss_evidence_report.py --picks-dir <corpus>            # offline
  python scripts/vig_loss_evidence_report.py --picks-dir <corpus> --fetch    # populate cache
  python scripts/vig_loss_evidence_report.py --picks-dir <corpus> --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_util import fetch_json  # noqa: E402
from mlb_postgame_evidence import (  # noqa: E402
    FEED_URL,
    PILLARS,
    auto_pillar_grades,
    collect_postgame_evidence,
)
from vig_historical_audit import (  # noqa: E402
    DEFAULT_MIN_CONSERVATIVE_EDGE,
    build_report,
    effective_price,
)
from vig_pick_replay import (  # noqa: E402
    MISS_CLASSIFICATIONS,
    classify_opposing_winner_miss,
)

# The ONLY rationale fields that may reach the grader as bet-time evidence.
# Everything else on the record — outcome labels, official rows, recorded
# results — is outcome-side and must never influence a pillar grade. The
# allowlist is the mechanism; test_no_outcome_field_reaches_the_grader pins it.
PREGAME_EVIDENCE_FIELDS = ("starter_role", "expected_ip", "named_risks")

PILLAR_GRADE_SET = ("held", "failed", "mixed", "unknown")
ACTUAL_ROLES = ("starter", "opener_bulk", "short_start", "unknown")
COHORTS = ("loss", "win")

# Evidence-cache entry statuses, a closed set. `complete` and `insufficient`
# are the collector's own words; the rest describe the cache, not the game.
EVIDENCE_FILE_STATUSES = ("complete", "insufficient", "missing", "invalid")


class LossEvidenceError(Exception):
    """Configuration or corpus errors that must fail loud, not report."""


# ---------------------------------------------------------------------------
# Corpus selection
# ---------------------------------------------------------------------------


def executed_decided(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The replay's executed decided cohort, in a deterministic order.

    Executed disposition with a win/loss outcome — pushes and unreconciled
    executions are counted by `corpus_selection`, never silently dropped, but
    they carry no side-outcome cohort to compare pillars across.
    """
    picked = [
        r for r in records
        if r.get("disposition") == "executed"
        and r.get("side_outcome") in ("win", "loss")
    ]
    return sorted(
        picked,
        key=lambda r: (r.get("date") or "", _game_pk(r) or 0, r.get("resolved_side") or ""),
    )


def _game_pk(record: dict[str, Any]) -> int | None:
    official = record.get("official")
    if isinstance(official, dict):
        pk = official.get("gamePk")
        if isinstance(pk, int) and not isinstance(pk, bool):
            return pk
    return None


def corpus_selection(records: list[dict[str, Any]]) -> dict[str, Any]:
    """How the analyzed cohort was selected, with every exclusion counted.

    The loss classification counts are zero-filled over the replay's closed
    `MISS_CLASSIFICATIONS` set: a category at 0 tells the reader the axis was
    checked and empty, which an absent key cannot.
    """
    executed = [r for r in records if r.get("disposition") == "executed"]
    decided = executed_decided(records)
    losses = [r for r in decided if r["side_outcome"] == "loss"]
    wins = [r for r in decided if r["side_outcome"] == "win"]
    excluded = [r for r in executed if r not in decided]
    classification = {name: 0 for name in MISS_CLASSIFICATIONS}
    for record in losses:
        classification[classify_opposing_winner_miss(record)] += 1
    return {
        "candidates_total": len(records),
        "executed": len(executed),
        "executed_decided": len(decided),
        "wins": len(wins),
        "losses": len(losses),
        "executed_excluded": [
            {
                "date": r.get("date"),
                "game": r.get("game"),
                "side_outcome": r.get("side_outcome"),
                "unreconciled_reason": r.get("unreconciled_reason"),
            }
            for r in excluded
        ],
        "loss_classification_counts": classification,
    }


# ---------------------------------------------------------------------------
# Bet-time evidence (allowlisted) and the postgame-evidence cache
# ---------------------------------------------------------------------------


def bet_time_evidence(record: dict[str, Any]) -> dict[str, Any]:
    """Pregame inputs for the grader, built ONLY from the allowlist."""
    rationale = record.get("recorded_rationale")
    rationale = rationale if isinstance(rationale, dict) else {}
    return {
        field: rationale.get(field)
        for field in PREGAME_EVIDENCE_FIELDS
        if rationale.get(field) is not None
    }


def evidence_path(evidence_dir: Path, game_pk: int) -> Path:
    return evidence_dir / f"{game_pk}.json"


def load_cached_evidence(evidence_dir: Path, game_pk: int) -> tuple[dict[str, Any] | None, str]:
    """Read one cached collector output, validating shape before trusting it.

    A cache file that is unreadable, is not an object, carries an unknown
    ``evidence_status``, or names a different ``game_pk`` than its filename is
    ``invalid`` — corrupt cache must never launder into a graded game.
    """
    path = evidence_path(evidence_dir, game_pk)
    if not path.is_file():
        return None, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "invalid"
    if not isinstance(payload, dict):
        return None, "invalid"
    if payload.get("evidence_status") not in ("complete", "insufficient"):
        return None, "invalid"
    if payload.get("game_pk") != game_pk:
        return None, "invalid"
    return payload, payload["evidence_status"]


def fetch_missing_evidence(
    evidence_dir: Path, game_pks: list[int]
) -> list[int]:
    """Opt-in: fetch feed/live for uncached games and store collector output.

    The cache holds the collector's OUTPUT, not the raw feed: it is the
    artifact the analysis actually consumes, it is small enough to review, and
    an insufficient result is stored too — what the API said is part of the
    record, and the report will say `insufficient` rather than retrying
    silently forever.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    written: list[int] = []
    for game_pk in game_pks:
        path = evidence_path(evidence_dir, game_pk)
        if path.is_file():
            continue
        feed = fetch_json(FEED_URL.format(game_pk=game_pk), timeout=30, attempts=3)
        evidence = collect_postgame_evidence(feed)
        if evidence.get("game_pk") != game_pk:
            raise LossEvidenceError(
                f"feed for gamePk {game_pk} returned game_pk "
                f"{evidence.get('game_pk')!r}; refusing to cache a mismatched payload"
            )
        path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        written.append(game_pk)
    return written


# ---------------------------------------------------------------------------
# Per-game grading
# ---------------------------------------------------------------------------


def _descriptive(postgame: dict[str, Any], side: str) -> dict[str, Any]:
    """Postgame game-script facts for the backed side, labelled descriptive.

    These are outcome-side observations for interpretation — they never feed a
    grade and must never be quoted as pregame rationale.
    """
    pitching = postgame.get("pitching", {}).get(side, {}) or {}
    offense = postgame.get("offense", {}).get(side, {}) or {}
    starter = pitching.get("starter") or {}
    reliever_runs = [
        line.get("runs") for line in (pitching.get("relievers") or [])
    ]
    # An empty bullpen line is 0 runs allowed (complete game), not no evidence.
    bullpen_runs = (
        sum(reliever_runs)
        if all(isinstance(r, int) and not isinstance(r, bool) for r in reliever_runs)
        else None
    )
    return {
        "actual_role": pitching.get("actual_role", "unknown"),
        "starter_outs": starter.get("outs"),
        "starter_earned_runs": starter.get("earned_runs"),
        "bullpen_runs_allowed": bullpen_runs,
        "team_runs": offense.get("runs"),
    }


def grade_record(
    record: dict[str, Any], evidence_dir: Path
) -> dict[str, Any]:
    """One executed pick's pillar grades against its cached postgame evidence.

    Every game produces a row; the row's ``evidence_file_status`` says whether
    it could be graded. Ungradeable games keep their identity in the output so
    nothing is silently dropped.
    """
    game_pk = _game_pk(record)
    team = record.get("resolved_side")
    price, price_basis = effective_price(record)
    row: dict[str, Any] = {
        "date": record.get("date"),
        "game": record.get("game"),
        "game_pk": game_pk,
        "team": team,
        "side_outcome": record.get("side_outcome"),
        "price": price,
        "price_basis": price_basis,
        "bet_time_evidence_fields": sorted(bet_time_evidence(record)),
        "evidence_file_status": None,
        "pillars": None,
        "descriptive": None,
        "note": None,
    }
    if game_pk is None:
        row["evidence_file_status"] = "missing"
        row["note"] = "record carries no official gamePk to resolve evidence by"
        return row
    postgame, status = load_cached_evidence(evidence_dir, game_pk)
    row["evidence_file_status"] = status
    if postgame is None:
        return row
    try:
        grades = auto_pillar_grades(bet_time_evidence(record), postgame, team)
    except ValueError as exc:
        row["evidence_file_status"] = "invalid"
        row["note"] = str(exc)
        return row
    row["pillars"] = grades
    if status == "complete":
        # Side comes from THIS record's team, never from an `our_side` a CLI
        # `--team` run may have baked into a shared cache file.
        row["descriptive"] = _descriptive(postgame, _side_of(postgame, team))
    else:
        row["note"] = "; ".join(postgame.get("insufficient_reasons") or [])
    return row


def _side_of(postgame: dict[str, Any], team: Any) -> str:
    # side_for_team already validated by auto_pillar_grades; this cannot miss.
    return "away" if postgame.get("away") == team else "home"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_pillars(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pillar grade counts per cohort, zero-filled, denominators explicit.

    ``graded`` counts rows whose evidence file was usable (complete or
    insufficient — the collector's `unknown` grades are real output, and an
    insufficient game grading all-unknown is the contract working, not noise
    to hide). ``decided`` per pillar is held+failed+mixed; ``failed_rate`` is
    failed/decided and is null when nothing was decided, never 0-by-default.
    """
    out: dict[str, Any] = {}
    for cohort in COHORTS:
        cohort_rows = [r for r in rows if r["side_outcome"] == cohort]
        graded = [r for r in cohort_rows if r["pillars"] is not None]
        pillar_block: dict[str, Any] = {}
        for pillar in PILLARS:
            counts = {grade: 0 for grade in PILLAR_GRADE_SET}
            for row in graded:
                counts[row["pillars"][pillar]["grade"]] += 1
            decided = counts["held"] + counts["failed"] + counts["mixed"]
            pillar_block[pillar] = {
                "counts": counts,
                "graded": len(graded),
                "decided": decided,
                "failed_rate": (
                    round(counts["failed"] / decided, 6) if decided else None
                ),
            }
        roles = Counter(
            r["descriptive"]["actual_role"]
            for r in graded
            if r["descriptive"] is not None
        )
        out[cohort] = {
            "games": len(cohort_rows),
            "graded": len(graded),
            "ungraded": len(cohort_rows) - len(graded),
            "pillars": pillar_block,
            "backed_side_actual_role": {
                role: roles.get(role, 0) for role in ACTUAL_ROLES
            },
        }
    return out


def coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every game the analysis could not fully grade, by name."""
    by_status: dict[str, list[dict[str, Any]]] = {
        status: [] for status in EVIDENCE_FILE_STATUSES
    }
    for row in rows:
        entry = {
            "date": row["date"],
            "game": row["game"],
            "game_pk": row["game_pk"],
            "note": row["note"],
        }
        by_status[row["evidence_file_status"]].append(entry)
    return {
        "counts": {status: len(by_status[status]) for status in EVIDENCE_FILE_STATUSES},
        "insufficient": by_status["insufficient"],
        "missing": by_status["missing"],
        "invalid": by_status["invalid"],
        "bet_time_evidence": {
            "note": (
                "pillars starter_role, starter_quality, and named_risk need "
                "recorded bet-time evidence (starter_role/expected_ip/"
                "named_risks); games recording none grade those pillars "
                "unknown by contract"
            ),
            "records_with_any_field": sum(
                1 for r in rows if r["bet_time_evidence_fields"]
            ),
            "records_total": len(rows),
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def build_loss_evidence_report(
    audit_report: dict[str, Any], evidence_dir: Path
) -> dict[str, Any]:
    records = [r for day in audit_report["days"] for r in day["candidates"]]
    cohort = executed_decided(records)
    rows = [grade_record(record, evidence_dir) for record in cohort]
    return {
        "population": (
            "executed picks with an official win/loss outcome, from the same "
            "audit build the merged replay grades; pillar grades are the "
            "postgame collector's deterministic output"
        ),
        "corpus_selection": corpus_selection(records),
        "games": rows,
        "aggregates": aggregate_pillars(rows),
        "coverage": coverage(rows),
        "caveats": [
            "Pillar failure rates are DESCRIPTIVE postgame facts about this "
            "corpus. They are not a model change; any adjustment mined from "
            "them must be graded leave-one-period-out before it is a candidate.",
            "The loss cohort is every executed decided loss; all currently "
            "classify evidence_process_miss, and the classification counts "
            "above are the receipt, not an assumption.",
            "starter_quality and named_risk (and starter_role's expected half) "
            "grade unknown wherever the card recorded no structured bet-time "
            "evidence — a corpus coverage limit, stated with its denominator.",
        ],
    }


def render(report: dict[str, Any]) -> str:
    sel = report["corpus_selection"]
    lines = [
        "MLB executed-pick postgame pillar analysis (read-only)",
        f"- corpus: {sel['executed_decided']} executed decided "
        f"({sel['wins']} wins / {sel['losses']} losses) of {sel['executed']} executed, "
        f"{sel['candidates_total']} candidates",
        "- loss classifications: "
        + ", ".join(f"{k}={v}" for k, v in sel["loss_classification_counts"].items()),
    ]
    cov = report["coverage"]["counts"]
    lines.append(
        "- evidence coverage: "
        + ", ".join(f"{status}={cov[status]}" for status in EVIDENCE_FILE_STATUSES)
    )
    bt = report["coverage"]["bet_time_evidence"]
    lines.append(
        f"- bet-time evidence recorded: {bt['records_with_any_field']}"
        f"/{bt['records_total']} records"
    )
    for cohort in COHORTS:
        block = report["aggregates"][cohort]
        lines.append(
            f"\n{cohort.upper()} cohort — {block['games']} games, "
            f"{block['graded']} graded, {block['ungraded']} ungraded:"
        )
        for pillar in PILLARS:
            p = block["pillars"][pillar]
            c = p["counts"]
            rate = "n/a" if p["failed_rate"] is None else f"{p['failed_rate']:.1%}"
            lines.append(
                f"  {pillar:22s} held={c['held']:2d} failed={c['failed']:2d} "
                f"mixed={c['mixed']:2d} unknown={c['unknown']:2d} "
                f"(failed/decided: {c['failed']}/{p['decided']} = {rate})"
            )
        roles = block["backed_side_actual_role"]
        lines.append(
            "  backed-side actual role: "
            + ", ".join(f"{role}={roles[role]}" for role in ACTUAL_ROLES)
        )
    lines.append("")
    for caveat in report["caveats"]:
        lines.append(f"CAVEAT: {caveat}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only postgame pillar analysis of executed MLB picks"
    )
    parser.add_argument("--picks-dir", help="the .picks directory (default: $SPORTS_PICKS_ROOT/.picks)")
    parser.add_argument("--results-dir", help="cache of MLB Stats API schedule payloads (default: <picks-dir>/audit-results)")
    parser.add_argument("--evidence-dir", help="cache of postgame collector outputs (default: <picks-dir>/postgame-evidence)")
    parser.add_argument("--since", help="earliest schedule date YYYY-MM-DD")
    parser.add_argument("--until", help="latest schedule date YYYY-MM-DD")
    parser.add_argument("--edge-floor", type=float, default=DEFAULT_MIN_CONSERVATIVE_EDGE,
                        help="conservative edge floor passed through to the audit")
    parser.add_argument("--fetch", action="store_true",
                        help="opt-in: populate the evidence cache for uncached games")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = parser.parse_args(argv)

    for flag, value in (("--since", args.since), ("--until", args.until)):
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parser.error(f"{flag} must be YYYY-MM-DD")
    if not 0 < args.edge_floor < 1:
        parser.error("--edge-floor must be between 0 and 1")

    picks_dir = args.picks_dir or (
        os.environ.get("SPORTS_PICKS_ROOT")
        and str(Path(os.environ["SPORTS_PICKS_ROOT"]) / ".picks")
    )
    if not picks_dir:
        print("no --picks-dir and no SPORTS_PICKS_ROOT", file=sys.stderr)
        return 2
    picks = Path(picks_dir).expanduser()
    execute_dir = picks / "execute"
    results_dir = Path(args.results_dir).expanduser() if args.results_dir else picks / "audit-results"
    evidence_dir = Path(args.evidence_dir).expanduser() if args.evidence_dir else picks / "postgame-evidence"

    audit_report = build_report(
        execute_dir, results_dir, args.edge_floor, args.since, args.until,
        0.05, 20,
    )
    records = [r for day in audit_report["days"] for r in day["candidates"]]

    if args.fetch:
        pks = sorted(
            {pk for pk in (_game_pk(r) for r in executed_decided(records)) if pk is not None}
        )
        written = fetch_missing_evidence(evidence_dir, pks)
        print(f"fetched {len(written)} postgame evidence payloads", file=sys.stderr)

    report = build_loss_evidence_report(audit_report, evidence_dir)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    # Fail loud when any game could not be graded from a usable evidence file:
    # scripted use must notice a hole in coverage, exactly like the collector.
    counts = report["coverage"]["counts"]
    return 1 if (counts["missing"] or counts["invalid"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
