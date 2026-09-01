#!/usr/bin/env python3
"""Land the MLB slate schedule through code, or do not land it at all.

PR #77 made the *checks* reachable: the scan persists its roster, the validator
finds it by convention, the scheduled gate runs the cross-check every cycle and
the receipt writes a verdict from a closed vocabulary. What it did not change is
who writes the record. On 2026-09-01 the producer handicapped fifteen games and
hand-authored a schedule with no ``game_reads`` and no ``slate_denominator``,
and the reason it could is unchanged by any of that work: **the schedule was
authored by the run, from a prompt, and every rail sat downstream of the write.**

Downstream rails tell you afterwards that the record is wrong. They cannot stop
a wrong record from existing, and a schedule is the input the reviewer, the
executor and every later analysis read. So this module is the producer-side
half:

- The **denominator is derived, never transcribed.** It is built here from
  ``.picks/tmp/stage2-<date>.json`` — the artifact ``mlb_stage2_scan.py`` writes
  on every run — and a draft that carries its own ``slate_denominator`` is
  *refused* rather than silently overwritten. A run cannot shrink a roster it
  does not write.
- **One validated read per scheduled game, before the record can land.** The
  composed schedule is put through ``mlb_game_reads.validate_with_denominator``
  and ``mlb_lineup_watchlist.validate_watchlist`` — the same functions the gate
  and the receipt call, not a second opinion — and nothing is written unless
  both come back empty. The write itself is atomic, so a refused landing leaves
  the previous schedule byte-identical rather than half-replaced.
- ``--skeleton`` emits the draft **from the scan**: one stub per scanned game
  carrying both id spaces, the team names and the DK fair prior the scan already
  computed. Enumerating the slate was the producer's job and the omission it got
  wrong; it is now a file. The stub is deliberately *incomplete* — it names no
  disposition and no prices — so an unfilled skeleton cannot land either.

**What this does not close.** Nothing here stops a run from writing
``.picks/execute/<date>-schedule.json`` by hand and skipping this module
entirely. That case is not left open: it is exactly what the postflight receipt
and the scheduled gate already catch, and this change deliberately leaves both
untouched. The claim is narrower and worth stating plainly — an *incomplete
record can no longer land through the supported path*, and the supported path is
now the easier one, because the ids, the roster and the fair priors arrive as a
file instead of as a transcription task.

No network. No order behaviour. No gate, policy or threshold input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import mlb_game_reads  # noqa: E402
import mlb_lineup_watchlist  # noqa: E402

# The scan decided where the roster went; the writer has to look in the same
# place, so it resolves the root through the scan's own function rather than a
# third copy of "find .picks". ``schedule_path_for`` comes from the receipt for
# the same reason: the writer, the receipt and the gate must agree byte-for-byte
# about where a day's schedule lives, and two copies of that is two chances to
# disagree about it.
from mlb_slate_receipt import schedule_path_for  # noqa: E402
from mlb_stage2_scan import denominator_output_path, resolve_scan_root  # noqa: E402

DENOMINATOR_SOURCE = "mlb_stage2_scan"

# Fields the scan carries per game that the denominator records verbatim. Both
# id spaces, always — an ESPN ``event_id`` and an MLB ``game_pk`` are different
# id spaces and anything joining on one of them gets silence.
DENOMINATOR_FIELDS = ("game_pk", "event_id", "away", "home")

# What the draft may not carry, and why. The denominator is the one field whose
# whole value is that the run did not write it.
DERIVED_KEYS = ("slate_denominator",)

# Review and execution state a landing must never clobber. A schedule that has
# been through the reviewer is no longer a slate being produced.
CANDIDATE_STATE_FIELDS = ("vig_approved", "vig_notes", "execution_status")


class SlateWriteError(Exception):
    """A refusal to land, carrying every reason at once.

    Every reason at once and not the first one: a producer that has to re-run
    the whole slate per error is a producer that will stop running the command.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def utc_iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_scan(path: Path) -> list[Any]:
    """The scan roster for this day, or a refusal.

    A MISSING scan is an error and never an empty slate. "Nobody ran the scan"
    and "the scan enumerated nothing" are different facts about the day, and
    only one of them makes a zero read count honest.
    """
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SlateWriteError(
            [
                f"denominator scan not readable at {path}: {exc}; the slate's size "
                "is unknown, so no schedule can be landed for this day — run "
                "scripts/mlb_stage2_scan.py --date <day> first"
            ]
        ) from exc
    if not isinstance(rows, list):
        raise SlateWriteError([f"denominator scan at {path} is not a JSON list of rows"])
    return rows


