#!/usr/bin/env python3
"""Fetch World Cup match markets from Polymarket's Next.js data route.

Gamma sports discovery has been unreliable for WC match pages. This script
loads the public Polymarket page, discovers the current Next.js build ID, then
fetches the matching _next/data JSON route and extracts binary match markets.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from dataclasses import asdict, dataclass
from typing import Any, Iterable

PAGE_URL = "https://polymarket.com/sports/world-cup/games"
DATA_URL = "https://polymarket.com/_next/data/{build_id}/sports/world-cup/games.json"
EVENT_DATA_URL = "https://polymarket.com/_next/data/{build_id}/sports/world-cup/{slug}.json"
USER_AGENT = "Mozilla/5.0 (compatible; sports-picks-polymarket-wc/1.0)"


@dataclass
class OutcomeMarket:
    outcome: str
    team: str | None
    slug: str
    question: str
    yes_price: float | None
    no_price: float | None
    yes_clob_token_id: str | None = None
    no_clob_token_id: str | None = None
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


def fetch_text(url: str, accept: str = "*/*") -> tuple[str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            # Avoid SDK/brotli decoder issues in unattended jobs.
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(charset, "replace")
        return body, response.headers.get("content-type", "")


def discover_build_id(page_html: str) -> str:
    next_data = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page_html, re.S)
    if next_data:
        payload = json.loads(html.unescape(next_data.group(1)))
        build_id = payload.get("buildId")
        if build_id:
            return str(build_id)

    # Fallback: the build id also appears in static asset URLs and inline JSON.
    for pattern in (r'"buildId"\s*:\s*"([^"]+)"', r'/_next/static/([^/]+)/_buildManifest\.js'):
        match = re.search(pattern, page_html)
        if match:
            return match.group(1)
    raise ValueError("could not discover Polymarket Next.js build id")


def fetch_data_route(build_id: str) -> dict[str, Any]:
    body, content_type = fetch_text(DATA_URL.format(build_id=build_id), accept="application/json")
    stripped = body.lstrip()
    if "text/html" in content_type or stripped.startswith("<!DOCTYPE") or stripped.startswith("<html"):
        raise ValueError("Polymarket data route returned HTML; build id is stale")
    return json.loads(body)


def decode_serialized_payload(payload: str) -> dict[str, Any]:
    padding = "=" * (-len(payload) % 4)
    raw = base64.urlsafe_b64decode(payload + padding)
    return json.loads(zlib.decompress(raw).decode("utf-8"))


def extract_serialized_payload(page_html: str) -> dict[str, Any]:
    # App-router/Turbopack pages no longer expose __NEXT_DATA__ or a build ID.
    # Polymarket now ships the hydrated World Cup data as a compressed React
    # Flight payload referenced by serializedPayload.
    if "serializedPayload" not in page_html:
        raise ValueError("Polymarket page did not contain serializedPayload")

    for match in re.finditer(r'self\.__next_f\.push\(\[1,"([A-Za-z0-9_-]{100,})"\]\)', page_html):
        payload = match.group(1)
        if not payload.startswith("eJ"):
            continue
        try:
            data = decode_serialized_payload(payload)
        except (ValueError, zlib.error, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and any(
            obj.get("slug", "").startswith("fifwc-") for obj in walk_dicts(data)
        ):
            return data

    raise ValueError("could not decode Polymarket serializedPayload")


def load_world_cup_data() -> tuple[str | None, dict[str, Any]]:
    page_html, _ = fetch_text(PAGE_URL, accept="text/html")
    try:
        build_id = discover_build_id(page_html)
    except ValueError:
        return None, extract_serialized_payload(page_html)
    try:
        return build_id, fetch_data_route(build_id)
    except (ValueError, urllib.error.URLError, json.JSONDecodeError):
        # One retry with a fresh page fetch handles rolling build IDs.
        page_html, _ = fetch_text(PAGE_URL, accept="text/html")
        try:
            build_id = discover_build_id(page_html)
        except ValueError:
            return None, extract_serialized_payload(page_html)
        return build_id, fetch_data_route(build_id)


def fetch_event_data(build_id: str, slug: str) -> dict[str, Any]:
    body, content_type = fetch_text(EVENT_DATA_URL.format(build_id=build_id, slug=slug), accept="application/json")
    stripped = body.lstrip()
    if "text/html" in content_type or stripped.startswith("<!DOCTYPE") or stripped.startswith("<html"):
        raise ValueError(f"Polymarket event data route returned HTML for {slug}; build id is stale")
    return json.loads(body)


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def parse_price(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_clob_token_ids(value: Any) -> tuple[str | None, str | None]:
    """Return YES/NO CLOB token ids from Gamma/Next page data.

    Polymarket has exposed this as either a JSON string or a two-element list.
    The ordering follows the binary outcomes list: ["Yes", "No"].
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(value, list) or len(value) < 2:
        return None, None
    yes = str(value[0]) if value[0] not in (None, "") else None
    no = str(value[1]) if value[1] not in (None, "") else None
    return yes, no


