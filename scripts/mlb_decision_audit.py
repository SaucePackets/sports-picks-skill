#!/usr/bin/env python3
"""Read-only accounting of candidate defects, price-qualified refusals and rechecks.

The report flags recorded evidence for investigation. It cannot establish a
missed profitable bet, historical input freshness, or model qualification.
"""

from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import hashlib
import os
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mlb_eligibility_report as eligibility
import mlb_lineup_watchlist as watchlist
from mlb_candidate_contract import candidate_errors
from mlb_runtime_policy import (
    MARKET_MODEL_VERSION,
    load_mlb_selection_policy,
    model_deployment_errors,
)


def build_audit(document, policy, *, now, state_dir=None, research=None):
    if not isinstance(document, dict):
        raise ValueError("schedule must be an object")
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    report = eligibility.build_report(document, policy)
    cards = document.get("candidates", [])
    watches = document.get("lineup_watchlist", [])
    reads = document.get("game_reads", [])
    errors = []
    for name, value in (
        ("candidates", cards),
        ("lineup_watchlist", watches),
        ("game_reads", reads),
    ):
        if not isinstance(value, list) or any(
            not isinstance(row, dict) for row in value
        ):
            errors.append(f"{name} must be a list of objects")
    result = {
        "schema": "vig-mlb-decision-audit-v1",
        "as_of_utc": now.isoformat(),
        "execution_enabled": False,
        "eligibility": report,
        "input_errors": errors,
        "candidate_contract_errors": [],
        "games": [],
        "refusal_counts": {},
    }
    if errors:
        return result
    result["candidate_contract_errors"] = [
        {
            "event_id": c.get("event_id"),
            "errors": candidate_errors(c, state_dir=state_dir),
        }
        for c in cards
    ]
    try:
        due = watchlist.due_entries(document, now)
        result["due_watchlist_ids"] = [watchlist.entry_id(e) for e in due]
    except watchlist.WatchlistFormatError as exc:
        result["due_watchlist_ids"] = None
        errors.append(str(exc))
    result["recheck_warnings"] = watchlist.overdue_recheck_warnings(document, now)
    block = document.get("slate_denominator")
    denominator = block.get("games") if isinstance(block, dict) else None
    if not isinstance(denominator, list) or any(
        not isinstance(g, dict) for g in denominator
    ):
        errors.append("slate_denominator.games missing or malformed; coverage unknown")
    else:
        expected = Counter(str(g.get("game_pk")) for g in denominator)
        actual = Counter(str(g.get("game_pk")) for g in reads)
        result["missing_game_pks"] = list((expected - actual).elements())
        result["extra_game_pks"] = list((actual - expected).elements())
        result["duplicate_game_pks"] = sorted(k for k, n in actual.items() if n > 1)
    rails = Counter()
    for read, game in zip(reads, report["games"]):
        identity = read.get("game_pk")
        event = read.get("event_id")
        unique_event = (
            event is not None
            and sum(str(r.get("event_id")) == str(event) for r in reads) == 1
        )
        linked = [
            w
            for w in watches
            if (identity is not None and w.get("game_pk") == identity)
            or (
                w.get("game_pk") is None
                and unique_event
                and str(w.get("event_id")) == str(event)
            )
        ]
        model_errors = model_deployment_errors(read, state_dir)
        if read.get("model_version") == MARKET_MODEL_VERSION:
            if (
                read.get("raw_probability") != read.get("dk_fair_prob")
                or read.get("conservative_probability") != read.get("dk_fair_prob")
                or read.get("uncertainty_haircut") != 0
            ):
                model_errors.append(
                    "market-only read carries adjusted or missing probabilities"
                )
        if not read.get("model_version") and read.get("raw_probability") is not None:
            model_errors.append("model_version missing")
        refusal = game["verdict"] == eligibility.SIDE_ELIGIBLE and read.get(
            "disposition"
        ) in {"pass", "incomplete_input_data"}
        rails.update(game["refusing_rails"])
        research_item = (research or {}).get("games", {}).get(str(identity), {})
        if not isinstance(research_item, dict):
            research_item = {}
        expected_identity = research_item.get("identity", [])
        if not isinstance(expected_identity, list) or len(expected_identity) != 5 or expected_identity[:4] != [identity, event, read.get("away"), read.get("home")]:
            research_item = {}
        result["games"].append(
            {
                "game_pk": identity,
                "disposition": read.get("disposition"),
                "price_qualified_refusal": refusal,
                "model_errors": model_errors,
                "refusing_rails": game["refusing_rails"],
                "unavailable": game.get("unavailable", {}),
                "recheck_statuses": [w.get("status") for w in linked],
                "research_status": research_item.get("status"),
                "research_next_retry": research_item.get("next_retry") if research_item.get("status") == "pending" else None,
                "research_deadline": research_item.get("deadline"),
                "incomplete_without_tracked_followup": read.get("disposition") == "incomplete_input_data" and not linked and not research_item,
                "incomplete_without_linked_recheck": read.get("disposition")
                == "incomplete_input_data"
                and not linked,
                "interpretation": "investigation only; price qualification is not execution eligibility",
            }
        )
    result["refusal_counts"] = dict(sorted(rails.items()))
    return result


def write_snapshot(root, day, *, now=None):
    """Refresh only the audit artifact, binding it to the exact schedule bytes read."""
    source = root / ".picks" / "execute" / f"{day}-schedule.json"
    now = now or datetime.now(timezone.utc)
    try:
        raw = source.read_bytes()
    except FileNotFoundError:
        report = {
            "schema": "vig-mlb-decision-audit-v1",
            "status": "no_schedule",
            "as_of_utc": now.isoformat(),
            "execution_enabled": False,
        }
    else:
        document = json.loads(raw)
        if isinstance(document, list):
            document = {"candidates": document}
        research = None
        queue_path = root / ".picks" / "research" / day / "queue.json"
        queue_error = None
        if queue_path.exists():
            try:
                research = json.loads(queue_path.read_text())
                if (not isinstance(research, dict) or research.get("schema") != "mlb-research-queue-v1"
                        or research.get("day") != day or not isinstance(research.get("games"), dict)
                        or any(not isinstance(item, dict) for item in research["games"].values())):
                    raise ValueError("invalid research queue shape")
            except (OSError, ValueError) as exc:
                research = None
                queue_error = str(exc)
        report = build_audit(document, load_mlb_selection_policy(), now=now, research=research)
        if queue_error:
            report["input_errors"].append("research queue unreadable: " + queue_error)
        report["schedule_sha256"] = hashlib.sha256(raw).hexdigest()
    directory = root / ".picks" / "journal"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{day}-decision-audit.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(report, indent=2) + "\n")
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--now", help="timezone-aware ISO instant; default current UTC")
    args = parser.parse_args(argv)
    try:
        now = (
            datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else datetime.now(timezone.utc)
        )
        report = build_audit(
            json.loads(args.schedule.read_text()),
            load_mlb_selection_policy(args.state_dir),
            now=now,
            state_dir=args.state_dir,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2))
    return 1 if report["input_errors"] or report["eligibility"]["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