def unresolved_scan_rows(rows: list[Any]) -> list[str]:
    """Scan rows that could not be pinned to a game, named individually.

    These are fatal — a denominator built from them fails validation anyway —
    but the validator's message is about a field, and the actionable fact is
    *which game the scan could not identify*. The rows are still carried into
    the denominator rather than dropped: a roster that shrinks quietly is the
    defect this whole lane exists to stop.
    """
    problems: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"scan row {index} is not an object")
            continue
        label = row.get("event") or row.get("game_pk") or row.get("event_id") or index
        # The identity rule is the validator's, not a second copy of it: what
        # makes a denominator entry usable and what makes a scan row usable are
        # the same question, and two copies of that answer would drift.
        identity = mlb_game_reads._identity_errors(
            f"scan row {label!r}", {field: row.get(field) for field in DENOMINATOR_FIELDS}
        )
        if identity:
            problems.append(
                "; ".join(identity)
                + (f" ({row['error']})" if isinstance(row.get("error"), str) else "")
                + "; re-run scripts/mlb_stage2_scan.py for this date"
            )
    return problems


def denominator_from_scan(rows: list[Any], fetched_at_utc: str) -> dict[str, Any]:
    """The schedule's ``slate_denominator``, built from the scan and nothing else."""
    games = []
    for row in rows:
        source = row if isinstance(row, dict) else {}
        games.append({field: source.get(field) for field in DENOMINATOR_FIELDS})
    return {
        "source": DENOMINATOR_SOURCE,
        "fetched_at_utc": fetched_at_utc,
        "games": games,
    }


def scan_fetched_at(path: Path) -> str:
    """When the roster was persisted, from the artifact rather than from prose.

    The old contract asked the run to write this timestamp, which made it a
    claim about a scan the writeup could not see. Taking it from the file makes
    it a fact about the file the denominator was actually built from.
    """
    return utc_iso(dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc))


def draft_errors(draft: Any, day: str) -> list[str]:
    """Everything wrong with the draft before a denominator is attached."""
    if not isinstance(draft, dict):
        return ["draft must be a JSON object"]
    errors: list[str] = []
    date = draft.get("date")
    if not isinstance(date, str) or not date.strip():
        errors.append("draft.date must be a non-empty YYYY-MM-DD string")
    elif date.strip() != day:
        errors.append(
            f"draft.date is {date!r} but the day being landed is {day!r}; "
            "a schedule filed under the wrong date is invisible to every later job"
        )
    sport = draft.get("sport")
    if sport != "MLB":
        errors.append(f"draft.sport must be 'MLB', got {sport!r}")
    for key in DERIVED_KEYS:
        if key in draft:
            errors.append(
                f"draft carries {key}; it is derived here from the scan roster and "
                "must never be transcribed by the run — remove it from the draft"
            )
    if not isinstance(draft.get("game_reads"), list):
        errors.append("draft.game_reads must be a list, one entry per scheduled game")
    return errors


def compose(draft: dict[str, Any], denominator: dict[str, Any]) -> dict[str, Any]:
    """The schedule that will be validated, and if it validates, written."""
    schedule = dict(draft)
    schedule["slate_denominator"] = denominator
    schedule.setdefault("candidates", [])
    schedule.setdefault("lineup_watchlist", [])
    return schedule


def record_errors(schedule_path: Path, schedule: dict[str, Any]) -> list[str]:
    """Every defect in the composed record, from the rails that already exist.

    ``validate_with_denominator`` is the function the scheduled gate and the
    receipt both call. Re-deriving the coverage rule here would give the
    producer a second opinion about what a valid record is, and the whole
    point of a preflight check is that it is the SAME check — a landing this
    accepts and the gate rejects is worse than no landing check at all.
    """
    errors = list(mlb_game_reads.validate_with_denominator(schedule_path, schedule))
    for label, entry_errors in sorted(
        mlb_lineup_watchlist.validate_watchlist(schedule).items()
    ):
        for message in entry_errors:
            errors.append(f"lineup_watchlist[{label}]: {message}")
    return errors


def occupancy_errors(schedule_path: Path) -> list[str]:
    """Refuse to overwrite a schedule that has moved on past production.

    Landing is the first write of a slate day. Once the reviewer has ruled on a
    candidate or the executor has stamped one, the schedule carries decisions
    that exist nowhere else, and replacing it wholesale would erase them with
    no trace. There is deliberately no flag to override this: an optional rail
    is the exact shape of defect this lane keeps paying for.
    """
    if not schedule_path.exists():
        return []
    try:
        existing = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [
            f"a schedule already exists at {schedule_path} and could not be parsed "
            f"({exc}); refusing to overwrite a file whose contents are unknown"
        ]
    errors: list[str] = []
    candidates = existing.get("candidates") if isinstance(existing, dict) else None
    for index, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        stamped = [
            field for field in CANDIDATE_STATE_FIELDS if candidate.get(field) is not None
        ]
        if candidate.get("executed"):
            stamped.append("executed")
        if stamped:
            errors.append(
                f"the existing schedule's candidates[{index}] already carries "
                f"{', '.join(sorted(set(stamped)))}; that is a reviewed or executed "
                "card and landing would erase it"
            )
    watchlist = existing.get("lineup_watchlist") if isinstance(existing, dict) else None
    for index, entry in enumerate(watchlist or []):
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        # The constant, never the string. A watchlist entry the producer just
        # wrote is ``pending_lineup_recheck``; restating that here as "pending"
        # would have made every ordinary re-landing look like a rechecked entry
        # and blocked it.
        if status is not None and status != mlb_lineup_watchlist.PENDING_STATUS:
            errors.append(
                f"the existing schedule's lineup_watchlist[{index}] has status "
                f"{status!r}; that entry has been rechecked and landing would erase it"
            )
    return errors