def split_teams(title: str) -> list[str]:
    if " vs. " in title:
        return [part.strip() for part in title.split(" vs. ", 1)]
    if " vs " in title:
        return [part.strip() for part in title.split(" vs ", 1)]
    return []


def extract_event_shells(data: dict[str, Any], date: str | None = None) -> list[tuple[str, str, list[str], float | None, float | None]]:
    shells: dict[str, tuple[str, str, list[str], float | None, float | None]] = {}
    for obj in walk_dicts(data):
        slug = obj.get("slug")
        title = obj.get("title")
        if not isinstance(slug, str) or not slug.startswith("fifwc-"):
            continue
        if date and not slug.endswith(date):
            continue
        if not isinstance(title, str):
            continue
        teams = split_teams(title)
        if len(teams) != 2:
            continue
        shells[slug] = (slug, title, teams, parse_price(obj.get("volume") or obj.get("volumeNum")), parse_price(obj.get("liquidity") or obj.get("liquidityNum")))
    return sorted(shells.values(), key=lambda row: row[0])


def classify_outcome(question: str, teams: list[str]) -> tuple[str, str | None] | None:
    q = question.lower()
    if "end in a draw" in q:
        return "draw", None
    for idx, team in enumerate(teams):
        if team.lower() in q and " win " in q:
            return ("home" if idx == 0 else "away"), team
    return None


def extract_outcome_markets(event_data: dict[str, Any], teams: list[str], base_slug: str | None = None) -> dict[str, OutcomeMarket]:
    outcomes: dict[str, OutcomeMarket] = {}
    for obj in walk_dicts(event_data):
        slug = obj.get("slug")
        question = obj.get("question")
        prices = obj.get("outcomePrices")
        raw_outcomes = obj.get("outcomes")
        if not isinstance(slug, str) or not isinstance(question, str):
            continue
        if base_slug and not slug.startswith(f"{base_slug}-"):
            continue
        if not isinstance(prices, list) or len(prices) < 2:
            continue
        if raw_outcomes and raw_outcomes != ["Yes", "No"]:
            continue
        yes_clob_token_id, no_clob_token_id = parse_clob_token_ids(obj.get("clobTokenIds"))
        classified = classify_outcome(question, teams)
        if not classified:
            continue
        outcome, team = classified
        # Use the first copy if the dehydrated state contains duplicate cache entries.
        outcomes.setdefault(outcome, OutcomeMarket(
            outcome=outcome,
            team=team,
            slug=slug,
            question=question,
            yes_price=parse_price(prices[0]),
            no_price=parse_price(prices[1]),
            yes_clob_token_id=yes_clob_token_id,
            no_clob_token_id=no_clob_token_id,
            volume=parse_price(obj.get("volume") or obj.get("volumeNum")),
            liquidity=parse_price(obj.get("liquidity") or obj.get("liquidityNum")),
        ))
    return outcomes


def extract_markets(data: dict[str, Any], build_id: str | None, date: str | None = None) -> list[MatchMarket]:
    markets: list[MatchMarket] = []
    for slug, title, teams, volume, liquidity in extract_event_shells(data, date=date):
        event_data = fetch_event_data(build_id, slug) if build_id else data
        outcomes = extract_outcome_markets(event_data, teams, base_slug=slug)
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
            volume=volume,
            liquidity=liquidity,
        ))
    return sorted(markets, key=lambda market: market.slug)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch Polymarket World Cup binary match markets")
    parser.add_argument("--date", help="Filter by YYYY-MM-DD date embedded in the market slug")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)

    build_id, data = load_world_cup_data()
    markets = extract_markets(data, build_id=build_id, date=args.date)
    print(json.dumps([asdict(market) for market in markets], indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
