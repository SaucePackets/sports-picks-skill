#!/usr/bin/env python3
"""Fetch World Cup match markets from Polymarket's public Gamma API.

Replaces the old Next.js build-ID scraping: the Gamma market-data API serves
stable JSON with ``markets[].outcomePrices`` directly.

  https://gamma-api.polymarket.com/events?slug=<slug>
  https://gamma-api.polymarket.com/events?tag_id=<id>&closed=false

Each World Cup match is a ``fifwc-{away}-{home}-YYYY-MM-DD`` event holding
Yes/No sub-markets for home win, draw, and away win. ``outcomes`` and
``outcomePrices`` arrive as JSON-encoded strings and are coerced here.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from http_util import fetch_json  # noqa: E402

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
# Gamma tag "fifa-world-cup" (id 102232) carries the 2026 World Cup events;
# tag "world-cup" (id 519) is the older generic tag.
DEFAULT_WORLD_CUP_TAG_ID = 102232
EVENT_SLUG_PREFIX = "fifwc-"
PAGE_LIMIT = 100
USER_AGENT = "sports-picks-polymarket-wc/2.0"


@dataclass
class OutcomeMarket:
    outcome: str
    team: str | None
    slug: str
    yes_price: float | None
    no_price: float | None
    volume: float | None = None
    liquidity: float | None = None


@dataclass
class MatchMarket:
    slug: str
    title: str
    teams: list[str]
    home_team: str | None
    away_team: str | None
    home_price: float | None
    draw_price: float | None
    away_price: float | None
    outcomes: dict[str, OutcomeMarket]
    volume: float | None = None
    liquidity: float | None = None


def gamma_get(params: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{GAMMA_EVENTS_URL}?{urllib.parse.urlencode(params)}"
    payload = fetch_json(url, timeout=30, attempts=3, headers={"User-Agent": USER_AGENT})
    if isinstance(payload, list):
        return [event for event in payload if isinstance(event, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [event for event in data if isinstance(event, dict)]
    raise ValueError("Gamma events endpoint returned an unexpected payload shape")


def fetch_events_by_slug(slug: str) -> list[dict[str, Any]]:
    return gamma_get({"slug": slug})


def fetch_events_by_tag(tag_id: int, include_closed: bool = False) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {"tag_id": tag_id, "limit": PAGE_LIMIT, "offset": offset}
        if not include_closed:
            params["closed"] = "false"
        page = gamma_get(params)
        events.extend(page)
        if len(page) < PAGE_LIMIT:
            return events
        offset += PAGE_LIMIT


def parse_json_list(value: Any) -> list[Any]:
    """Gamma encodes list fields (outcomes, outcomePrices) as JSON strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def parse_price(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def split_teams(title: str) -> list[str]:
    if " vs. " in title:
        return [part.strip() for part in title.split(" vs. ", 1)]
    if " vs " in title:
        return [part.strip() for part in title.split(" vs ", 1)]
    return []


def extract_event_shells(
    events: list[dict[str, Any]], date: str | None = None
) -> list[tuple[dict[str, Any], str, str, list[str]]]:
    """Keep fifwc match events (optionally date-filtered) with parseable teams."""
    shells: dict[str, tuple[dict[str, Any], str, str, list[str]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        slug = event.get("slug")
        title = event.get("title")
        if not isinstance(slug, str) or not slug.startswith(EVENT_SLUG_PREFIX):
            continue
        if date and not slug.endswith(date):
            continue
        if not isinstance(title, str):
            continue
        teams = split_teams(title)
        if len(teams) != 2:
            continue
        shells[slug] = (event, slug, title, teams)
    return sorted(shells.values(), key=lambda row: row[1])


def classify_outcome(question: str, teams: list[str]) -> tuple[str, str | None] | None:
    q = question.lower()
    if "end in a draw" in q:
        return "draw", None
    for idx, team in enumerate(teams):
        if team.lower() in q and " win " in q:
            return ("home" if idx == 0 else "away"), team
    return None


def extract_outcome_markets(
    event: dict[str, Any], teams: list[str], base_slug: str | None = None
) -> dict[str, OutcomeMarket]:
    outcomes: dict[str, OutcomeMarket] = {}
    for market in event.get("markets", []) or []:
        if not isinstance(market, dict):
            continue
        slug = market.get("slug")
        question = market.get("question")
        if not isinstance(slug, str) or not isinstance(question, str):
            continue
        if base_slug and not slug.startswith(f"{base_slug}-"):
            continue
        prices = parse_json_list(market.get("outcomePrices"))
        if len(prices) < 2:
            continue
        raw_outcomes = parse_json_list(market.get("outcomes"))
        if raw_outcomes and raw_outcomes != ["Yes", "No"]:
            continue
        classified = classify_outcome(question, teams)
        if not classified:
            continue
        outcome, team = classified
        # Use the first copy if the payload contains duplicate entries.
        outcomes.setdefault(outcome, OutcomeMarket(
            outcome=outcome,
            team=team,
            slug=slug,
            yes_price=parse_price(prices[0]),
            no_price=parse_price(prices[1]),
            volume=parse_price(market.get("volume") or market.get("volumeNum")),
            liquidity=parse_price(market.get("liquidity") or market.get("liquidityNum")),
        ))
    return outcomes


def extract_markets(events: list[dict[str, Any]], date: str | None = None) -> list[MatchMarket]:
    markets: list[MatchMarket] = []
    for event, slug, title, teams in extract_event_shells(events, date=date):
        outcomes = extract_outcome_markets(event, teams, base_slug=slug)
        if not outcomes:
            continue
        markets.append(MatchMarket(
            slug=slug,
            title=title,
            teams=teams,
            home_team=teams[0],
            away_team=teams[1],
            home_price=outcomes.get("home").yes_price if outcomes.get("home") else None,
            draw_price=outcomes.get("draw").yes_price if outcomes.get("draw") else None,
            away_price=outcomes.get("away").yes_price if outcomes.get("away") else None,
            outcomes=outcomes,
            volume=parse_price(event.get("volume") or event.get("volumeNum")),
            liquidity=parse_price(event.get("liquidity") or event.get("liquidityNum")),
        ))
    return sorted(markets, key=lambda market: market.slug)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Polymarket World Cup match markets via the Gamma API")
    parser.add_argument("--date", help="Filter by YYYY-MM-DD date embedded in the market slug")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--slug", action="append", help="Fetch specific event slug(s) instead of the tag listing")
    parser.add_argument("--tag-id", type=int, default=DEFAULT_WORLD_CUP_TAG_ID,
                        help=f"Gamma tag id for the listing (default {DEFAULT_WORLD_CUP_TAG_ID}, fifa-world-cup)")
    parser.add_argument("--include-closed", action="store_true",
                        help="Include closed events (needed for past match dates)")
    args = parser.parse_args(argv)

    if args.slug:
        events: list[dict[str, Any]] = []
        for slug in args.slug:
            events.extend(fetch_events_by_slug(slug))
    else:
        events = fetch_events_by_tag(args.tag_id, include_closed=args.include_closed)

    markets = extract_markets(events, date=args.date)
    print(json.dumps([asdict(market) for market in markets], indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
