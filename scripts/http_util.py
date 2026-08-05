#!/usr/bin/env python3
"""Shared urllib-based JSON fetch with exponential-backoff retries.

Canonical copy: ``scripts/http_util.py`` in the sports-picks-skill repo.
A byte-identical copy ships next to deployed skill scripts (for same-directory
imports); keep every copy in sync with the canonical file.

No third-party dependencies: standard-library urllib only.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

__all__ = ["fetch_json", "DEFAULT_USER_AGENT"]

DEFAULT_USER_AGENT = "HermesSportsPicks/1.0"

# Browser-like UA for the ESPN mirror fallback: Akamai blocks the default
# script UA on site.api.espn.com but serves site.web.api.espn.com, which
# answers the same /apis/site/v2 paths with the same payload shape.
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
)

# ESPN's site API host (site.api.espn.com) returns 403 to data-center/script
# clients. The public web host (site.web.api.espn.com) serves the identical
# /apis/site/v2/sports/... routes and is the supported fallback used by
# ESPN's own frontends. Mirror only exact site-api URLs; core/web/FITT hosts
# are untouched.
_ESPN_PRIMARY_HOST = "site.api.espn.com"
_ESPN_FALLBACK_HOST = "site.web.api.espn.com"


def espn_mirror_url(url: str) -> str | None:
    """Return the site.web.api.espn.com mirror for a site.api.espn.com URL.

    Returns None for any non-ESPN or already-mirrored URL so callers can
    decide whether a fallback attempt exists.
    """
    prefix = f"https://{_ESPN_PRIMARY_HOST}/"
    if url.startswith(prefix):
        return f"https://{_ESPN_FALLBACK_HOST}/" + url[len(prefix):]
    return None


def _is_retryable_http_code(code: int) -> bool:
    return code == 429 or code >= 500


def fetch_json(
    url: str,
    *,
    timeout: float = 30,
    attempts: int = 3,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: Any = None,
    allow_espn_mirror: bool = True,
) -> Any:
    """Fetch ``url`` and parse the JSON response body.

    Retries on HTTP 429/5xx, ``urllib.error.URLError``, and timeouts with
    exponential backoff (1s, 2s, 4s, ...). Other HTTP errors raise
    immediately; the last retryable error is re-raised once ``attempts``
    are exhausted. An empty response body parses as ``{}``.

    ESPN mirror fallback: when ``url`` targets ``site.api.espn.com`` and the
    primary request fails with HTTP 403/404 (Akamai blocks data-center
    clients), the request is retried once against the equivalent
    ``site.web.api.espn.com`` URL with a browser-like User-Agent before the
    error propagates. The fallback is fail-closed: if the mirror also fails,
    the original exception is raised so callers keep their existing error
    handling. Pass ``allow_espn_mirror=False`` to disable.

    ``data`` may be a dict/list (JSON-encoded automatically, with a
    ``Content-Type: application/json`` header) or pre-encoded bytes.

    Safety note: callers doing non-idempotent POSTs (order placement)
    should pass ``attempts=1`` — a retried write can double-execute.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    mirror = espn_mirror_url(url) if allow_espn_mirror and method == "GET" else None
    try:
        return _fetch_json_with_retries(url, timeout=timeout, attempts=attempts, headers=headers, method=method, data=data)
    except urllib.error.HTTPError as error:
        if mirror is not None and error.code in (403, 404):
            mirror_headers = {"User-Agent": _FALLBACK_USER_AGENT}
            if headers:
                mirror_headers.update(headers)
            try:
                return _fetch_json_with_retries(mirror, timeout=timeout, attempts=attempts, headers=mirror_headers, method=method, data=data)
            except Exception:
                # Fail-closed: surface the original primary-host error.
                raise error
        raise


def _fetch_json_with_retries(
    url: str,
    *,
    timeout: float,
    attempts: int,
    headers: dict[str, str] | None,
    method: str,
    data: Any,
) -> Any:
    request_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        request_headers.update(headers)
    body = data
    if isinstance(body, (dict, list)):
        body = json.dumps(body, separators=(",", ":")).encode()
        request_headers.setdefault("Content-Type", "application/json")

    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as error:
            if not _is_retryable_http_code(error.code):
                raise
            last_error = error
        except (TimeoutError, socket.timeout, urllib.error.URLError) as error:
            last_error = error
        if attempt < attempts - 1:
            time.sleep(2**attempt)
    assert last_error is not None  # attempts >= 1 guarantees at least one loop
    raise last_error
