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
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from http_util import fetch_json  # noqa: E402
from mlb_game_reads import conventional_denominator_path  # noqa: E402

from mlb_data_completeness import (  # noqa: E402
    canonical_team, canonical_offense, lineup_from_feed, assess, coverage, utc_now,
)
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



# Approximate 3-year run park factors (100 = neutral). Handicap CONTEXT, not
# precision: the slate flags extreme parks (>=105 hitter / <=96 pitcher) so the
# Coors-class adjustment rule fires on data instead of memory.
PARK_RUN_FACTORS = {
    "Coors Field": 112,
    "Fenway Park": 106,
    "Great American Ball Park": 107,
    "Sutter Health Park": 104,
    "Yankee Stadium": 103,
    "Citizens Bank Park": 103,
    "Chase Field": 102,
    "Wrigley Field": 102,
    "Rate Field": 102,
    "Guaranteed Rate Field": 102,
    "Truist Park": 101,
    "Kauffman Stadium": 101,
    "Rogers Centre": 101,
    "Nationals Park": 100,
    "Camden Yards": 100,
    "Oriole Park at Camden Yards": 100,
    "American Family Field": 99,
    "Globe Life Field": 99,
    "Angel Stadium": 99,
    "Comerica Park": 99,
    "Target Field": 99,
    "Daikin Park": 99,
    "Minute Maid Park": 99,
    "Busch Stadium": 98,
    "Progressive Field": 98,
    "Dodger Stadium": 98,
    "Citi Field": 97,
    "PNC Park": 97,
    "loanDepot park": 97,
    "George M. Steinbrenner Field": 103,
    "Petco Park": 96,
    "Oracle Park": 96,
    "T-Mobile Park": 95,
}



SAVANT_CACHE = Path.home() / ".cache" / "hermes" / "savant-team-xwoba"


