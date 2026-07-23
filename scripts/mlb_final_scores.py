#!/usr/bin/env python3
"""Deterministic MLB final scores for settlement.

Fetches the MLB Stats API schedule (with linescore) for a date and prints a
JSON list of Final games only: {gamePk, away, home, away_score, home_score,
status, winner}. The settlement agent calls this instead of LLM web-fetching
scores.

Usage:
  python scripts/mlb_final_scores.py --date 2026-07-22
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

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}&hydrate=linescore"


def _score(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def final_scores(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for date_block in schedule.get("dates", []):
        if not isinstance(date_block, dict):
            continue
        for game in date_block.get("games", []):
            if not isinstance(game, dict):
                continue
            status = game.get("status", {}).get("detailedState")
            if status != "Final":
                continue
            teams = game.get("teams", {})
            away = teams.get("away", {})
            home = teams.get("home", {})
            away_name = away.get("team", {}).get("name")
            home_name = home.get("team", {}).get("name")
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
                    "gamePk": game.get("gamePk"),
                    "away": away_name,
                    "home": home_name,
                    "away_score": away_score,
                    "home_score": home_score,
                    "status": status,
                    "winner": winner,
                }
            )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic MLB final scores for settlement")
    parser.add_argument("--date", required=True, help="Game date YYYY-MM-DD")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        parser.error("--date must be YYYY-MM-DD")

    schedule = fetch_json(SCHEDULE_URL.format(date=args.date), timeout=30, attempts=3)
    if not isinstance(schedule, dict):
        print(json.dumps({"error": "MLB schedule returned a non-object response"}), file=sys.stderr)
        return 1
    print(json.dumps(final_scores(schedule), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
