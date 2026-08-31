#!/usr/bin/env python3
"""Read-only, day-by-day diagnostic of a stretch that produced no executed picks.

Written for the 2026-08-11..2026-08-31 window, in which the MLB lane executed
zero picks across 21 days (Jerry, 2026-08-31). The question it answers is not
"why did we lose" — that is the loss-evidence report's job — but the earlier
one: **for each day, how far down the pipeline did anything get, and where did
it stop.**

The distinction this module exists to enforce is that a drought is not one
thing. A day whose job never fired, a day whose scan ran but never reached the
write, a day whose scan ran and found nothing on a fifteen-game slate, a day
that produced only lineup-watchlist near-misses, and a day that produced priced
candidates which the review gate then rejected are DIFFERENT findings with
different fixes. Collapsing them into "no picks" is what makes a drought look
like one problem.

So every day in the window gets exactly one ``day_class``:

- ``job_never_fired`` — the scheduler never started the run. Nothing about the
  games or the scan is implicated; the fix is in the scheduler.
- ``scan_ran_artifact_unwritten`` — the scan ran and delivered an analysis but
  never reached the step that persists the slate files. The analysis exists;
  only the artifact is missing. The fix is in the run's budget, not the market.
  A day reaches this class EITHER from run evidence or from the corpus alone,
  when the only MLB-lane file it left is the schedule cache the run writes
  before it produces anything. ``day_class_source`` records which.
- ``no_slate_artifact`` — no artifact in any known root and no run evidence that
  says why. This is the honest residual: it means "we do not know", and a day
  only lands here when nothing can explain it.
- ``slate_empty`` — the scan ran and produced neither a candidate nor a
  watchlist entry.
- ``watchlist_only`` — near-misses were recorded but nothing was priced.
- ``candidates_rejected`` — candidates were priced and none was approved.
- ``candidates_executed`` — at least one candidate executed.

The first three all look identical from a single directory listing, which is the
point: separating them takes evidence the corpus does not contain, and this
module refuses to guess in its absence.

**The corpus is enumerated across EVERY known root.** The 2026-08 window spans a
deploy cutover, and the daily slate wrote into more than one checkout across it:
``2026-08-20``'s artifacts exist only under ``sports-picks-skill/.picks`` while
every other day in the window is under ``sports-picks-runtime/.picks``. A report
built from one root reported that day as "no artifact" — the file was there, the
lookup was pointed at the wrong root, and the silence read as absence. That is
the same failure as joining an ``event_id`` against a ``gamePk``, and the report
names them as one pattern in ``findings``.

**The enumeration is recursive, and the classification is lane-scoped.** Those
are two different jobs and the first draft did both with one shallow glob. The
walk now lists every date-named file at any depth under the searched
subdirectories — ``slate/nfl/2026-08-26.md`` was invisible to a top-level glob
and the table rendered that invisibility as "—", which is the same pattern a
third time, inside the enumeration written to answer it. Only a file at the TOP
level of a subdirectory is MLB-lane output, and only MLB-lane files decide a day
class: an NFL writeup satisfying "the scan produced an artifact" would be the
mirror defect of missing it. Both listings travel in every day record.

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
      [--also-picks-dir <dir> ...] [--run-evidence <json>] \
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

REPORT_SCHEMA = "vig-mlb-drought-diagnostic-v2"
RUN_EVIDENCE_SCHEMA = "vig-drought-run-evidence-v1"

# The subdirectories of a `.picks` root this report reads. Enumerated per date
# and per root so a reader can see exactly what was and was not there.
PICKS_SUBDIRS = ("execute", "slate", "audit-results")

# One label per day, mutually exclusive and exhaustive. Ordered from "nothing
# happened" to "something executed" so a reader can scan the counts as a funnel.
DAY_CLASSES = (
    "job_never_fired",
    "scan_ran_artifact_unwritten",
    "no_slate_artifact",
    "slate_empty",
    "watchlist_only",
    "candidates_rejected",
    "candidates_executed",
)

# The only classes external run evidence is allowed to assign, and only to a day
# the corpus has NOTHING for. Evidence explains a silence; it can never overrule
# an artifact, because the artifact is the stronger fact and the evidence was
# collected from logs that rotate. Kept as its own tuple rather than a slice of
# DAY_CLASSES so widening it is a deliberate edit with a test attached.
EVIDENCE_ASSIGNABLE_CLASSES = (
    "job_never_fired",
    "scan_ran_artifact_unwritten",
)

# How much a receipt is worth taking on trust. A quoted log line can be checked
# character-for-character against the source; a count ("this grep returned 0")
# is a DERIVED measurement that a reader can only re-run, not re-read. Both are
# legitimate — an absence argument is made of counts — but labelling them the
# same would let "verbatim" mean nothing.
RECEIPT_KINDS = ("verbatim", "derived")

# An absence argument is only as strong as its denominator, and a denominator
# has parts. "Zero job lines on 08-19" is worthless without the signature being
# searched for, proof the log was continuous across the day, proof the logger
# was writing at all, neighbouring dates that DO show the line, and a source
# off the host entirely. Requiring the roles — rather than trusting the prose
# to mention them — means deleting any one of them REFUSES the file instead of
# quietly shrinking the argument.
REQUIRED_ABSENCE_ROLES = (
    "firing_signature_absence",
    "log_continuity",
    "liveness_control",
    "neighbour_comparison",
    "independent_source",
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


def picks_roots(primary: Path, extras: list[Path] | None) -> list[dict[str, Any]]:
    """Label every `.picks` root the report will search, primary first.

    Order is precedence: for any single-valued fact (the schedule JSON, the
    cached MLB payload) the first root that HAS a usable copy wins, and the
    report records which one it came from. Enumeration, by contrast, is over
    ALL roots — that is the half that catches a file the primary root lacks.

    The label is the root's parent directory name, which is what distinguishes
    the checkouts in practice (`sports-picks-runtime` vs `sports-picks-skill`)
    and carries no home path. Collisions get a numeric suffix rather than being
    silently merged: two roots reported under one label would hide exactly the
    discrepancy this function exists to surface.
    """
    ordered = [("primary", primary)] + [("secondary", p) for p in (extras or [])]
    seen: dict[str, int] = {}
    roots: list[dict[str, Any]] = []
    for role, path in ordered:
        base = path.parent.name or path.name
        count = seen.get(base, 0)
        seen[base] = count + 1
        roots.append(
            {
                "label": base if count == 0 else f"{base}-{count + 1}",
                "role": role,
                "path": portable(path),
                "_path": path,
                "_dirs": {sub: path / sub for sub in PICKS_SUBDIRS},
            }
        )
    return roots


def load_run_evidence(path: Path) -> dict[str, Any]:
    """Load externally-collected run evidence, refusing anything unsupported.

    This file is the ONLY way a day with no artifact gets a cause, so it is the
    one input that could quietly launder a guess into the headline table. Three
    refusals keep it honest, and each raises rather than warning:

    - the schema must match, so a differently-shaped file is not read loosely;
    - a verdict must be in ``EVIDENCE_ASSIGNABLE_CLASSES``, so evidence cannot
      invent a class or claim one the corpus is responsible for;
    - a verdict must carry at least one receipt with a source and a verbatim
      quote. The sources behind this file rotate — the durable cron DB is a
      1000-row ring buffer that no longer reaches these dates — so a claim
      without a quoted line is unfalsifiable by the time anyone reads the
      report, and an unfalsifiable claim is worth less than an open question.
    """
    payload = json.loads(path.read_text())
    schema = payload.get("schema")
    if schema != RUN_EVIDENCE_SCHEMA:
        raise DiagnosticError(
            f"run evidence schema is {schema!r}, expected {RUN_EVIDENCE_SCHEMA!r}"
        )
    dates = payload.get("dates") or {}
    for iso in sorted(dates):
        entry = dates[iso]
        verdict = entry.get("verdict")
        if verdict not in EVIDENCE_ASSIGNABLE_CLASSES:
            raise DiagnosticError(
                f"run evidence for {iso}: verdict {verdict!r} is not one of "
                f"{list(EVIDENCE_ASSIGNABLE_CLASSES)}"
            )
        receipts = entry.get("receipts") or []
        if not receipts:
            raise DiagnosticError(
                f"run evidence for {iso}: verdict {verdict!r} carries no receipt; "
                "an evidence claim with nothing quoted is not evidence"
            )
        roles: set[str] = set()
        for index, receipt in enumerate(receipts):
            if not (receipt.get("source") and receipt.get("quote")):
                raise DiagnosticError(
                    f"run evidence for {iso}: receipt {index} needs both a "
                    "'source' and a verbatim 'quote'"
                )
            kind = receipt.get("kind")
            if kind not in RECEIPT_KINDS:
                raise DiagnosticError(
                    f"run evidence for {iso}: receipt {index} has kind {kind!r}; "
                    f"every receipt must declare one of {list(RECEIPT_KINDS)} so a "
                    "quoted line is not confused with a derived count"
                )
            roles.update(receipt.get("roles") or [])
        if verdict == "job_never_fired":
            missing = [role for role in REQUIRED_ABSENCE_ROLES if role not in roles]
            if missing:
                raise DiagnosticError(
                    f"run evidence for {iso}: verdict 'job_never_fired' is an "
                    f"absence argument and its denominator is incomplete — no "
                    f"receipt carries {missing}"
                )
            if not payload.get("firing_signature"):
                raise DiagnosticError(
                    f"run evidence for {iso}: verdict 'job_never_fired' needs the "
                    "file to state the 'firing_signature' that was searched for; "
                    "an absence is meaningless without saying what was absent"
                )
    return payload


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

    Raises ``json.JSONDecodeError`` for a payload that exists and does not
    parse; the caller records that as a corrupt cache rather than a missing
    one. A truncated file is exactly what a run killed mid-write leaves behind,
    which is the subject of three days in this very window, so it has to be a
    reported condition and not an unhandled traceback that aborts the report.
    """
    payload = _load_json(results_dir / f"{day.isoformat()}.json")
    if payload is None:
        return None, {}
    return _walk_schedule(payload)


