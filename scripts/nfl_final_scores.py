#!/usr/bin/env python3
"""Deterministic NFL final scores for settlement.

Fetches the ESPN NFL scoreboard for a date and prints a JSON list of
completed games only: {event_id, away, home, away_score, home_score,
status, winner}. The settlement agent calls this instead of LLM
web-fetching scores. Ties (winner: null with both scores present) are
possible in the NFL regular season.

Usage:
  python scripts/nfl_final_scores.py --date 2026-09-13
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

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={date}&limit=100"


def _score(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def final_scores(scoreboard: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in scoreboard.get("events", []):
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions") or []
        if not competitions or not isinstance(competitions[0], dict):
            continue
        competition = competitions[0]
        status_type = ((competition.get("status") or {}).get("type") or {})
        if not status_type.get("completed"):
            continue
        competitors = {
            c.get("homeAway"): c
            for c in competition.get("competitors", [])
            if isinstance(c, dict)
        }
        away = competitors.get("away", {})
        home = competitors.get("home", {})
        away_name = (away.get("team") or {}).get("displayName")
        home_name = (home.get("team") or {}).get("displayName")
        away_score = _score(away.get("score"))
        home_score = _score(home.get("score"))
        winner: str | None = None
        if away_score is not None and home_score is not None:
            if away_score > home_score:
                winner = away_name
            elif home_score > away_score:
                winner = home_name
        rows.append(
            {
                "event_id": event.get("id"),
                "away": away_name,
                "home": home_name,
                "away_score": away_score,
                "home_score": home_score,
                "status": status_type.get("name"),
                "winner": winner,
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic NFL final scores for settlement")
    parser.add_argument("--date", required=True, help="Game date YYYY-MM-DD")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        parser.error("--date must be YYYY-MM-DD")

    url = SCOREBOARD_URL.format(date=args.date.replace("-", ""))
    scoreboard = fetch_json(url, timeout=30, attempts=3)
    if not isinstance(scoreboard, dict):
        print(json.dumps({"error": "ESPN scoreboard returned a non-object response"}), file=sys.stderr)
        return 1
    print(json.dumps(final_scores(scoreboard), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