def atomic_write(path: Path, payload: str) -> None:
    """Write via a sibling temp file and one rename.

    The interesting failure is not a crash mid-write; it is that a refused
    landing must leave the previous schedule byte-identical. Truncating in
    place would make a validation failure destructive.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def land(root: Path, day: str, draft: Any) -> tuple[Path, dict[str, Any]]:
    """Validate a draft against the scan roster and write it, or raise.

    Returns the schedule path and the schedule that was written.
    """
    schedule_path = schedule_path_for(root, day)
    scan_path = denominator_output_path(day, root)

    errors = draft_errors(draft, day)
    errors.extend(occupancy_errors(schedule_path))
    try:
        rows = load_scan(scan_path)
    except SlateWriteError as exc:
        # A missing scan is fatal on its own, but a producer fixing one problem
        # at a time is a producer running this command five times: report the
        # draft's defects alongside it rather than only the first wall hit.
        raise SlateWriteError(errors + exc.errors) from exc
    errors.extend(unresolved_scan_rows(rows))
    if not isinstance(draft, dict) or not isinstance(draft.get("game_reads"), list):
        # Nothing further is checkable without a read list, and reporting field
        # errors over a draft with no reads would bury the one that matters.
        raise SlateWriteError(errors)

    schedule = compose(draft, denominator_from_scan(rows, scan_fetched_at(scan_path)))
    errors.extend(record_errors(schedule_path, schedule))
    if errors:
        raise SlateWriteError(errors)

    atomic_write(schedule_path, json.dumps(schedule, indent=2) + "\n")
    return schedule_path, schedule


def skeleton(root: Path, day: str) -> dict[str, Any]:
    """A draft with one stub per scanned game, from the scan's own numbers.

    Deliberately incomplete: no disposition, no ask, no handicap. Those are the
    run's decisions and it must record them. What the run should never have been
    doing by hand is *enumerating the slate* and copying ids across id spaces,
    which is the part that silently went missing on 2026-09-01.
    """
    scan_path = denominator_output_path(day, root)
    rows = load_scan(scan_path)
    reads: list[dict[str, Any]] = []
    for row in rows:
        source = row if isinstance(row, dict) else {}
        stub = {field: source.get(field) for field in DENOMINATOR_FIELDS}
        away_fair = source.get("away_fair")
        home_fair = source.get("home_fair")
        # Only when the scan has BOTH sides as usable probabilities. Half a
        # de-vigged pair is not a fair price, and pre-filling one side would
        # invite a read whose other side was invented to match it.
        if mlb_game_reads._is_probability(away_fair) and mlb_game_reads._is_probability(
            home_fair
        ):
            stub["dk_fair_prob"] = {"away": away_fair, "home": home_fair}
        reads.append(stub)
    return {
        "date": day,
        "sport": "MLB",
        "market_type": "moneyline",
        "candidates": [],
        "lineup_watchlist": [],
        "game_reads": reads,
    }


def default_draft_path(root: Path, day: str) -> Path:
    return root / ".picks" / "tmp" / f"{day}-slate-draft.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--skeleton",
        action="store_true",
        help="write a draft carrying one game_reads stub per scanned game",
    )
    mode.add_argument(
        "--land",
        type=Path,
        metavar="DRAFT",
        help="validate a filled draft against the scan roster and write the schedule",
    )
    parser.add_argument("--day", default=None, help="slate date YYYY-MM-DD (default: today)")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="skeleton destination (default: .picks/tmp/)"
    )
    args = parser.parse_args(argv)

    root = (args.root or resolve_scan_root()).resolve()
    day = args.day or dt.date.today().isoformat()

    if args.skeleton:
        destination = args.out or default_draft_path(root, day)
        if destination.exists():
            print(
                f"error: a draft already exists at {destination}; delete it if you "
                "mean to start the day over — overwriting it would discard reads "
                "that may already be filled in",
                file=sys.stderr,
            )
            return 1
        try:
            draft = skeleton(root, day)
        except SlateWriteError as exc:
            for message in exc.errors:
                print(f"error: {message}", file=sys.stderr)
            return 1
        payload = json.dumps(draft, indent=2) + "\n"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
        print(
            f"draft with {len(draft['game_reads'])} game_reads stub(s) written to "
            f"{destination}",
            file=sys.stderr,
        )
        print(payload, end="")
        return 0

    try:
        draft = json.loads(args.land.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    try:
        schedule_path, schedule = land(root, day, draft)
    except SlateWriteError as exc:
        print(
            json.dumps({"landed": False, "day": day, "errors": exc.errors}, indent=2)
        )
        return 1
    print(
        json.dumps(
            {
                "landed": True,
                "day": day,
                "schedule_path": str(schedule_path),
                "scheduled_games": len(schedule["slate_denominator"]["games"]),
                "reads_recorded": len(schedule["game_reads"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