def is_mlb_lane(name: str) -> bool:
    """True when a listed file belongs to the MLB daily lane.

    The MLB slate writes at the TOP level of each ``.picks`` subdirectory
    (``slate/2026-08-20.md``); the other sports lanes and the rerun archive
    write into a nested directory of their own (``slate/nfl/2026-08-26.md``,
    ``slate/archive/...``). The nesting IS the lane marker in this corpus.

    Enumeration is deliberately wider than classification, and this predicate
    is the seam between them: every file for a date is listed so a reader can
    see what is genuinely there, but only an MLB-lane file may decide an MLB
    day class. Letting an NFL writeup satisfy "the scan produced an artifact"
    would be the same defect as missing it — silence in one direction, a false
    positive in the other — and this report is about a drought in one lane.
    """
    return name.count("/") == 1


def enumerate_day(roots: list[dict[str, Any]], day: dt.date) -> dict[str, list[str]]:
    """List every file each root holds for one date, INCLUDING the empty lists.

    A root that has nothing for a date must appear with ``[]`` rather than be
    omitted. An absent key and an empty list read the same to a skimmer and mean
    opposite things to anyone checking whether a root was searched at all — and
    "was it searched" is precisely the question the 2026-08-20 miss raised.

    **The walk is RECURSIVE.** The first version of this function globbed the
    top level of three fixed subdirectories, which made a date-named file one
    directory down invisible — and the report then rendered that invisibility
    as a bare "—", i.e. as absence. That is the third instance of the very
    pattern this report names: a lookup scoped one notch too narrow returns
    silence, and silence reads as "there was nothing there". It cost a real
    file (``slate/nfl/2026-08-26.md``, 10942 B in the secondary root) and it
    falsified a stated inference about that root no longer being written to.

    Paths are returned relative to the root so the depth is visible in the
    listing itself rather than flattened away.
    """
    iso = day.isoformat()
    listing: dict[str, list[str]] = {}
    for root in roots:
        found: list[str] = []
        for sub in PICKS_SUBDIRS:
            directory = root["_dirs"][sub]
            if not directory.is_dir():
                continue
            found += sorted(
                f"{sub}/{p.relative_to(directory).as_posix()}"
                for p in directory.rglob(f"{iso}*")
                if p.is_file()
            )
        listing[root["label"]] = found
    return listing


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
        # Each number carries the team it belongs to. A bare "2-3" is only
        # readable if you already know the convention, and the away-home
        # ordering was in fact read backwards off this very field.
        "outcome_score": (
            f"{game.get('away_team')} {game.get('away_score')} at "
            f"{game.get('home_team')} {game.get('home_score')}"
        ),
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
    roots: list[dict[str, Any]],
    ledger_by_date: dict[str, list[dict[str, Any]]],
    run_evidence_dates: dict[str, Any] | None = None,
    fetch_outcomes: bool = False,
) -> dict[str, Any]:
    """Build the full trace for a single day. Never raises on a missing file."""
    iso = day.isoformat()
    run_evidence_dates = run_evidence_dates or {}
    files_by_root = enumerate_day(roots, day)
    # The listing is exhaustive; the classification is lane-scoped. Keeping both
    # in the record is what lets a reader see that a date the MLB lane produced
    # nothing for is still a date some OTHER lane wrote on — which is a fact
    # about the checkout, not about the drought.
    mlb_files_by_root = {
        label: [name for name in names if is_mlb_lane(name)]
        for label, names in files_by_root.items()
    }
    other_lane_files_by_root = {
        label: [name for name in names if not is_mlb_lane(name)]
        for label, names in files_by_root.items()
    }

    # Scan roots to the first schedule JSON that PARSES, keeping the provenance
    # of every root that had an unreadable one. Stopping at the first PRESENT
    # copy would let a corrupt file in the primary root veto a valid one behind
    # it — PR #61 lost a day to that shape, in the other direction.
    slate: Any | None = None
    slate_json_root: str | None = None
    corrupt: list[dict[str, str]] = []
    for root in roots:
        schedule_path = root["_dirs"]["execute"] / f"{iso}-schedule.json"
        try:
            payload = _load_json(schedule_path)
        except json.JSONDecodeError as exc:
            corrupt.append(
                {
                    "root": root["label"],
                    "detail": f"{schedule_path.name} is not valid JSON: {exc}",
                }
            )
            continue
        if payload is not None:
            slate, slate_json_root = payload, root["label"]
            break

    # Every writeup for the day, morning and evening, from every root. A day can
    # have both, and — as 2026-08-20 proved — a root the primary lacks.
    writeups = [
        {"root": label, "name": name.split("/", 1)[1]}
        for label, names in mlb_files_by_root.items()
        for name in names
        if name.startswith("slate/") and name.endswith(".md")
    ]

    games_scheduled: int | None = None
    outcomes: dict[str, Any] = {}
    schedule_cache_root: str | None = None
    cache_corrupt: list[dict[str, str]] = []
    for root in roots:
        try:
            count, walked = scheduled_games(root["_dirs"]["audit-results"], day)
        except json.JSONDecodeError as exc:
            # Same treatment the execute-side schedule already got: a file that
            # exists and will not parse is a reported condition, and the scan
            # continues into the next root rather than the report dying.
            cache_corrupt.append(
                {"root": root["label"], "detail": f"{iso}.json is not valid JSON: {exc}"}
            )
            continue
        if count is not None:
            games_scheduled, outcomes = count, walked
            schedule_cache_root = root["label"]
            break

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

    # "The scan produced an artifact" is a narrower question than "this root
    # holds a file for this date", and the two were previously conflated. The
    # schedule CACHE is written by the run before it produces anything: a day
    # whose only MLB-lane file is that cache is a run that started and never
    # reached its own output. The corpus can say so on its own, which makes it
    # a classification and not a gap for evidence to fill.
    has_artifact = slate is not None or bool(writeups) or bool(corrupt)
    roots_with_mlb_files = sorted(
        label for label, names in mlb_files_by_root.items() if names
    )
    corpus_shows_partial_run = not has_artifact and bool(roots_with_mlb_files)

    # Run evidence is CONSULTED only when the corpus has nothing AT ALL in the
    # MLB lane, and the day record says so either way. An artifact is a stronger
    # fact than a log line: the file is still there to re-read, while the logs
    # behind the evidence rotate. Recording `applied: false` next to an unused
    # verdict makes that precedence visible in the artifact, not only here.
    evidence = run_evidence_dates.get(iso)
    evidence_applies = not has_artifact and not roots_with_mlb_files
    run_evidence: dict[str, Any] | None = None
    if evidence is not None:
        if evidence_applies:
            not_applied_reason = None
        elif has_artifact:
            not_applied_reason = (
                "the corpus has an artifact for this date; it outranks run evidence"
            )
        else:
            not_applied_reason = (
                "the corpus holds an MLB-lane file for this date, so the run's "
                "own trace outranks run evidence"
            )
        run_evidence = {
            "verdict": evidence.get("verdict"),
            "basis": evidence.get("basis"),
            "receipts": evidence.get("receipts") or [],
            "applied": evidence_applies,
            "not_applied_reason": not_applied_reason,
        }

    if corpus_shows_partial_run:
        day_class = "scan_ran_artifact_unwritten"
        day_class_source = "corpus"
    elif not has_artifact:
        day_class = (evidence or {}).get("verdict") or "no_slate_artifact"
        day_class_source = "run_evidence" if evidence else "corpus"
    elif any(c["stop"] == "executed" for c in candidates) or executed_ledger:
        day_class = "candidates_executed"
        day_class_source = "corpus"
    elif candidates:
        day_class = "candidates_rejected"
        day_class_source = "corpus"
    elif watchlist:
        day_class = "watchlist_only"
        day_class_source = "corpus"
    else:
        day_class = "slate_empty"
        day_class_source = "corpus"

    return {
        "date": iso,
        "day_class": day_class,
        "day_class_source": day_class_source,
        "artifact_present": has_artifact,
        "games_scheduled": games_scheduled,
        "slate_json_present": slate is not None,
        "slate_json_root": slate_json_root,
        "slate_json_corrupt": corrupt,
        "schedule_cache_root": schedule_cache_root,
        "schedule_cache_corrupt": cache_corrupt,
        "files_by_root": files_by_root,
        "mlb_lane_files_by_root": mlb_files_by_root,
        "other_lane_files_by_root": other_lane_files_by_root,
        "roots_with_files": sorted(
            label for label, names in files_by_root.items() if names
        ),
        "roots_with_mlb_lane_files": roots_with_mlb_files,
        "run_evidence": run_evidence,
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
    extra_picks_dirs: list[Path] | None = None,
    results_dir: Path | None = None,
    slate_dir: Path | None = None,
    ledger_path: Path | None = None,
    run_evidence: dict[str, Any] | None = None,
    runtime_revision: str | None = None,
    repo_revision: str | None = None,
    fetch_outcomes: bool = False,
) -> dict[str, Any]:
    roots = picks_roots(picks_dir, extra_picks_dirs)
    # The explicit --results-dir/--slate-dir overrides address the PRIMARY root
    # only. They exist for a corpus assembled out of place; a secondary root is
    # always a real `.picks` tree and keeps its own layout.
    if results_dir is not None:
        roots[0]["_dirs"]["audit-results"] = results_dir
    if slate_dir is not None:
        roots[0]["_dirs"]["slate"] = slate_dir

    ledger_by_date, ledger_gap = _ledger_by_date(ledger_path)
    run_evidence = run_evidence or {}
    run_evidence_dates = run_evidence.get("dates") or {}

    days = [
        analyze_day(
            day,
            roots=roots,
            ledger_by_date=ledger_by_date,
            run_evidence_dates=run_evidence_dates,
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
    corrupt_cache = [
        {"date": d["date"], **entry}
        for d in days
        for entry in d["schedule_cache_corrupt"]
    ]
    if corrupt_cache:
        data_gaps.append(
            {
                "kind": "corrupt_schedule_cache",
                "detail": (
                    "a cached MLB schedule exists and does not parse, so the day "
                    "has no denominator. A truncated file is what a run killed "
                    "mid-write leaves behind — reported, never fatal"
                ),
                "entries": corrupt_cache,
            }
        )
    data_gaps.append(
        {
            "kind": "root_list_provenance",
            "detail": (
                "the set of .picks roots searched is an input to this report, "
                "supplied on the command line, and their labels are the parent "
                "directory names of those paths. Nothing here measures whether "
                "the list is complete: a root nobody named cannot be reported "
                "missing. This is the residual of the 2026-08-20 miss itself"
            ),
            "roots": [root["label"] for root in roots],
        }
    )
    silent_days = [d["date"] for d in days if d["day_class"] == "no_slate_artifact"]
    if silent_days:
        data_gaps.append(
            {
                "kind": "no_slate_artifact",
                "detail": (
                    "no artifact in any searched root and no run evidence "
                    "explaining the silence. The corpus alone cannot distinguish "
                    "'the job did not run' from 'the job ran and wrote nothing'; "
                    "that needs scheduler state, supplied via --run-evidence"
                ),
                "dates": silent_days,
            }
        )
    for question in run_evidence.get("open_questions") or []:
        data_gaps.append(
            {
                "kind": "run_evidence_open_question",
                "detail": question.get("question", ""),
                "dates": question.get("dates") or [],
                "why_unanswerable": question.get("why_unanswerable"),
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
            "picks_roots": [
                {k: v for k, v in root.items() if not k.startswith("_")}
                for root in roots
            ],
            "ledger": portable(ledger_path),
            "run_evidence": {
                "schema": run_evidence.get("schema"),
                "collected_at_utc": run_evidence.get("collected_at_utc"),
                "collected_by": run_evidence.get("collected_by"),
                "sources": run_evidence.get("sources") or [],
                "artifact_receipts": run_evidence.get("artifact_receipts") or [],
            }
            if run_evidence
            else None,
            "repo_revision": repo_revision,
            "runtime_revision": runtime_revision,
        },
        "enumeration": enumeration_summary(days, roots),
        "findings": findings(days, roots),
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


def enumeration_summary(
    days: list[dict[str, Any]], roots: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-root coverage of the window, and the dates only one root has.

    Two entries, and the difference between them matters. ``dates_in_one_root
    _only`` is the symmetric answer to "was every date checked against every
    root": any date here is one a single-root run would have described wrongly
    in one direction or the other. Most of them are benign — the secondary root
    simply stopped being written to partway through the window.

    ``dates_missing_from_primary`` is the subset that is a defect: a date the
    primary root does NOT have in the MLB lane and another root does. Those are
    the days a primary-only enumeration reports as "no artifact" when the file
    exists. Reporting only the symmetric list would bury one real miss under ten
    benign ones; reporting only the asymmetric one would not answer whether the
    rest of the window was checked at all. Both are derived from the listings
    rather than asserted, so neither can drift away from the data.

    ``last_date_with_files_per_root`` exists because an inference about a root
    having "stopped being written to" is exactly the kind of claim that gets
    written into prose and then quietly falsified. It is measured here, in both
    scopes, so the sentence in the rendered report can be generated from the
    data instead of asserted next to it — the first draft of this report said
    the secondary checkout stopped receiving writes after 08-22 while that
    checkout held a 10942 B file written on 08-26.
    """
    labels = [root["label"] for root in roots]
    per_root = {
        label: sorted(d["date"] for d in days if d["files_by_root"].get(label))
        for label in labels
    }
    per_root_mlb = {
        label: sorted(d["date"] for d in days if d["mlb_lane_files_by_root"].get(label))
        for label in labels
    }
    one_root_only = []
    for day in days:
        present = day["roots_with_files"]
        if len(present) == 1 and len(labels) > 1:
            one_root_only.append(
                {
                    "date": day["date"],
                    "present_in": present[0],
                    "absent_from": [lb for lb in labels if lb != present[0]],
                    "files": day["files_by_root"][present[0]],
                }
            )
    primary = labels[0] if labels else None
    missing_from_primary = [
        {
            "date": day["date"],
            "present_in": day["roots_with_mlb_lane_files"],
            "files": {
                label: day["mlb_lane_files_by_root"][label]
                for label in day["roots_with_mlb_lane_files"]
            },
        }
        for day in days
        if day["roots_with_mlb_lane_files"]
        and primary not in day["roots_with_mlb_lane_files"]
    ]
    other_lane = [
        {
            "date": day["date"],
            "files": {
                label: names
                for label, names in day["other_lane_files_by_root"].items()
                if names
            },
        }
        for day in days
        if any(day["other_lane_files_by_root"].values())
    ]
    return {
        "roots_searched": labels,
        "primary_root": primary,
        "enumeration_is_recursive": True,
        "lane_scope_note": (
            "Every file for a date is listed, at any depth under "
            f"{list(PICKS_SUBDIRS)}. Only a file at the TOP level of one of "
            "those directories is an MLB-lane file; a nested directory is "
            "another sport's lane or the rerun archive. Day classes are decided "
            "from MLB-lane files only, and both listings are carried per date."
        ),
        "root_list_provenance": (
            "The roots searched are the ones supplied on the command line, and "
            "their labels are the parent directory names of those paths. "
            "Completeness of this list is an INPUT to the report, not something "
            "the report measured — a root nobody named cannot be found missing. "
            "That residual is the 2026-08-20 miss itself, one level up."
        ),
        "dates_with_files_per_root": {label: len(v) for label, v in per_root.items()},
        "dates_with_mlb_lane_files_per_root": {
            label: len(v) for label, v in per_root_mlb.items()
        },
        "last_date_with_files_per_root": {
            label: (v[-1] if v else None) for label, v in per_root.items()
        },
        "last_date_with_mlb_lane_files_per_root": {
            label: (v[-1] if v else None) for label, v in per_root_mlb.items()
        },
        "dates_with_no_files_in_any_root": sorted(
            d["date"] for d in days if not d["roots_with_files"]
        ),
        "dates_with_no_mlb_lane_files_in_any_root": sorted(
            d["date"] for d in days if not d["roots_with_mlb_lane_files"]
        ),
        "dates_in_one_root_only": one_root_only,
        "dates_missing_from_primary": missing_from_primary,
        "dates_with_other_lane_files": other_lane,
    }


def findings(days: list[dict[str, Any]], roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Named patterns the window exposed, each with its concrete instances.

    Both instances of ``namespace_silence`` are the same defect wearing
    different clothes: a lookup issued against the wrong namespace returns
    nothing, and nothing is indistinguishable from "there was nothing there".
    Neither raises, neither logs, and both read downstream as an honest absence.
    This is the third time this fleet has been bitten by that shape, which is
    why it is recorded as ONE pattern rather than two unrelated bugs.
    """
    instances = [
        {
            "instance": "event_id joined against gamePk",
            "detail": (
                "the slate's event_id is a different id space from the MLB "
                "gamePk — 2026-08-30 records event_id 401816733 for the game the "
                "schedule calls gamePk 824876. Joining outcomes on the id matches "
                "nothing and reads as 'no outcome data'"
            ),
            "mitigation": "the outcome join is keyed on the matchup, never on an id",
        }
    ]
    # Keyed on the ASYMMETRIC miss — a date some root has in the MLB lane and
    # the primary does not — because that is the direction in which the silence
    # produced a wrong answer. A date the primary has alone is not a defect.
    primary = roots[0]["label"] if roots else None
    missing_from_primary = [
        d
        for d in days
        if d["roots_with_mlb_lane_files"]
        and primary not in d["roots_with_mlb_lane_files"]
    ]
    if missing_from_primary:
        instances.append(
            {
                "instance": "corpus enumerated from one .picks root",
                "detail": (
                    "the daily slate wrote into more than one checkout across "
                    "this window, so a report built from the primary root alone "
                    "reported an existing artifact as absent"
                ),
                "mitigation": "every date is enumerated across every known root",
                "dates": [
                    {"date": d["date"], "present_in": d["roots_with_mlb_lane_files"]}
                    for d in missing_from_primary
                ],
            }
        )
    # The third instance is this report's own enumeration, and it belongs in the
    # list precisely because it was found by the same reading that found the
    # other two. The walk covered the top level of three directories and the
    # rendered table printed the resulting blank as "—" — absence, not "not
    # searched". A date-named file one directory down was invisible.
    nested = [d for d in days if any(d["other_lane_files_by_root"].values())]
    if nested:
        instances.append(
            {
                "instance": "enumeration scoped one directory level too shallow",
                "detail": (
                    "the first version of this report globbed only the top level "
                    "of each .picks subdirectory, so date-named files in a nested "
                    "lane directory were listed as nothing at all and rendered as "
                    "absence. The walk is now recursive and the lane of each file "
                    "is recorded rather than inferred from whether it was seen"
                ),
                "mitigation": (
                    "the walk is recursive; MLB-lane and other-lane files are "
                    "listed separately and only the MLB lane decides a day class"
                ),
                "dates": [
                    {
                        "date": d["date"],
                        "other_lane_files": {
                            label: names
                            for label, names in d["other_lane_files_by_root"].items()
                            if names
                        },
                    }
                    for d in nested
                ],
            }
        )
    return [
        {
            "pattern": "namespace_silence",
            "detail": (
                "a lookup against the wrong namespace returns silence, and "
                "silence reads as absence. It never raises and never logs, so "
                "the wrong answer arrives looking like a finding"
            ),
            "instances": instances,
        }
    ]


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
        {
            # The three-way split has to stay exhaustive over the same
            # population the old single class covered, and the population is
            # "the scan produced no artifact" — NOT "no file exists". Those two
            # came apart on a day whose only file is the schedule cache, and
            # keying the check on the file listing made a day the report
            # classified perfectly well read as a reconciliation failure.
            "check": "days with no scan artifact are exactly the no-artifact split",
            "expected": sum(1 for d in days if not d["artifact_present"]),
            "actual": (
                class_counts["job_never_fired"]
                + class_counts["scan_ran_artifact_unwritten"]
                + class_counts["no_slate_artifact"]
            ),
        },
        {
            # An evidence verdict must never be recorded as applied on a day the
            # corpus could class itself. This check is the artifact's own copy of
            # that precedence, and it covers BOTH ways the corpus can speak: a
            # finished artifact, or an MLB-lane trace of a run that started.
            "check": "applied run evidence only ever explains a day with no MLB-lane file",
            "expected": 0,
            "actual": sum(
                1
                for d in days
                if (d.get("run_evidence") or {}).get("applied")
                and d["roots_with_mlb_lane_files"]
            ),
        },
        {
            # A day classified from the corpus must not be reported as though
            # evidence decided it, and vice versa. The provenance of a class is
            # the whole reason the evidence lane is trustworthy.
            "check": "every run_evidence-sourced class was actually applied evidence",
            "expected": sum(1 for d in days if d["day_class_source"] == "run_evidence"),
            "actual": sum(
                1 for d in days if (d.get("run_evidence") or {}).get("applied")
            ),
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
    enumeration = report.get("enumeration") or {}
    roots = enumeration.get("roots_searched") or []
    lines += ["", "## Day by day", ""]
    header = "| date | class | games | cands | watch |"
    divider = "| --- | --- | --- | --- | --- |"
    for label in roots:
        header += f" files in `{label}` |"
        divider += " --- |"
    lines += [header, divider]
    for day in report["days"]:
        games = "—" if day["games_scheduled"] is None else day["games_scheduled"]
        row = (
            f"| {day['date']} | `{day['day_class']}` | {games} | "
            f"{day['counts']['candidates']} | {day['counts']['watchlist_entries']} |"
        )
        for label in roots:
            found = day["files_by_root"].get(label) or []
            row += (" " + ", ".join(f"`{n}`" for n in found) if found else " —") + " |"
        lines.append(row)
    lines += [
        "",
        "Listings are RECURSIVE over "
        + ", ".join(f"`{sub}/`" for sub in PICKS_SUBDIRS)
        + ". A path with a directory in it (`slate/nfl/…`) is another sport's "
        "lane or the rerun archive, not the MLB daily lane, and does not decide "
        "an MLB day class. A `—` means the root was searched and held nothing "
        "for that date, at any depth.",
    ]

    evidenced = [d for d in report["days"] if (d.get("run_evidence") or {}).get("applied")]
    if evidenced:
        lines += [
            "",
            "## Run evidence — days the corpus cannot explain by itself",
            "",
            "Quoted because the sources behind them rotate and cannot be "
            "re-derived later. Consulted only for a date with no MLB-lane file "
            "in any root. Each receipt is labelled **verbatim** — a line quoted "
            "character-for-character — or **derived**, a measurement a reader "
            "can re-run but not re-read. Roles name the part of the argument a "
            "receipt carries; for an absence argument every role in "
            f"{list(REQUIRED_ABSENCE_ROLES)} must be present or the file is "
            "refused.",
        ]
        for day in evidenced:
            ev = day["run_evidence"]
            lines += ["", f"### {day['date']} — `{ev['verdict']}`", "", ev.get("basis") or ""]
            for receipt in ev["receipts"]:
                roles = receipt.get("roles") or []
                suffix = f" _[{', '.join(roles)}]_" if roles else ""
                lines.append(
                    f"- **{receipt.get('kind')}** · `{receipt['source']}` — "
                    f"`{receipt['quote']}`{suffix}"
                )

    receipts = ((report["sources"].get("run_evidence") or {}).get("artifact_receipts")) or []
    if receipts:
        lines += [
            "",
            "## Artifact receipts",
            "",
            "Fingerprints for the files a reader would otherwise have to take on "
            "description. Size, mtime and hash make the claim checkable.",
            "",
            "| date | root | file | size | mtime (UTC) | sha256 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for receipt in receipts:
            lines.append(
                f"| {receipt['date']} | `{receipt['root']}` | `{receipt['file']}` | "
                f"{receipt['size']} | {receipt['mtime_utc']} | `{receipt['sha256']}` |"
            )

    if roots:
        lines += [
            "",
            "## Roots searched",
            "",
            enumeration.get("root_list_provenance") or "",
            "",
            "| label | role | path | dates with files | MLB-lane dates | last file | last MLB-lane file |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        counts = enumeration.get("dates_with_files_per_root") or {}
        mlb_counts = enumeration.get("dates_with_mlb_lane_files_per_root") or {}
        last_any = enumeration.get("last_date_with_files_per_root") or {}
        last_mlb = enumeration.get("last_date_with_mlb_lane_files_per_root") or {}
        for root in report["sources"]["picks_roots"]:
            label = root["label"]
            lines.append(
                f"| `{label}` | {root['role']} | `{root['path']}` | "
                f"{counts.get(label, 0)} | {mlb_counts.get(label, 0)} | "
                f"{last_any.get(label) or '—'} | {last_mlb.get(label) or '—'} |"
            )
        # Generated from the measurement, not asserted beside it. The two dates
        # differ for the secondary root in this window, and the first draft of
        # this report inferred from the MLB-lane one that the checkout had gone
        # quiet — while it was still being written to four days later.
        primary_label = enumeration.get("primary_root")
        for root in report["sources"]["picks_roots"]:
            label = root["label"]
            if label == primary_label:
                continue
            any_date, mlb_date = last_any.get(label), last_mlb.get(label)
            if any_date and mlb_date and any_date != mlb_date:
                lines += [
                    "",
                    f"**`{label}` last received an MLB-lane file on {mlb_date}, "
                    f"but was still being written to on {any_date}.** Those are "
                    "different facts, and reading the first as the second is how "
                    "a checkout gets described as stale while it is in use.",
                ]
        missing = enumeration.get("dates_missing_from_primary") or []
        lines += [
            "",
            f"**Dates the primary root `{enumeration.get('primary_root')}` does not "
            f"have in the MLB lane and another root does: {len(missing)}.** These "
            "are the days a primary-only enumeration reports as having no artifact "
            "when the file exists.",
            "",
        ]
        for entry in missing:
            lines.append(
                f"- **{entry['date']}** — in "
                f"{', '.join('`' + label + '`' for label in entry['present_in'])}: "
                + ", ".join(
                    f"`{name}`"
                    for names in entry["files"].values()
                    for name in names
                )
            )
        only = enumeration.get("dates_in_one_root_only") or []
        if only:
            lines += [
                "",
                f"For completeness, all {len(only)} dates present in exactly one "
                "root, in either direction. A date present only in the PRIMARY "
                "root is not a defect — it is a date the secondary checkout has "
                "nothing for, which the table above dates precisely rather than "
                "explaining away:",
                "",
            ]
            for entry in only:
                lines.append(
                    f"- {entry['date']} — only in `{entry['present_in']}`, "
                    f"absent from {', '.join('`' + a + '`' for a in entry['absent_from'])}"
                )
        other_lane = enumeration.get("dates_with_other_lane_files") or []
        if other_lane:
            lines += [
                "",
                f"Dates carrying files outside the MLB lane: {len(other_lane)}. "
                "Listed because they are real files a shallower walk did not see, "
                "and excluded from every day class because they are not this "
                "lane's output:",
                "",
            ]
            for entry in other_lane:
                rendered = "; ".join(
                    f"`{label}`: " + ", ".join(f"`{n}`" for n in names)
                    for label, names in entry["files"].items()
                )
                lines.append(f"- {entry['date']} — {rendered}")

    for finding in report.get("findings") or []:
        detail = finding["detail"]
        lines += ["", f"## Finding — `{finding['pattern']}`", "", detail[:1].upper() + detail[1:] + ".", ""]
        for instance in finding["instances"]:
            lines.append(f"- **{instance['instance']}** — {instance['detail']}. "
                         f"_Mitigation:_ {instance['mitigation']}.")

    if report["data_gaps"]:
        lines += ["", "## Data gaps"]
        for gap in report["data_gaps"]:
            detail = gap.get("dates") or gap.get("entries") or gap.get("roots") or ""
            lines.append(f"- **{gap['kind']}** — {gap['detail']}. {detail}")
    recon = report["reconciliation"]
    lines += ["", f"Reconciliation: {'ok' if recon['ok'] else 'FAILED'}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only day-by-day diagnostic of a no-pick stretch"
    )
    parser.add_argument("--picks-dir", required=True, help="the primary .picks directory")
    parser.add_argument(
        "--also-picks-dir",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "an additional .picks root to enumerate, repeatable. Every date is "
            "searched in every root; the first root with a usable copy wins for "
            "any single-valued fact. Needed whenever the window spans a deploy "
            "cutover — 2026-08-20's slate landed in a different checkout."
        ),
    )
    parser.add_argument(
        "--run-evidence",
        help=(
            "JSON of externally-collected scheduler evidence. Consulted ONLY for "
            "a date with no artifact in any root, and only to assign "
            f"{list(EVIDENCE_ASSIGNABLE_CLASSES)}."
        ),
    )
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
        evidence = (
            load_run_evidence(Path(args.run_evidence).expanduser())
            if args.run_evidence
            else None
        )
        report = build_report(
            picks_dir=Path(args.picks_dir).expanduser(),
            extra_picks_dirs=[Path(p).expanduser() for p in args.also_picks_dir],
            run_evidence=evidence,
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
