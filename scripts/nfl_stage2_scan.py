#!/usr/bin/env python3
"""Collect NFL weekly slate context for proposed-card analysis.

Outputs JSON rows with ESPN event/odds, last-5 team form, rest days and
short-week flags, best-effort ESPN injuries, and venue/indoor context.
No betting orders. No prediction-market calls.

Hardening (same contract as mlb_stage2_scan.py):
- HTTP via the shared retry helper (exponential backoff on 429/5xx/network).
- One game's failure emits a partial row with an "error" field instead of
  killing the whole slate.

Usage:
  python scripts/nfl_stage2_scan.py --season 2026 --week 1
  python scripts/nfl_stage2_scan.py --season 2026 --week 2 --seasontype 1   # preseason shakedown
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

from http_util import fetch_json  # noqa: E402

SITE_API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
CORE_API = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"

# A normal NFL week is 7 days of rest (Sunday to Sunday = 6 full days between).
SHORT_WEEK_REST_DAYS = 6


def get(url: str) -> dict[str, Any]:
    """Fetch JSON with retries/backoff via the shared helper."""
    return fetch_json(url, timeout=25, headers={"User-Agent": "HermesSportsPicks/1.0"})


def american_prob(odds: str | int | None) -> float | None:
    if odds is None:
        return None
    o = int(str(odds).replace("+", ""))
    return (-o) / ((-o) + 100) if o < 0 else 100 / (o + 100)


def devig(away_odds: str | int | None, home_odds: str | int | None) -> tuple[float | None, float | None]:
    away = american_prob(away_odds)
    home = american_prob(home_odds)
    if away is None or home is None:
        return None, None
    total = away + home
    return away / total, home / total


def score_value(score: Any) -> int | None:
    """ESPN scores appear as ints, strings, or {"value": 24.0} objects."""
    if isinstance(score, dict):
        score = score.get("value")
    if isinstance(score, bool) or score is None:
        return None
    try:
        return int(float(score))
    except (TypeError, ValueError):
        return None


def extract_moneylines(competition: dict[str, Any]) -> tuple[Any, Any]:
    """Moneylines from either ESPN odds shape (moneyline.close or *TeamOdds)."""
    odds = (competition.get("odds") or [{}])[0]
    moneyline = odds.get("moneyline") or {}
    away = ((moneyline.get("away") or {}).get("close") or {}).get("odds")
    home = ((moneyline.get("home") or {}).get("close") or {}).get("odds")
    if away is None:
        away = (odds.get("awayTeamOdds") or {}).get("moneyLine")
    if home is None:
        home = (odds.get("homeTeamOdds") or {}).get("moneyLine")
    return away, home


def parse_event_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


class NflSlateCollector:
    def __init__(self, season: int, week: int, seasontype: int = 2):
        self.season = season
        self.week = week
        self.seasontype = seasontype
        self._schedule_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}

    def team_schedule_events(self, team_id: str | int, season: int) -> list[dict[str, Any]]:
        key = (int(team_id), season)
        if key not in self._schedule_cache:
            data = get(f"{SITE_API}/teams/{team_id}/schedule?season={season}")
            self._schedule_cache[key] = data.get("events", [])
        return self._schedule_cache[key]

    def completed_games(self, team_id: str | int, season: int, before: dt.date | None) -> list[dict[str, Any]]:
        """Completed regular/post-season results before the slate date, oldest first."""
        games: list[tuple[dt.date, dict[str, Any]]] = []
        for event in self.team_schedule_events(team_id, season):
            competition = (event.get("competitions") or [{}])[0]
            status_type = ((competition.get("status") or {}).get("type") or {})
            if not status_type.get("completed"):
                continue
            game_date = parse_event_date(event.get("date"))
            if game_date is None:
                continue
            if before is not None and game_date >= before:
                continue
            games.append((game_date, competition))
        games.sort(key=lambda pair: pair[0])
        return [dict(competition, _game_date=game_date.isoformat()) for game_date, competition in games]

    def team_form(self, team_id: str | int, before: dt.date | None, limit: int = 5) -> dict[str, Any]:
        completed = self.completed_games(team_id, self.season, before)
        prior_games = 0
        # Weeks 1-2 have little/no current-season data; backfill from the prior
        # season so the row still carries a form read (flagged for discounting).
        if len(completed) < 3:
            prior = self.completed_games(team_id, self.season - 1, None)
            prior_games = min(limit - len(completed), len(prior))
            completed = prior[len(prior) - prior_games:] + completed
        completed = completed[-limit:]

        wins = losses = ties = pf = pa = 0
        last_game_date: str | None = None
        for competition in completed:
            competitors = {c.get("homeAway"): c for c in competition.get("competitors", [])}
            mine = opp = None
            for side in ("home", "away"):
                competitor = competitors.get(side)
                if competitor and str((competitor.get("team") or {}).get("id")) == str(team_id):
                    mine = competitor
                    opp = competitors.get("away" if side == "home" else "home")
            if mine is None or opp is None:
                continue
            my_score = score_value(mine.get("score"))
            opp_score = score_value(opp.get("score"))
            if my_score is None or opp_score is None:
                continue
            pf += my_score
            pa += opp_score
            if my_score > opp_score:
                wins += 1
            elif my_score < opp_score:
                losses += 1
            else:
                ties += 1
            last_game_date = competition.get("_game_date")
        return {
            "w": wins,
            "l": losses,
            "t": ties,
            "pf": pf,
            "pa": pa,
            "pd": pf - pa,
            "n": wins + losses + ties,
            "prior_season_games": prior_games,
            "last_game_date": last_game_date,
        }

    def rest_days(self, team_id: str | int, event_date: dt.date | None) -> int | None:
        """Days since the team's last completed CURRENT-season game."""
        if event_date is None:
            return None
        completed = self.completed_games(team_id, self.season, event_date)
        if not completed:
            return None
        last = parse_event_date(completed[-1].get("_game_date"))
        if last is None:
            return None
        return (event_date - last).days

    def injuries(self, espn_team_id: str | int) -> list[dict[str, Any]]:
        try:
            data = get(f"{CORE_API}/teams/{espn_team_id}/injuries?lang=en&region=us&limit=50")
            out = []
            for item in data.get("items", [])[:10]:
                ref = item.get("$ref")
                if not ref:
                    continue
                detail = get(ref.replace("http://", "https://"))
                name = "Unknown"
                athlete_ref = detail.get("athlete", {}).get("$ref")
                if athlete_ref:
                    try:
                        name = get(athlete_ref.replace("http://", "https://")).get("displayName", "Unknown")
                    except Exception:
                        pass
                out.append({"name": name, "status": detail.get("status"), "type": detail.get("type")})
            return out
        except Exception:
            return []

    def build_row(self, event: dict[str, Any]) -> dict[str, Any]:
        competition = event["competitions"][0]
        competitors = {c["homeAway"]: c for c in competition["competitors"]}
        away = competitors["away"]
        home = competitors["home"]
        away_id = away["team"]["id"]
        home_id = home["team"]["id"]
        event_date = parse_event_date(event.get("date"))

        away_ml, home_ml = extract_moneylines(competition)
        away_fair, home_fair = devig(away_ml, home_ml)
        odds = (competition.get("odds") or [{}])[0]
        venue = competition.get("venue") or {}
        away_rest = self.rest_days(away_id, event_date)
        home_rest = self.rest_days(home_id, event_date)

        def record(competitor: dict[str, Any]) -> str | None:
            records = competitor.get("records") or []
            return records[0].get("summary") if records else None

        return {
            "event_id": event["id"],
            "event": event["name"],
            "time": event["date"],
            "season": self.season,
            "week": self.week,
            "seasontype": self.seasontype,
            "away": away["team"]["displayName"],
            "home": home["team"]["displayName"],
            "away_abbr": away["team"].get("abbreviation"),
            "home_abbr": home["team"].get("abbreviation"),
            "away_record": record(away),
            "home_record": record(home),
            "away_ml": away_ml,
            "home_ml": home_ml,
            "away_fair": away_fair,
            "home_fair": home_fair,
            "spread_details": odds.get("details"),
            "over_under": odds.get("overUnder"),
            "away_form": self.team_form(away_id, event_date),
            "home_form": self.team_form(home_id, event_date),
            "away_rest_days": away_rest,
            "home_rest_days": home_rest,
            "away_short_week": away_rest is not None and away_rest < SHORT_WEEK_REST_DAYS,
            "home_short_week": home_rest is not None and home_rest < SHORT_WEEK_REST_DAYS,
            "away_injuries": self.injuries(away_id),
            "home_injuries": self.injuries(home_id),
            "venue": {"name": venue.get("fullName"), "indoor": venue.get("indoor")},
        }

    def collect(self) -> list[dict[str, Any]]:
        scoreboard = get(
            f"{SITE_API}/scoreboard?dates={self.season}&seasontype={self.seasontype}&week={self.week}&limit=100",
        )
        rows = []
        for event in scoreboard.get("events", []):
            # A single broken game must not kill the whole slate: emit a
            # partial row with an "error" field and keep scanning.
            try:
                rows.append(self.build_row(event))
            except Exception as exc:
                rows.append(
                    {
                        "event_id": event.get("id") if isinstance(event, dict) else None,
                        "event": event.get("name") if isinstance(event, dict) else None,
                        "time": event.get("date") if isinstance(event, dict) else None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=dt.date.today().year, help="NFL season year")
    parser.add_argument("--week", type=int, required=True, help="Week number (1-18 regular, 19-23 postseason)")
    parser.add_argument(
        "--seasontype", type=int, default=2, choices=(1, 2, 3), help="1=preseason, 2=regular, 3=postseason"
    )
    args = parser.parse_args()
    rows = NflSlateCollector(args.season, args.week, args.seasontype).collect()
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
