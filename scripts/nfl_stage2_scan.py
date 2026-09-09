#!/usr/bin/env python3
"""Collect NFL weekly slate context for proposed-card analysis.

Outputs JSON rows with ESPN event/odds, discounted early-season form, rest,
roster/injury/QB evidence, stadium weather, and explicit readiness blockers.
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
import math
from urllib.parse import urlencode
import sys
import time
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
# Conservative evidence weight, not a calibrated win-probability model.
PRIOR_SEASON_WEIGHT = 0.5


def get(url: str) -> Any:
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
        self._geocode_cache: dict[str, Any] = {}
        self._last_geocode = 0.0
        self._evidence_cache: dict[str, dict[str, Any]] = {}
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
            season_type = event.get("seasonType") or {}
            if season_type.get("type", 2) not in (2, 3):
                continue
            if (event.get("season") or {}).get("year", season) != season:
                continue
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
        return [dict(competition, _game_date=game_date.isoformat(), _season=season) for game_date, competition in games]

    def team_form(self, team_id: str | int, before: dt.date | None, limit: int = 5) -> dict[str, Any]:
        completed = self.completed_games(team_id, self.season, before)
        prior_games = 0
        # Early regular-season weeks may need a prior-season baseline; preserve
        # its origin and discount its contribution instead of relabelling it.
        if self.seasontype == 2 and 1 <= self.week <= 4 and len(completed) < 3:
            prior = self.completed_games(team_id, self.season - 1, before)
            prior_games = min(limit - len(completed), len(prior))
            completed = prior[len(prior) - prior_games:] + completed
        completed = completed[-limit:]

        wins = losses = ties = pf = pa = 0
        prior_games = 0
        weighted_pf = weighted_pa = effective_n = 0.0
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
            prior_game = competition.get("_season") == self.season - 1
            prior_games += int(prior_game)
            weight = PRIOR_SEASON_WEIGHT if prior_game else 1.0
            weighted_pf += my_score * weight
            weighted_pa += opp_score * weight
            effective_n += weight
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
            "current_season_games": wins + losses + ties - prior_games,
            "prior_season_weight": PRIOR_SEASON_WEIGHT if prior_games else None,
            "discounted": bool(prior_games),
            "form_label": ("Early season — prior-season baseline, discounted."
                           if prior_games else "Current-season form"),
            "weighted_pf": weighted_pf,
            "weighted_pa": weighted_pa,
            "weighted_pd": weighted_pf - weighted_pa,
            "effective_n": effective_n,
            # Use raw n here: dividing by effective_n would cancel discount
            # entirely in Week 1 when every game is from the prior season.
            "discounted_pd_per_game": ((weighted_pf - weighted_pa) / (wins + losses + ties)
                                       if wins + losses + ties else None),
            "confidence_cap": "Medium" if self.week == 1 else None,
            "offseason_adjustment_required": bool(prior_games),
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

    def evidence(self, url: str, key: str) -> dict[str, Any]:
        """Keep transport/schema failures distinct from a successful empty feed."""
        if url not in self._evidence_cache:
            result = {"source": url, "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            try:
                data = get(url)
                if not isinstance(data, dict) or key not in data:
                    raise ValueError(f"missing {key} in response")
                result.update(status="retrieved", data=data, source_timestamp=data.get("timestamp"))
            except Exception as exc:
                result.update(status="collector_failure", error=f"{type(exc).__name__}: {exc}")
            self._evidence_cache[url] = result
        return dict(self._evidence_cache[url])

    def roster_evidence(self, team_id: str | int) -> dict[str, Any]:
        result = self.evidence(f"{SITE_API}/teams/{team_id}/roster?season={self.season}", "athletes")
        data = result.pop("data", {})
        result["players"] = []
        if result["status"] != "retrieved":
            return result
        try:
            if (data.get("season") or {}).get("year") != self.season:
                raise ValueError("roster season mismatch or missing")
            if not isinstance(data["athletes"], list):
                raise ValueError("roster athletes must be a list")
            for group in data["athletes"]:
                for player in group["items"]:
                    if not player.get("id") or not player.get("displayName") or not (player.get("position") or {}).get("abbreviation"):
                        raise ValueError("roster athlete identity or position missing")
                    if not isinstance(player.get("injuries"), list) or any(not isinstance(i, dict) for i in player.get("injuries", [])):
                        raise ValueError("malformed roster injury rows")
                    result["players"].append({
                        "athlete_id": player.get("id"), "name": player.get("displayName"),
                        "position": (player.get("position") or {}).get("abbreviation"),
                        "team_id": str(team_id), "status": player.get("status"),
                        "injuries": player.get("injuries", []),
                    })
            if not result["players"]:
                result["status"] = "unavailable"
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            result.update(status="collector_failure", error=str(exc))
        return result

    def injury_evidence(self, team_id: str | int) -> dict[str, Any]:
        # Roster feed retains position and injury detail without truncating the
        # team report to ten players or silently losing failed athlete lookups.
        roster = self.roster_evidence(team_id)
        result = {k: v for k, v in roster.items() if k != "players"}
        result["items"] = []
        for player in roster["players"]:
            for injury in player["injuries"]:
                result["items"].append({
                    "name": player["name"], "athlete_id": player["athlete_id"],
                    "position": player["position"], "team_id": str(team_id),
                    "status": injury.get("status"), "type": injury.get("type"),
                    "detail": injury.get("details"), "description": injury.get("longComment"),
                    "source_timestamp": injury.get("date"), "source": roster["source"],
                    "retrieved_at": roster["retrieved_at"],
                })
        # A successful empty roster injury list is not an official clean bill
        # of health. Official practice/inactives review remains a hard gate.
        return result

    def injuries(self, espn_team_id: str | int) -> list[dict[str, Any]]:
        """Compatibility list; build_row also exposes collection status."""
        return self.injury_evidence(espn_team_id)["items"]

    def qb_evidence(self, team_id: str | int, event_id: str) -> dict[str, Any]:
        roster = self.roster_evidence(team_id)
        depth = self.evidence(f"{SITE_API}/teams/{team_id}/depthcharts?season={self.season}", "depthchart")
        data = depth.pop("data", {})
        depth["quarterbacks"] = []
        if depth["status"] == "retrieved":
            try:
                if (data.get("season") or {}).get("year") != self.season:
                    raise ValueError("depth-chart season mismatch or missing")
                if str((data.get("team") or {}).get("id")) != str(team_id):
                    raise ValueError("depth-chart team mismatch")
                players = {str(p["athlete_id"]): p for p in roster["players"]}
                for chart in data["depthchart"]:
                    for position in chart["positions"].values():
                        if position["position"].get("abbreviation") != "QB":
                            continue
                        for rank, athlete in enumerate(position["athletes"], 1):
                            matched = players.get(str(athlete.get("id")))
                            depth["quarterbacks"].append({
                                "athlete_id": athlete.get("id"), "name": athlete.get("displayName"),
                                "depth_rank": rank, "roster_match": bool(matched and matched["position"] == "QB"),
                                "roster_status": matched.get("status") if matched else None,
                                "injuries": matched.get("injuries") if matched else None,
                            })
                if not depth["quarterbacks"]:
                    depth["status"] = "unavailable"
            except (TypeError, KeyError, ValueError, AttributeError) as exc:
                depth.update(status="collector_failure", error=str(exc))
        summary = self.evidence(f"{SITE_API}/summary?event={event_id}", "header")
        summary_data = summary.pop("data", {})
        if summary["status"] == "retrieved" and str(summary_data["header"].get("id")) != str(event_id):
            summary.update(status="collector_failure", error="summary event mismatch")
        # ESPN's pregame summary does not supply official starting-QB
        # confirmation. Never promote depth rank or roster Active to confirmed.
        confirmation = {**summary, "status": ("unavailable" if summary["status"] == "retrieved"
                                              else summary["status"]),
                        "confirmed": False, "event_id": str(event_id), "team_id": str(team_id),
                        "reason": "official_game_day_qb_confirmation_not_supplied"}
        return {"roster": roster, "depth_chart": depth, "confirmation": confirmation,
                "official_inactives": {"status": "not_retrieved", "reason": "separate_official_report_required"}}

    def weather_evidence(self, venue: dict[str, Any], kickoff: str) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "not_retrieved", "venue": venue,
                                  "kickoff": kickoff, "forecast_issued_at": None}
        if venue.get("indoor") is True:
            return {**result, "status": "not_applicable", "reason": "venue_marked_indoor"}
        if venue.get("indoor") is not False:
            return {**result, "status": "unavailable", "reason": "indoor_outdoor_unknown"}
        try:
            start = dt.datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if start.tzinfo is None:
                raise ValueError("kickoff requires timezone")
            start = start.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
            end = start + dt.timedelta(hours=4)
            address = venue.get("address") or {}
            name, city = venue.get("fullName"), address.get("city")
            if not name or not city:
                return {**result, "status": "unavailable", "reason": "venue_identity_missing"}
            geo_url = "https://nominatim.openstreetmap.org/search?" + urlencode({
                "q": ", ".join(filter(None, [name, city, address.get("state"), address.get("country")])),
                "format": "jsonv2", "addressdetails": 1, "limit": 5})
            # Exact venue and city matching prevents a generic city forecast
            # from silently being labelled stadium weather.
            if geo_url not in self._geocode_cache:
                time.sleep(max(0, 1.1 - (time.monotonic() - self._last_geocode)))
                try:
                    self._geocode_cache[geo_url] = get(geo_url)
                finally:
                    self._last_geocode = time.monotonic()
            matches = self._geocode_cache[geo_url]
            if not isinstance(matches, list):
                raise ValueError("invalid geocoder response")
            matches = [m for m in matches if m.get("name", "").casefold() == name.casefold()
                       and city.casefold() in [str(v).casefold() for k, v in (m.get("address") or {}).items()
                                             if k in ("city", "town", "village", "municipality")]]
            if len(matches) != 1:
                return {**result, "status": "unavailable", "reason": "venue_coordinates_missing_or_ambiguous",
                        "coordinate_source": geo_url}
            lat, lon = float(matches[0]["lat"]), float(matches[0]["lon"])
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("invalid stadium coordinates")
            result.update(latitude=lat, longitude=lon, coordinate_source=geo_url,
                          coordinate_attribution="© OpenStreetMap contributors, ODbL 1.0",
                          coordinate_license="https://www.openstreetmap.org/copyright")
            fields = ["temperature_2m", "wind_speed_10m", "wind_gusts_10m", "precipitation", "snowfall"]
            url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
                "latitude": lat, "longitude": lon, "hourly": ",".join(fields), "timezone": "GMT",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
                "start_hour": start.strftime("%Y-%m-%dT%H:%M"), "end_hour": end.strftime("%Y-%m-%dT%H:%M")})
            result.update(source=url, retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat())
            forecast = get(url)
            if forecast.get("utc_offset_seconds") != 0:
                raise ValueError("forecast must use UTC")
            hourly = forecast["hourly"]
            units = forecast["hourly_units"]
            if any(units.get(k) != v for k, v in {"temperature_2m": "°F", "wind_speed_10m": "mp/h",
                                                  "wind_gusts_10m": "mp/h", "precipitation": "inch",
                                                  "snowfall": "inch"}.items()):
                raise ValueError("unexpected forecast units")
            expected = [(start + dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(5)]
            if hourly["time"] != expected:
                return {**result, "status": "unavailable", "reason": "kickoff_window_incomplete"}
            samples = []
            for i, hour in enumerate(expected):
                sample = {"time_utc": hour + "Z"}
                for key in fields:
                    value = hourly[key][i]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError(f"invalid forecast {key}")
                    if key != "temperature_2m" and value < 0:
                        raise ValueError(f"negative forecast {key}")
                    sample[key] = value
                samples.append(sample)
            return {**result, "status": "retrieved", "units": units, "samples": samples,
                    "weather_thesis_review_required": True,
                    "wind_at_least_15_mph": any(s["wind_speed_10m"] >= 15 for s in samples)}
        except Exception as exc:
            return {**result, "status": "collector_failure", "error": f"{type(exc).__name__}: {exc}"}

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

        away_injuries = self.injury_evidence(away_id)
        home_injuries = self.injury_evidence(home_id)
        row = {
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
            "away_injuries": away_injuries["items"],
            "away_injury_evidence": away_injuries,
            "away_qb": self.qb_evidence(away_id, event["id"]),
            "home_injuries": home_injuries["items"],
            "home_injury_evidence": home_injuries,
            "home_qb": self.qb_evidence(home_id, event["id"]),
            "venue": {"name": venue.get("fullName"), "indoor": venue.get("indoor"),
                      "id": venue.get("id"), "address": venue.get("address")},
            "weather": self.weather_evidence(venue, event["date"]),
            "exchange": {"status": "not_retrieved", "reason": "sportsbook_only_collector",
                         "ask": None, "net_edge": None},
        }

        row["blockers"] = readiness_blockers(row)
        row["assessment"] = "PASS"
        row["candidates"] = []
        row["official_pick_allowed"] = False
        return row

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
                        "assessment": "PASS", "candidates": [], "official_pick_allowed": False,
                        "blockers": [{"component": "game", "category": "collector_failure",
                                      "reason": "row_collection_failed"}],
                    }
                )
        return rows


def readiness_blockers(row: dict[str, Any]) -> list[dict[str, str]]:
    """Collection diagnostics, not an implementation of the full handicap gate."""
    blockers = []

    def check(component: str, evidence: dict[str, Any]) -> None:
        status = evidence.get("status", "not_retrieved")
        if status in ("retrieved", "not_applicable"):
            return
        category = {"unavailable": "upstream_missing", "collector_failure": "collector_failure"}.get(status, "hard_gate")
        blockers.append({"component": component, "category": category,
                         "reason": evidence.get("reason", status)})

    for side in ("away", "home"):
        check(f"{side}_injuries", row.get(f"{side}_injury_evidence", {}))
        qb = row.get(f"{side}_qb", {})
        for part in ("roster", "depth_chart", "confirmation", "official_inactives"):
            check(f"{side}_qb_{part}", qb.get(part, {}))
        form = row.get(f"{side}_form", {})
        if not form.get("n"):
            blockers.append({"component": f"{side}_form", "category": "upstream_missing", "reason": "no_completed_form"})
        if form.get("prior_season_games"):
            blockers.append({"component": f"{side}_form", "category": "hard_gate", "reason": "offseason_changes_require_review"})
    check("weather", row.get("weather", {}))
    check("exchange", row.get("exchange", {}))
    if row.get("away_fair") is None or row.get("home_fair") is None:
        blockers.append({"component": "sportsbook", "category": "upstream_missing", "reason": "two_sided_price_missing"})
    blockers.append({"component": "analysis", "category": "hard_gate",
                     "reason": "full_nfl_handicap_and_lock_gate_required"})
    return blockers


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