def team_offense_quality(season: int) -> dict[str, dict[str, float]]:
    """Team wOBA/xwOBA from Baseball Savant, cached per day; failures propagate."""
    import csv as _csv
    import io as _io
    from datetime import date as _date

    try:
        SAVANT_CACHE.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    cache = SAVANT_CACHE / f"{_date.today().isoformat()}-{season}.json"
    if cache.exists():
        try:
            return canonical_offense(json.loads(cache.read_text()))
        except (OSError, ValueError):
            pass
    url = (
        "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
        f"?type=batter-team&year={season}&position=&team=&csv=true"
    )
    import urllib.request
    import urllib.error
    import time
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8-sig")
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code != 429 and exc.code < 500:
                raise
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    out: dict[str, dict[str, float]] = {}
    for row in _csv.DictReader(_io.StringIO(text)):
        abbr = str(row.get("team_id") or "").strip().upper()
        if not abbr:
            continue
        try:
            out[abbr] = {
                "woba": float(row["woba"]),
                "xwoba": float(row["est_woba"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    out = canonical_offense(out)
    if not out:
        raise ValueError("Savant returned no usable team offense rows")
    try:
        cache.write_text(json.dumps(out))
    except OSError:
        pass
    return out



def park_context(venue_name: str | None) -> dict[str, Any]:
    """Park run environment for a game, with the data outage named explicitly.

    ``PARK_RUN_FACTORS`` is a fixed table, so any venue off it — a neutral-site
    game, a new ballpark, a renamed one — yields ``run_factor: None``. That is a
    DATA OUTAGE, and it is reported as one (``data_status: "unavailable"``)
    rather than as a silent null the handicapper has to interpret, because a
    silent null reads as "no park effect" and a missing input reads as a stop.
    It is neither: the run environment is unknown, which is a reason to charge
    the ``unknown_park_environment`` uncertainty haircut and take no
    ``park_home_context`` adjustment. Never substitute a neutral 100 here — a
    fabricated factor would let a park read be claimed on no data at all.
    """
    key = str(venue_name or "").strip()
    factor = PARK_RUN_FACTORS.get(key)
    flag = None
    if factor is None:
        flag = (
            f"park run factor unavailable for {key or '<unknown venue>'} — "
            "no park_home_context adjustment; charge the unknown_park_environment "
            "haircut instead of discarding the game"
        )
    elif factor >= 105:
        flag = "extreme hitter park — cap confidence per park rule"
    elif factor <= 96:
        flag = "strong pitcher park"
    return {
        "venue": venue_name,
        "run_factor": factor,
        "data_status": "unavailable" if factor is None else "available",
        "flag": flag,
    }


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


def start_time_gap(left: Any, right: Any) -> float:
    """Seconds between two ISO timestamps, or ``inf`` if either is unreadable.

    ``inf`` rather than ``0`` on purpose. This is the tiebreak that separates
    the two games of a doubleheader, and a timestamp nobody could parse must
    never win that tiebreak by looking like a perfect match.
    """
    parsed = []
    for value in (left, right):
        if not isinstance(value, str):
            return float("inf")
        try:
            parsed.append(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return float("inf")
    if any(t.utcoffset() is None for t in parsed):
        return float("inf")
    delta = parsed[0] - parsed[1]
    return abs(delta.total_seconds())


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

        k = int(stat.get("strikeOuts", 0) or 0)
        bb = int(stat.get("baseOnBalls", 0) or 0)
        hr = int(stat.get("homeRuns", 0) or 0)
        hbp = int(stat.get("hitByPitch", 0) or 0)
        bf = int(stat.get("battersFaced", 0) or 0)
        ip_outs = outs_from_ip(stat.get("inningsPitched"))
        # FIP strips defense/sequencing luck out of ERA; the 3.15 constant is a
        # league-typical anchor, close enough for cross-pitcher comparison.
        fip = None
        if ip_outs >= 3:
            innings = ip_outs / 3
            fip = round((13 * hr + 3 * (bb + hbp) - 2 * k) / innings + 3.15, 2)
        k_bb_pct = round((k - bb) / bf * 100, 1) if bf > 0 else None
        return {
            "era": as_float("era"),
            "whip": as_float("whip"),
            "fip": fip,
            "k_bb_pct": k_bb_pct,
            "ip": stat.get("inningsPitched"),
            "k": k,
            "bb": bb,
            "hr": hr,
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
        url = (
            "https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/teams/"
            f"{espn_team_id}/injuries?lang=en&region=us&limit=1000"
        )
        data = get(url)
        if not isinstance(data.get("items"), list):
            raise ValueError("injury source missing items list")
        if data.get("pageCount", 1) != 1 or data.get("count", len(data["items"])) > len(data["items"]):
            raise ValueError("injury source is truncated")
        out = []
        for item in data["items"]:
            detail = get(item["$ref"].replace("http://", "https://"))
            athlete = get(detail["athlete"]["$ref"].replace("http://", "https://"))
            if not athlete.get("displayName") or not detail.get("status"):
                raise ValueError("incomplete injury detail")
            out.append({"name": athlete["displayName"], "status": detail["status"], "type": detail.get("type")})
        return out

    def match_stats_game(
        self,
        event: dict[str, Any],
        stats_by_pair: dict[tuple[str | None, str | None], list[dict[str, Any]]],
        used: set[Any],
    ) -> dict[str, Any] | None:
        """The StatsAPI game this ESPN event is, or None.

        The team pair alone does NOT identify a game: a doubleheader plays the
        same pair twice on one date. Keying a dict on the pair kept only the
        last game, so both ESPN events resolved to the same StatsAPI record and
        one game of every doubleheader was handicapped with the OTHER game's
        probable starters, form and venue. Measured on the 2026 corpus before
        this was written: 6 of 52 dates with a stage2 output, 12 rows, and in
        every one of them the two rows carry identical starters.

        So the pair narrows and the first pitch decides, and a game already
        claimed by an earlier event is never handed out twice.
        """
        competition = event["competitions"][0]
        competitors = {c["homeAway"]: c for c in competition["competitors"]}
        away_abbr = competitors["away"]["team"]["abbreviation"]
        home_abbr = competitors["home"]["team"]["abbreviation"]

        key = (canonical_team(away_abbr), canonical_team(home_abbr))
        candidates = [g for g in stats_by_pair.get(key, []) if g.get("gamePk") not in used]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        gaps = sorted((start_time_gap(g.get("gameDate"), event.get("date")), i) for i, g in enumerate(candidates))
        if gaps[0][0] == float("inf") or gaps[0][0] == gaps[1][0]:
            return None
        return candidates[gaps[0][1]]

    def build_row(
        self,
        event: dict[str, Any],
        stats_game: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not stats_game:
            return None
        competition = event["competitions"][0]
        competitors = {c["homeAway"]: c for c in competition["competitors"]}
        away = competitors["away"]
        home = competitors["home"]
        away_abbr = away["team"]["abbreviation"]
        home_abbr = home["team"]["abbreviation"]

        moneyline = ((competition.get("odds") or [{}])[0].get("moneyline") or {})
        away_ml = moneyline.get("away", {}).get("close", {}).get("odds")
        home_ml = moneyline.get("home", {}).get("close", {}).get("odds")
        failures = list(getattr(self, "source_failures", []))
        def acquire(field, operation):
            try:
                return operation()
            except Exception as exc:
                failures.append({"field": field, "error": f"{type(exc).__name__}: {exc}"})
                return None
        fair = acquire("prices", lambda: devig(away_ml, home_ml))
        away_fair, home_fair = fair or (None, None)
        away_team = stats_game["teams"]["away"]
        home_team = stats_game["teams"]["home"]
        away_id = away_team["team"]["id"]
        home_id = home_team["team"]["id"]
        away_sp = away_team.get("probablePitcher", {})
        home_sp = home_team.get("probablePitcher", {})

        row = {
            # BOTH id spaces, always. ``event_id`` is ESPN's and ``game_pk`` is
            # MLB's, and they are not interchangeable — 2026-08-30 carries
            # event 401816733 for the game MLB calls 824876. They are joined
            # right here and only one used to be emitted, which is why anything
            # downstream that tried to join on an id got silence.
            "game_pk": stats_game.get("gamePk"),
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
            "away_form": acquire("away_form", lambda: self.team_form(away_id)),
            "home_form": acquire("home_form", lambda: self.team_form(home_id)),
            "away_bullpen": acquire("away_bullpen", lambda: self.bullpen(away_id)),
            "home_bullpen": acquire("home_bullpen", lambda: self.bullpen(home_id)),
            "away_starter": away_sp.get("fullName"),
            "home_starter": home_sp.get("fullName"),
            "away_starter_stats": acquire("away_starter_stats", lambda: self.pitcher_stats(away_sp.get("id"))),
            "home_starter_stats": acquire("home_starter_stats", lambda: self.pitcher_stats(home_sp.get("id"))),
            "away_injuries": acquire("away_injuries", lambda: self.injuries(away["team"]["id"])),
            "home_injuries": acquire("home_injuries", lambda: self.injuries(home["team"]["id"])),
            "park": park_context((stats_game.get("venue") or {}).get("name")),
            "away_offense": acquire("away_offense", lambda: self.offense_quality.get(canonical_team(away_abbr))),
            "home_offense": acquire("home_offense", lambda: self.offense_quality.get(canonical_team(home_abbr))),
            "away_team_id": away_id,
            "home_team_id": home_id,
            "source_failures": failures,
        }
        feed = acquire("lineups", lambda: get(f"https://statsapi.mlb.com/api/v1.1/game/{stats_game['gamePk']}/feed/live"))
        retrieved = utc_now().isoformat()
        row["retrieved_at_utc"] = retrieved
        for side in ("away", "home"):
            row[f"{side}_lineup"] = acquire(f"{side}_lineup", lambda: lineup_from_feed(feed or {}, stats_game, side, retrieved))
        row["data_completeness"] = assess(row)
        return row

    def collect(self) -> list[dict[str, Any]]:
        self.source_failures = []
        try:
            self.offense_quality = canonical_offense(team_offense_quality(self.season))
            if not self.offense_quality:
                raise ValueError("no usable offense rows")
        except Exception as exc:
            self.offense_quality = {}
            self.source_failures.append({"field": "offense", "error": f"{type(exc).__name__}: {exc}"})
        date_compact = self.date.replace("-", "")
        def source(name, url, field):
            try:
                value = get(url)
                if not isinstance(value.get(field), list):
                    raise ValueError(f"missing {field} list")
                if name == "schedule":
                    games = [g for d in value["dates"] for g in d["games"]]
                    if "totalGames" in value and value["totalGames"] != len(games):
                        raise ValueError("schedule totalGames differs from returned games")
                    if any(not isinstance(g, dict) or type(g.get("gamePk")) is not int for g in games):
                        raise ValueError("schedule has invalid game identities")
                return value
            except Exception as exc:
                self.source_failures.append({"field": name, "source_url": url, "error": f"{type(exc).__name__}: {exc}"})
                return None
        espn = source("scoreboard", f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={date_compact}&limit=100", "events")
        schedule = source("schedule", f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={self.date}&hydrate=probablePitcher,team,venue,linescore", "dates")
        self.schedule_verified = schedule is not None
        espn = espn or {"events": []}
        schedule = schedule or {"dates": []}
        stats_by_pair: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
        scheduled: list[dict[str, Any]] = []
        for day in schedule.get("dates", []):
            for game in day.get("games", []):
                scheduled.append(game)
                try:
                    away = game["teams"]["away"]["team"].get("abbreviation")
                    home = game["teams"]["home"]["team"].get("abbreviation")
                    key = (canonical_team(away), canonical_team(home))
                except (ValueError, KeyError, TypeError):
                    continue  # retained below as an unmatched scheduled game
                stats_by_pair.setdefault(key, []).append(game)
        for games in stats_by_pair.values():
            games.sort(key=lambda game: str(game.get("gameDate") or ""))

        self.scheduled_games = len(scheduled) if self.schedule_verified else None
        rows = []
        used: set[Any] = set()
        for event in espn.get("events", []):
            # A single broken game must not kill the whole slate: emit a
            # partial row with an "error" field and keep scanning.
            stats_game = None
            try:
                stats_game = self.match_stats_game(event, stats_by_pair, used)
                if stats_game is not None:
                    used.add(stats_game.get("gamePk"))
                row = self.build_row(event, stats_game)
            except Exception as exc:
                rows.append(
                    {
                        "event_id": event.get("id") if isinstance(event, dict) else None,
                        "event": event.get("name") if isinstance(event, dict) else None,
                        "time": event.get("date") if isinstance(event, dict) else None,
                        "game_pk": stats_game.get("gamePk") if stats_game else None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if row is None:
                # An event we could not resolve to a StatsAPI game used to be
                # DROPPED, silently, and a dropped row is a game that never
                # existed as far as any later count is concerned. The slate's
                # denominator has to be able to say "this game was scanned and
                # could not be identified"; it must never say nothing.
                rows.append(
                    {
                        "game_pk": None,
                        "event_id": event.get("id") if isinstance(event, dict) else None,
                        "event": event.get("name") if isinstance(event, dict) else None,
                        "time": event.get("date") if isinstance(event, dict) else None,
                        "error": "unmatched: no MLB StatsAPI game for this ESPN event",
                    }
                )
                continue
            used.add(row.get("game_pk"))
            rows.append(row)

        # The other direction, and it is a different failure: ESPN is the
        # enumerator, so a game on MLB's schedule that ESPN never listed would
        # not appear at all. Neither direction has fired on the 2026 corpus —
        # 53 dates, stage2 row count equal to StatsAPI totalGames on every one
        # — but a denominator that can shrink without saying so is not a
        # denominator, and this is the whole point of recording one.
        for game in scheduled:
            if game.get("gamePk") in used:
                continue
            teams = game.get("teams") or {}
            rows.append(
                {
                    "game_pk": game.get("gamePk"),
                    "event_id": None,
                    "event": " at ".join(
                        str(((teams.get(side) or {}).get("team") or {}).get("name"))
                        for side in ("away", "home")
                    ),
                    "time": game.get("gameDate"),
                    "error": "unmatched: no ESPN scoreboard event for this MLB game",
                }
            )
        for row in rows:
            if "error" in row:
                row["source_failures"] = list(self.source_failures) + [{"field": "game_collection", "error": row["error"]}]
                row["data_completeness"] = assess(row)
        self.coverage = coverage(rows, scheduled_games=self.scheduled_games, schedule_verified=self.schedule_verified)
        self.coverage["source_failures"] = self.source_failures
        return rows


def denominator_output_path(date: str, root: Path | None = None) -> Path:
    """Canonical location for this scan's roster, as the validator expects it.

    The path is derived from ``mlb_game_reads.conventional_denominator_path``
    against the schedule that day's run will write, so the two cannot drift
    into looking for each other in different directories.
    """
    base = (root or resolve_scan_root()).resolve()
    schedule_path = base / ".picks" / "execute" / f"{date}-schedule.json"
    resolved = conventional_denominator_path(schedule_path, {"date": date})
    # conventional_denominator_path returns None only without a usable date,
    # and the date is required here, so this is a contract violation not a
    # runtime condition.
    assert resolved is not None
    return resolved


def resolve_scan_root(cwd: Path | None = None, home: Path | None = None) -> Path:
    override = os.environ.get("SPORTS_PICKS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = (cwd or Path.cwd()).expanduser().resolve()
    if (current / ".picks").is_dir():
        return current
    default = ((home or Path.home()) / "projects" / "sports-picks-skill").resolve()
    if (default / ".picks").is_dir():
        return default
    return current


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="Slate date YYYY-MM-DD")
    parser.add_argument("--season", type=int, default=None, help="MLB season year; defaults to date year")
    args = parser.parse_args()
    try:
        day = dt.date.fromisoformat(args.date)
        if day.isoformat() != args.date:
            raise ValueError("use YYYY-MM-DD")
    except ValueError as exc:
        parser.error(str(exc))
    season = args.season or day.year
    collector = MlbSlateCollector(args.date, season)
    rows = collector.collect()
    payload = json.dumps(rows, indent=2, sort_keys=True)
    # Persist unconditionally. Printing to stdout left the denominator existing
    # only inside whatever shell the run happened to use, so the cross-check
    # that is supposed to stop a short roster had no file to check against —
    # the newest scan artifact on the runtime was three weeks old while the
    # slate ran daily. Writing it is what makes the check reachable at all.
    destination = denominator_output_path(args.date)
    write_failed = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload + "\n", encoding="utf-8")
        collector.coverage["scan_sha256"] = hashlib.sha256((payload + "\n").encode()).hexdigest()
        destination.with_suffix(".coverage.json").write_text(json.dumps(collector.coverage, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # stdout is still the primary output; a write failure is reported and
        # never silently swallowed, but it does not destroy the scan itself.
        write_failed = True
        print(f"warning: could not write denominator to {destination}: {exc}", file=sys.stderr)
    else:
        print(f"denominator written to {destination}", file=sys.stderr)
    print(payload)
    if write_failed or not collector.coverage["reconciled"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
