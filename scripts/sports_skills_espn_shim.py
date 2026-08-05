#!/usr/bin/env python3
"""Shim for sports-skills ESPN host blocked by Akamai.

site.api.espn.com returns 403 to data-center clients. The equivalent public
host site.web.api.espn.com serves the same /apis/site/v2/... routes and is
the durable fallback used by ESPN's own frontends.

This shim monkey-patches ``sports_skills._espn_base.espn_request`` so NFL
(and other US sport) helpers use the working host without modifying the
installed package. Import this module before calling sports_skills.nfl
functions.

Usage:
    import scripts.sports_skills_espn_shim  # noqa: F401
    from sports_skills import nfl
    injuries = nfl.get_injuries({})

CLI equivalent:
    python -c "import scripts.sports_skills_espn_shim; from sports_skills import nfl; print(nfl.get_injuries({}))"
"""
from __future__ import annotations

import json
import logging
import urllib.parse

logger = logging.getLogger("sports_skills_espn_shim")

_PRIMARY_HOST = "site.api.espn.com"
_FALLBACK_HOST = "site.web.api.espn.com"
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
)


def _mirror_url(url: str) -> str | None:
    prefix = f"https://{_PRIMARY_HOST}/"
    if url.startswith(prefix):
        return f"https://{_FALLBACK_HOST}/" + url[len(prefix):]
    return None


def apply_shim() -> None:
    """Patch sports_skills._espn_base.espn_request to use the web mirror."""
    try:
        from sports_skills import _espn_base
    except ImportError:
        logger.warning("sports_skills not installed; shim not applied")
        return

    original_espn_request = _espn_base.espn_request

    def espn_request_via_mirror(
        sport_path, resource="scoreboard", params=None, max_retries=_espn_base._MAX_RETRIES
    ):
        cache_key = f"espn:{sport_path}:{resource}:{json.dumps(params or {}, sort_keys=True)}"
        cached = _espn_base._cache_get(cache_key)
        if cached is not None:
            return cached
        url = f"https://{_PRIMARY_HOST}/apis/site/v2/sports/{sport_path}/{resource}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": _FALLBACK_USER_AGENT}
        raw, err = _espn_base._http_fetch(
            url,
            headers=headers,
            rate_limiter=_espn_base._espn_rate_limiter,
            max_retries=max_retries,
        )
        if err and err.get("status_code") in (403, 404):
            mirror = _mirror_url(url)
            if mirror:
                raw, err = _espn_base._http_fetch(
                    mirror,
                    headers=headers,
                    rate_limiter=_espn_base._espn_rate_limiter,
                    max_retries=max_retries,
                )
        if err:
            return err
        try:
            data = json.loads(raw.decode())
            _espn_base._cache_set(cache_key, data, ttl=120)
            return data
        except (json.JSONDecodeError, ValueError):
            return {"error": True, "message": "ESPN returned invalid JSON"}

    _espn_base.espn_request = espn_request_via_mirror


apply_shim()
