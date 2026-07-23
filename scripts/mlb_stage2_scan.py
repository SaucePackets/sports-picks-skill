#!/usr/bin/env python3
"""Collect MLB Stage 2 slate context for proposed-card analysis.

Outputs JSON rows with ESPN event/odds, MLB Stats probable starters,
last-7 team form, last-7 bullpen aggregates, and best-effort ESPN injuries.
No betting orders. No prediction-market calls.

Hardening:
- HTTP via the shared retry helper (exponential backoff on 429/5xx/network).
- One game's failure emits a partial row with an "error" field instead of
  killing the whole slate.
- Final-game boxscores are cached to ~/.cache/hermes/mlb-boxscores/ —
  immutable once a game is Final.

Usage:
  python scripts/mlb_stage2_scan.py --date 2026-05-17
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

from http_util import fetch_json  # noqa: E402

ALIASES = {"WSH": "WSN", "CHW": "CWS", "AZ": "ARI"}
BOXSCORE_CACHE_DIR = Path.home() / ".cache" / "hermes" / "mlb-boxscores"


def get(url: str) -> dict[str, Any]:
    """Fetch JSON with retries/backoff via the shared helper."""
    return fetch_json(url, timeout=25, headers={"User-Agent": "HermesSportsPicks/1.0"})


def cached_final_boxscore(game_pk: Any) -> dict[str, Any]:
    """Boxscore for a Final game; cached on disk because it never changes."""
    cache_path = BOXSCORE_CACHE_DIR / f"{game_pk}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass  # corrupt/partial cache entry: refetch and rewrite below
    box = get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore")
    try:
        BOXSCORE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(f".tmp{os.getpid()}")
        tmp_path.write_text(json.dumps(box, sort_keys=True), encoding="utf-8")
        tmp_path.replace(cache_path)
    except OSError:
        pass  # cache is best-effort; never fail the scan over it
    return box


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


def outs_from_ip(ip: str | None) -> int:
    if not ip:
        return 0
    s = str(ip)
    if "." in s:
        whole, frac = s.split(".", 1)
        return int(whole) * 3 + int(frac[:1])
    return int(float(s)) * 3


class MlbSlateCollector:
    def __init__(self, date: str, season: int):
        self.date = date
        self.season = season
        self._team_games_cache: dict[int, list[dict[str, Any]]] = {}

    def recent_completed(self, team_id: int, days: int = 18, limit: int = 7) -> list[dict[str, Any]]:
        if team_id in self._team_games_cache:
            return self._team_games_cache[team_id]
        end = dt.date.fromisoformat(self.date)
        start = (end - dt.timedelta(days=days)).isoformat()
        stop = (end - dt.timedelta(days=1)).isoformat()
        url = (
            "https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&teamId={team_id}&startDate={start}&endDate={stop}"
            "&hydrate=linescore,team"
        )
        data = get(url)
        games: list[dict[str, Any]] = []
        for day in data.get("dates", []):
            for game in day.get("games", []):
                if game.get("status", {}).get("detailedState") != "Final":
                    continue
                if game.get("officialDate") >= self.date:
                    continue
                games.append(game)
        games = games[-limit:]
        self._team_games_cache[team_id] = games
        return games

    def team_form(self, team_id: int) -> dict[str, int]:
        wins = losses = runs_for = runs_against = 0
        games = self.recent_completed(team_id)
        for game in games:
            side = "home" if game["teams"]["home"]["team"]["id"] == team_id else "away"
            opp = "away" if side == "home" else "home"
            team_runs = game["teams"][side].get("score", 0)
            opp_runs = game["teams"][opp].get("score", 0)
            runs_for += team_runs
            runs_against += opp_runs
            if team_runs > opp_runs:
                wins += 1
            else:
                losses += 1
        return {"w": wins, "l": losses, "rf": runs_for, "ra": runs_against, "rd": runs_for - runs_against, "n": len(games)}

    def pitcher_stats(self, player_id: int | None) -> dict[str, Any] | None:
        if not player_id:
            return None
        url = (
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats"
            f"?stats=season&group=pitching&season={self.season}"
        )
        data = get(url)
        stats = data.get("stats") or []
        if not stats:
            return None
        splits = stats[0].get("splits") or []
        if not splits:
            return None
        stat = splits[0].get("stat", {})

        def as_float(key: str) -> float | None:
            try:
                return float(stat.get(key))
            except (TypeError, ValueError):
                return None

        return {
            "era": as_float("era"),
            "whip": as_float("whip"),
            "ip": stat.get("inningsPitched"),
            "k": int(stat.get("strikeOuts", 0) or 0),
            "bb": int(stat.get("baseOnBalls", 0) or 0),
            "hr": int(stat.get("homeRuns", 0) or 0),
            "starts": int(stat.get("gamesStarted", 0) or 0),
            "record": f"{stat.get('wins', 0)}-{stat.get('losses', 0)}",
        }

    def bullpen(self, team_id: int) -> dict[str, Any]:
        outs = earned_runs = hits = walks = strikeouts = homers = 0
        for game in self.recent_completed(team_id):
            # recent_completed only returns Final games, so the boxscore is
            # immutable and safe to cache.
            box = cached_final_boxscore(game["gamePk"])
            side = "home" if box["teams"]["home"]["team"]["id"] == team_id else "away"
            for player in box["teams"][side].get("players", {}).values():
                pitching = player.get("stats", {}).get("pitching")
                if not pitching:
                    continue
                if int(pitching.get("gamesStarted", 0) or 0) > 0:
                    continue
                player_outs = outs_from_ip(pitching.get("inningsPitched"))
                if player_outs <= 0:
                    continue
                outs += player_outs
                earned_runs += int(pitching.get("earnedRuns", 0) or 0)
                hits += int(pitching.get("hits", 0) or 0)
                walks += int(pitching.get("baseOnBalls", 0) or 0)
                strikeouts += int(pitching.get("strikeOuts", 0) or 0)
                homers += int(pitching.get("homeRuns", 0) or 0)
        ip = outs / 3 if outs else 0
        return {
            "ip": ip,
            "era": (earned_runs * 9 / ip if ip else None),
            "whip": ((hits + walks) / ip if ip else None),
            "k": strikeouts,
            "bb": walks,
            "hr": homers,
        }

    def injuries(self, espn_team_id: str) -> list[dict[str, Any]]:
        try:
            url = (
                "https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/teams/"
                f"{espn_team_id}/injuries?lang=en&region=us&limit=50"
            )
            data = get(url)
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

    def build_row(
        self,
        event: dict[str, Any],
        stats_by_pair: dict[tuple[str | None, str | None], dict[str, Any]],
    ) -> dict[str, Any] | None:
        competition = event["competitions"][0]
        competitors = {c["homeAway"]: c for c in competition["competitors"]}
        away = competitors["away"]
        home = competitors["home"]
        away_abbr = away["team"]["abbreviation"]
        home_abbr = home["team"]["abbreviation"]
        stats_game = stats_by_pair.get((ALIASES.get(away_abbr, away_abbr), ALIASES.get(home_abbr, home_abbr)))
        if not stats_game:
            return None

        moneyline = ((competition.get("odds") or [{}])[0].get("moneyline") or {})
        away_ml = moneyline.get("away", {}).get("close", {}).get("odds")
        home_ml = moneyline.get("home", {}).get("close", {}).get("odds")
        away_fair, home_fair = devig(away_ml, home_ml)
        away_team = stats_game["teams"]["away"]
        home_team = stats_game["teams"]["home"]
        away_id = away_team["team"]["id"]
        home_id = home_team["team"]["id"]
        away_sp = away_team.get("probablePitcher", {})
        home_sp = home_team.get("probablePitcher", {})

        return {
            "event_id": event["id"],
            "event": event["name"],
            "time": event["date"],
            "away": away["team"]["displayName"],
            "home": home["team"]["displayName"],
            "away_abbr": away_abbr,
            "home_abbr": home_abbr,
            "away_ml": away_ml,
            "home_ml": home_ml,
            "away_fair": away_fair,
            "home_fair": home_fair,
            "away_form": self.team_form(away_id),
            "home_form": self.team_form(home_id),
            "away_bullpen": self.bullpen(away_id),
            "home_bullpen": self.bullpen(home_id),
            "away_starter": away_sp.get("fullName"),
            "home_starter": home_sp.get("fullName"),
            "away_starter_stats": self.pitcher_stats(away_sp.get("id")),
            "home_starter_stats": self.pitcher_stats(home_sp.get("id")),
            "away_injuries": self.injuries(away["team"]["id"]),
            "home_injuries": self.injuries(home["team"]["id"]),
        }

    def collect(self) -> list[dict[str, Any]]:
        date_compact = self.date.replace("-", "")
        espn = get(
            f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={date_compact}&limit=100",
        )
        schedule = get(
            f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={self.date}&hydrate=probablePitcher,team,venue,linescore",
        )
        stats_by_pair: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        for day in schedule.get("dates", []):
            for game in day.get("games", []):
                away = game["teams"]["away"]["team"].get("abbreviation")
                home = game["teams"]["home"]["team"].get("abbreviation")
                stats_by_pair[(away, home)] = game
                stats_by_pair[(ALIASES.get(away, away), ALIASES.get(home, home))] = game

        rows = []
        for event in espn.get("events", []):
            # A single broken game must not kill the whole slate: emit a
            # partial row with an "error" field and keep scanning.
            try:
                row = self.build_row(event, stats_by_pair)
            except Exception as exc:
                rows.append(
                    {
                        "event_id": event.get("id") if isinstance(event, dict) else None,
                        "event": event.get("name") if isinstance(event, dict) else None,
                        "time": event.get("date") if isinstance(event, dict) else None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if row is not None:
                rows.append(row)
        return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="Slate date YYYY-MM-DD")
    parser.add_argument("--season", type=int, default=None, help="MLB season year; defaults to date year")
    args = parser.parse_args()
    season = args.season or int(args.date[:4])
    rows = MlbSlateCollector(args.date, season).collect()
    print(json.dumps(rows, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
