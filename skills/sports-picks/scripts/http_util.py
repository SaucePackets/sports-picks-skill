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
) -> Any:
    """Fetch ``url`` and parse the JSON response body.

    Retries on HTTP 429/5xx, ``urllib.error.URLError``, and timeouts with
    exponential backoff (1s, 2s, 4s, ...). Other HTTP errors raise
    immediately; the last retryable error is re-raised once ``attempts``
    are exhausted. An empty response body parses as ``{}``.

    ``data`` may be a dict/list (JSON-encoded automatically, with a
    ``Content-Type: application/json`` header) or pre-encoded bytes.

    Safety note: callers doing non-idempotent POSTs (order placement)
    should pass ``attempts=1`` — a retried write can double-execute.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
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
