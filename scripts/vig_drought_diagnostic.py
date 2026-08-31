#!/usr/bin/env python3
"""Read-only, day-by-day diagnostic of a stretch that produced no executed picks.

Written for the 2026-08-11..2026-08-31 window, in which the MLB lane executed
zero picks across 21 days (Jerry, 2026-08-31). The question it answers is not
"why did we lose" — that is the loss-evidence report's job — but the earlier
one: **for each day, how far down the pipeline did anything get, and where did
it stop.**

The distinction this module exists to enforce is that a drought is not one
thing. A day with no slate artifact at all, a day whose scan ran and found
nothing on a fifteen-game slate, a day that produced only lineup-watchlist
near-misses, and a day that produced priced candidates which the review gate
then rejected are FOUR different findings with four different fixes. Collapsing
them into "no picks" is what makes a drought look like one problem.

So every day in the window gets exactly one ``day_class``:

- ``no_slate_artifact`` — no schedule JSON, no slate writeup, nothing. The scan
  did not run, or ran and wrote nothing at all. This is the only class that is
  about the SCAN rather than about the games.
- ``slate_empty`` — the scan ran and produced neither a candidate nor a
  watchlist entry.
- ``watchlist_only`` — near-misses were recorded but nothing was priced.
- ``candidates_rejected`` — candidates were priced and none was approved.
- ``candidates_executed`` — at least one candidate executed.

**Denominators travel with numerators.** "The scan found nothing" means one
thing against a fifteen-game slate and another against a two-game slate, so
every day carries ``games_scheduled`` read from the cached MLB schedule
payload. A day whose schedule payload is absent reports ``None`` and is named
in ``data_gaps`` — never silently zero, which would read as "no games" when it
means "no evidence about games".

**Outcomes are reported separately from process.** For a rejected candidate the
report records what the chosen side actually did, because "we passed on a
winner" and "we passed correctly" are both worth knowing and only the first is
visible from the outcome. Outcome facts are labelled ``outcome_*`` and never
feed a process verdict; the ``process_note`` on a candidate is derived only
from what was recorded at decision time.

Read-only: opens files, writes nothing outside its own report. No network, no
ledger mutation, no execution.

Usage:
  python scripts/vig_drought_diagnostic.py --picks-dir <dir> \
      --since 2026-08-11 --until 2026-08-31 [--ledger picks.json] [--json]

No third-party dependencies: standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from mlb_lineup_watchlist import VALID_STATUSES, validate_entry  # noqa: E402

REPORT_SCHEMA = "vig-mlb-drought-diagnostic-v1"

# One label per day, mutually exclusive and exhaustive. Ordered from "nothing
# happened" to "something executed" so a reader can scan the counts as a funnel.
DAY_CLASSES = (
    "no_slate_artifact",
    "slate_empty",
    "watchlist_only",
    "candidates_rejected",
    "candidates_executed",
)

# Where a priced candidate stopped. `review_gate_rejected` is a DECISION, not a
# failure — separating it from the mechanical stops is the whole point.
CANDIDATE_STOPS = (
    "executed",
    "review_gate_rejected",
    "approved_not_executed",
    "unknown",
)


class DiagnosticError(RuntimeError):
    """Raised for an input the report cannot honestly describe."""


def parse_date(value: str) -> dt.date:
    """Parse YYYY-MM-DD as a REAL date, not just a well-shaped string."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise DiagnosticError(f"not a real calendar date: {value!r}") from exc


def date_range(since: dt.date, until: dt.date) -> list[dt.date]:
    if until < since:
        raise DiagnosticError(f"--until {until} is before --since {since}")
    span = (until - since).days
    return [since + dt.timedelta(days=offset) for offset in range(span + 1)]


def portable(path: Path | None) -> str | None:
    """Render a path home-relative, so a committed report is not machine-bound.

    A report carries its own source paths so a reader can re-run it, but an
    ABSOLUTE path bakes in whoever generated it: the artifact then names a
    developer's home directory in a file that ships to a box where that home
    does not exist. This repo already fails a test for exactly that
    (``test_deployed_scripts_and_docs_have_no_baked_in_home``, widened in PR
    #59 to cover docs), and it caught this artifact — the paths below used to
    be absolute.

    ``~`` keeps the provenance useful and portable at once.
    """
    if path is None:
        return None
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return "~" + text[len(home):]
    return text


def _load_json(path: Path) -> Any | None:
    """Return parsed JSON, or None when the file is absent.

    A file that exists but does not parse is NOT None — that is a corrupt
    artifact, which is a different finding from a missing one, and the caller
    records it as such.
    """
    if not path.exists():
        return None
    return json.loads(path.read_text())


SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"


def refetch_schedule(day: dt.date) -> dict[str, Any]:
    """Fetch one day's public MLB schedule. Opt-in, in memory, never written.

    The cached payloads are snapshots taken during the slate run, so a day's
    games are routinely still ``In Progress`` in the cache — 2026-08-30's was
    taken at 19:42Z with eleven games unfinished. That is a stale snapshot, not
    a missing outcome, and it is the only thing standing between this report
    and "what happened to the candidates we passed on".

    Deliberately does NOT write to the results cache: this diagnostic is
    read-only over the corpus, and a report that quietly rewrote its own inputs
    could never be re-run against the same evidence.
    """
    import urllib.request

    url = SCHEDULE_URL.format(date=day.isoformat())
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def scheduled_games(results_dir: Path, day: dt.date) -> tuple[int | None, dict[str, Any]]:
    """Return (game count, {game key -> final outcome}) for one day.

    The count is ``None`` when the schedule payload is absent. None and 0 mean
    opposite things — "no evidence about games" versus "no games" — and a
    reader who cannot tell them apart will read an outage as an off-day.
    """
    payload = _load_json(results_dir / f"{day.isoformat()}.json")
    if payload is None:
        return None, {}
    return _walk_schedule(payload)


def _walk_schedule(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    count = 0
    outcomes: dict[str, Any] = {}
    for date_block in payload.get("dates", []):
        for game in date_block.get("games", []):
            count += 1
            teams = game.get("teams", {})
            away, home = teams.get("away", {}), teams.get("home", {})
            record = {
                "game_pk": game.get("gamePk"),
                "status": (game.get("status") or {}).get("detailedState"),
                "away_team": (away.get("team") or {}).get("name"),
                "home_team": (home.get("team") or {}).get("name"),
                "away_score": away.get("score"),
                "home_score": home.get("score"),
            }
            winner = None
            if away.get("isWinner"):
                winner = record["away_team"]
            elif home.get("isWinner"):
                winner = record["home_team"]
            record["winner"] = winner
            # Keyed on "AWAY at HOME", NOT on an id.
            #
            # The slate's `event_id` is a DIFFERENT ID SPACE from the MLB
            # `gamePk` — 2026-08-30 records event_id 401816733 for the game the
            # schedule calls gamePk 824876. Joining on it silently matches
            # nothing, which reads as "no outcome data" when it means "wrong
            # key". PR #61 already taught this lane that an id is a
            # disambiguator until independently corroborated; here the matchup
            # is the only field both sides genuinely share.
            if record["away_team"] and record["home_team"]:
                outcomes[f"{record['away_team']} at {record['home_team']}"] = record
    return count, outcomes


def _candidate_stop(candidate: dict[str, Any]) -> tuple[str, str]:
    """Classify where one priced candidate stopped, and say why.

    Keyed on recorded STATE, never on the presence of a field: `vig_notes` is
    written on both outcomes, so its presence carries no polarity. The lane
    learned that on PR #64 and it applies unchanged here.
    """
    if candidate.get("executed") is True:
        return "executed", "order placed and recorded"
    approved = candidate.get("vig_approved")
    if approved is False:
        note = candidate.get("vig_notes")
        reason = "review gate rejected the candidate"
        if isinstance(note, str) and note.strip():
            reason = f"review gate rejected the candidate: {note.strip()}"
        return "review_gate_rejected", reason
    if approved is True:
        return (
            "approved_not_executed",
            "approved by the review gate but no execution recorded",
        )
    return (
        "unknown",
        f"vig_approved is {approved!r}; no recorded decision to attribute",
    )


def _candidate_outcome(
    candidate: dict[str, Any], outcomes: dict[str, Any]
) -> dict[str, Any]:
    """What the chosen side actually did, or an explicit reason it is unknown.

    Reported for interest, never fed into the process verdict. A pass on a team
    that went on to win is not thereby a mistake.
    """
    key = candidate.get("game")
    game = outcomes.get(key)
    if game is None:
        return {
            "outcome_known": False,
            "outcome_reason": f"no game in the cached schedule matching {key!r}",
        }
    if game.get("status") != "Final":
        # Not a missing outcome — a schedule payload cached BEFORE the games
        # ended. 2026-08-30's was fetched at 19:42Z with eleven games still in
        # progress, so the cache can never answer "did the passed side win"
        # for that day without a re-fetch. Saying "unknown" without saying why
        # would read as a data hole rather than a stale snapshot.
        return {
            "outcome_known": False,
            "outcome_reason": (
                f"cached schedule has this game as {game.get('status')!r}, not "
                "Final; the payload was cached before the game ended"
            ),
        }
    winner = game.get("winner")
    side = candidate.get("side")
    if winner is None or side is None:
        return {
            "outcome_known": False,
            "outcome_reason": "final recorded but no winner or no chosen side",
        }
    return {
        "outcome_known": True,
        "outcome_winner": winner,
        "outcome_side_won": side == winner,
        "outcome_score": f"{game.get('away_score')}-{game.get('home_score')}",
    }


def _watchlist_trace(entry: dict[str, Any]) -> dict[str, Any]:
    """One watchlist entry, with its validity taken from the REAL validator.

    `validate_entry` is imported rather than restated: a second copy of the
    status rule would agree with the first only until one of them changed, and
    this lane has spent four PRs on exactly that failure mode.
    """
    status = entry.get("status")
    errors = validate_entry(entry)
    return {
        "id": entry.get("id"),
        "game": entry.get("game"),
        "status": status,
        "status_is_valid": status in VALID_STATUSES,
        "validator_errors": errors,
    }


def analyze_day(
    day: dt.date,
    *,
    execute_dir: Path,
    results_dir: Path,
    slate_dir: Path,
    ledger_by_date: dict[str, list[dict[str, Any]]],
    fetch_outcomes: bool = False,
) -> dict[str, Any]:
    """Build the full trace for a single day. Never raises on a missing file."""
    iso = day.isoformat()
    schedule_path = execute_dir / f"{iso}-schedule.json"

    corrupt: str | None = None
    try:
        slate = _load_json(schedule_path)
    except json.JSONDecodeError as exc:
        slate, corrupt = None, f"{schedule_path.name} is not valid JSON: {exc}"

    # Every writeup for the day, morning and evening. A day can have both.
    writeups = sorted(p.name for p in slate_dir.glob(f"{iso}*.md")) if slate_dir.exists() else []

    games_scheduled, outcomes = scheduled_games(results_dir, day)
    outcome_source = "cache"
    outcome_refetch_error = None

    candidates_raw = (slate or {}).get("candidates") or []
    watchlist_raw = (slate or {}).get("lineup_watchlist") or []

    # Only refetch when there is a candidate whose outcome the cache cannot
    # answer. A day with nothing to attribute has no reason to touch the API.
    if fetch_outcomes and candidates_raw:
        stale = any(
            (outcomes.get(c.get("game")) or {}).get("status") != "Final"
            for c in candidates_raw
        )
        if stale:
            try:
                _, refreshed = _walk_schedule(refetch_schedule(day))
                if refreshed:
                    outcomes, outcome_source = refreshed, "live-refetch"
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                outcome_refetch_error = f"{type(exc).__name__}: {exc}"

    candidates = []
    for index, candidate in enumerate(candidates_raw):
        stop, reason = _candidate_stop(candidate)
        trace = {
            "trace_id": f"{iso}-cand-{index:02d}",
            "event_id": candidate.get("event_id"),
            "game": candidate.get("game"),
            "side": candidate.get("side"),
            "net_edge": candidate.get("net_edge"),
            "polymarket_ask": candidate.get("polymarket_ask"),
            "execution_mode": candidate.get("execution_mode"),
            "vig_approved": candidate.get("vig_approved"),
            "vig_review_needed": candidate.get("vig_review_needed"),
            "executed": candidate.get("executed"),
            "stop": stop,
            "stop_reason": reason,
        }
        trace.update(_candidate_outcome(candidate, outcomes))
        candidates.append(trace)

    watchlist = [_watchlist_trace(entry) for entry in watchlist_raw]
    executed_ledger = ledger_by_date.get(iso, [])

    has_artifact = slate is not None or bool(writeups) or corrupt is not None
    if not has_artifact:
        day_class = "no_slate_artifact"
    elif any(c["stop"] == "executed" for c in candidates) or executed_ledger:
        day_class = "candidates_executed"
    elif candidates:
        day_class = "candidates_rejected"
    elif watchlist:
        day_class = "watchlist_only"
    else:
        day_class = "slate_empty"

    return {
        "date": iso,
        "day_class": day_class,
        "games_scheduled": games_scheduled,
        "slate_json_present": slate is not None,
        "slate_json_corrupt": corrupt,
        "outcome_source": outcome_source,
        "outcome_refetch_error": outcome_refetch_error,
        "writeups": writeups,
        "counts": {
            "candidates": len(candidates),
            "watchlist_entries": len(watchlist),
            "executed_in_ledger": len(executed_ledger),
        },
        "candidates": candidates,
        "watchlist": watchlist,
    }


def _ledger_by_date(ledger_path: Path | None) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Index the canonical ledger by game date; report why if it is unusable."""
    if ledger_path is None:
        return {}, "no --ledger supplied; executed counts come from slate state only"
    if not ledger_path.exists():
        return {}, f"ledger not found at {portable(ledger_path)}"
    payload = json.loads(ledger_path.read_text())
    picks = payload if isinstance(payload, list) else payload.get("picks") or []
    indexed: dict[str, list[dict[str, Any]]] = {}
    for pick in picks:
        key = pick.get("date") or pick.get("game_date")
        if key:
            indexed.setdefault(str(key), []).append(pick)
    return indexed, None


def build_report(
    *,
    picks_dir: Path,
    since: dt.date,
    until: dt.date,
    results_dir: Path | None = None,
    slate_dir: Path | None = None,
    ledger_path: Path | None = None,
    runtime_revision: str | None = None,
    repo_revision: str | None = None,
    fetch_outcomes: bool = False,
) -> dict[str, Any]:
    execute_dir = picks_dir / "execute"
    results_dir = results_dir or (picks_dir / "audit-results")
    slate_dir = slate_dir or (picks_dir / "slate")

    ledger_by_date, ledger_gap = _ledger_by_date(ledger_path)

    days = [
        analyze_day(
            day,
            execute_dir=execute_dir,
            results_dir=results_dir,
            slate_dir=slate_dir,
            ledger_by_date=ledger_by_date,
            fetch_outcomes=fetch_outcomes,
        )
        for day in date_range(since, until)
    ]

    # Zero-filled over the CLOSED class set, so a class that never occurred
    # prints 0 rather than vanishing. A missing key and a zero read the same to
    # a skimmer and mean opposite things to an analyst.
    class_counts = {name: 0 for name in DAY_CLASSES}
    for day in days:
        class_counts[day["day_class"]] += 1

    stop_counts = {name: 0 for name in CANDIDATE_STOPS}
    for day in days:
        for candidate in day["candidates"]:
            stop_counts[candidate["stop"]] += 1

    total_candidates = sum(d["counts"]["candidates"] for d in days)
    total_watchlist = sum(d["counts"]["watchlist_entries"] for d in days)

    data_gaps: list[dict[str, Any]] = []
    if ledger_gap:
        data_gaps.append({"kind": "ledger", "detail": ledger_gap})
    missing_schedule = [d["date"] for d in days if d["games_scheduled"] is None]
    if missing_schedule:
        data_gaps.append(
            {
                "kind": "no_cached_mlb_schedule",
                "detail": (
                    "no denominator for these days: the scan's output cannot be "
                    "read against the slate that was actually available"
                ),
                "dates": missing_schedule,
            }
        )
    silent_days = [d["date"] for d in days if d["day_class"] == "no_slate_artifact"]
    if silent_days:
        data_gaps.append(
            {
                "kind": "no_slate_artifact",
                "detail": (
                    "no schedule JSON and no writeup. The corpus cannot "
                    "distinguish 'the job did not run' from 'the job ran and "
                    "wrote nothing'; that needs cron/journal state, which is "
                    "outside this report's inputs"
                ),
                "dates": silent_days,
            }
        )
    invalid_status = [
        {"date": d["date"], "id": w["id"], "status": w["status"]}
        for d in days
        for w in d["watchlist"]
        if not w["status_is_valid"]
    ]
    if invalid_status:
        data_gaps.append(
            {
                "kind": "invalid_watchlist_status",
                "detail": (
                    "status is outside the validator's accepted set, so no "
                    "transition can act on the entry again"
                ),
                "entries": invalid_status,
            }
        )

    return {
        "schema": REPORT_SCHEMA,
        "window": {"since": since.isoformat(), "until": until.isoformat(), "days": len(days)},
        "sources": {
            "picks_dir": portable(picks_dir),
            "execute_dir": portable(execute_dir),
            "results_dir": portable(results_dir),
            "slate_dir": portable(slate_dir),
            "ledger": portable(ledger_path),
            "repo_revision": repo_revision,
            "runtime_revision": runtime_revision,
        },
        "aggregates": {
            "day_classes": class_counts,
            "candidate_stops": stop_counts,
            "total_candidates": total_candidates,
            "total_watchlist_entries": total_watchlist,
            "total_executed": stop_counts["executed"],
        },
        "reconciliation": reconcile(days, class_counts, stop_counts),
        "days": days,
        "data_gaps": data_gaps,
    }


def reconcile(
    days: list[dict[str, Any]],
    class_counts: dict[str, int],
    stop_counts: dict[str, int],
) -> dict[str, Any]:
    """Assert the funnel adds up, and SAY so in the artifact.

    A reader has no way to check a stage count by eye, so the report carries
    its own arithmetic. `ok: false` is a defect in this script, not in the
    data, and it is better read in the artifact than never noticed.
    """
    checks = [
        {
            "check": "every day is classified exactly once",
            "expected": len(days),
            "actual": sum(class_counts.values()),
        },
        {
            "check": "every candidate has exactly one stop",
            "expected": sum(d["counts"]["candidates"] for d in days),
            "actual": sum(stop_counts.values()),
        },
        {
            "check": "days with candidates are classed rejected or executed",
            "expected": sum(1 for d in days if d["counts"]["candidates"]),
            "actual": class_counts["candidates_rejected"] + class_counts["candidates_executed"],
        },
    ]
    for entry in checks:
        entry["ok"] = entry["expected"] == entry["actual"]
    return {"ok": all(entry["ok"] for entry in checks), "checks": checks}


def render(report: dict[str, Any]) -> str:
    window = report["window"]
    agg = report["aggregates"]
    lines = [
        f"# MLB drought diagnostic — {window['since']} to {window['until']} "
        f"({window['days']} days)",
        "",
        f"Executed picks in window: **{agg['total_executed']}**. "
        f"Priced candidates: {agg['total_candidates']}. "
        f"Watchlist near-misses: {agg['total_watchlist_entries']}.",
        "",
        "## Days by class",
        "",
        "| class | days |",
        "| --- | --- |",
    ]
    for name in DAY_CLASSES:
        lines.append(f"| `{name}` | {agg['day_classes'][name]} |")
    lines += ["", "## Where priced candidates stopped", "", "| stop | count |", "| --- | --- |"]
    for name in CANDIDATE_STOPS:
        lines.append(f"| `{name}` | {agg['candidate_stops'][name]} |")
    lines += ["", "## Day by day", "", "| date | class | games | cands | watch |", "| --- | --- | --- | --- | --- |"]
    for day in report["days"]:
        games = "—" if day["games_scheduled"] is None else day["games_scheduled"]
        lines.append(
            f"| {day['date']} | `{day['day_class']}` | {games} | "
            f"{day['counts']['candidates']} | {day['counts']['watchlist_entries']} |"
        )
    if report["data_gaps"]:
        lines += ["", "## Data gaps"]
        for gap in report["data_gaps"]:
            detail = gap.get("dates") or gap.get("entries") or ""
            lines.append(f"- **{gap['kind']}** — {gap['detail']}. {detail}")
    recon = report["reconciliation"]
    lines += ["", f"Reconciliation: {'ok' if recon['ok'] else 'FAILED'}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only day-by-day diagnostic of a no-pick stretch"
    )
    parser.add_argument("--picks-dir", required=True, help="the .picks directory")
    parser.add_argument("--since", required=True, help="first day YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="last day YYYY-MM-DD")
    parser.add_argument("--results-dir", help="MLB schedule cache (default: <picks-dir>/audit-results)")
    parser.add_argument("--slate-dir", help="slate writeups (default: <picks-dir>/slate)")
    parser.add_argument("--ledger", help="canonical picks.json")
    parser.add_argument("--runtime-revision", help="deployed runtime revision, recorded verbatim")
    parser.add_argument("--repo-revision", help="analysis code revision, recorded verbatim")
    parser.add_argument(
        "--fetch-outcomes",
        action="store_true",
        help=(
            "opt-in: refetch the public MLB schedule in memory for days whose "
            "cached payload predates the final. Never writes to the cache."
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = parser.parse_args(argv)

    try:
        report = build_report(
            picks_dir=Path(args.picks_dir).expanduser(),
            since=parse_date(args.since),
            until=parse_date(args.until),
            results_dir=Path(args.results_dir).expanduser() if args.results_dir else None,
            slate_dir=Path(args.slate_dir).expanduser() if args.slate_dir else None,
            ledger_path=Path(args.ledger).expanduser() if args.ledger else None,
            runtime_revision=args.runtime_revision,
            repo_revision=args.repo_revision,
            fetch_outcomes=args.fetch_outcomes,
        )
    except DiagnosticError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0 if report["reconciliation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
