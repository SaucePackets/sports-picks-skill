#!/usr/bin/env python3
"""Shim for sports-skills ESPN host blocked by Akamai.

site.api.espn.com returns 403 to data-center clients. The equivalent public
host site.web.api.espn.com serves the same /apis/site/v2/... routes and is
the durable fallback used by ESPN's own frontends.

The sports-skills package binds ``espn_request`` and ``_http_fetch`` into
each sport connector's module globals at import time (``from ... import``),
so patching only ``sports_skills._espn_base`` leaves the connectors calling
the original functions. This shim therefore wraps ``_http_fetch`` at every
binding actually used by the package: the ``_espn_base`` module itself plus
each sport connector module that imported it directly. With the transport
wrapped, every sport's ``espn_request`` (including the module-local copies
in nfl/mlb/nba/nhl/cfb/cbb/wnba/cricket/golf/tennis connectors) gains the
403/404 mirror fallback, and the connectors' direct ``_http_fetch`` calls
against site.api.espn.com are covered too. football._connector defines its
own private ``_http_fetch`` and ``_espn_request``; those are wrapped as
well for its site.api.espn.com soccer calls.

Import this module before calling sports_skills functions.

Usage:
    import scripts.sports_skills_espn_shim  # noqa: F401
    from sports_skills import nfl
    injuries = nfl.get_injuries()

CLI equivalent:
    python -c "import scripts.sports_skills_espn_shim; from sports_skills import nfl; print(nfl.get_injuries())"
"""
from __future__ import annotations

import functools
import importlib
import logging

logger = logging.getLogger("sports_skills_espn_shim")

_PRIMARY_HOST = "site.api.espn.com"
_FALLBACK_HOST = "site.web.api.espn.com"
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
)
_MIRROR_STATUS_CODES = (403, 404)

# Modules inside sports_skills that bind _http_fetch from _espn_base at
# import time (from ... import _http_fetch). Patching _espn_base alone does
# not reach these module-global bindings.
_FETCH_BINDING_MODULES = (
    "sports_skills.cricket._espn",
    "sports_skills.mlb._connector",
    "sports_skills.golf._connector",
    "sports_skills.nhl._connector",
    "sports_skills.nfl._connector",
    "sports_skills.nba._connector",
    "sports_skills.nba._cdn",
    "sports_skills.tennis._connector",
    "sports_skills.wnba._connector",
)

# football._connector defines its own private _http_fetch (not imported
# from _espn_base); wrap it in place too.
_PRIVATE_FETCH_MODULES = ("sports_skills.football._connector",)


def _mirror_url(url: str) -> str | None:
    prefix = f"https://{_PRIMARY_HOST}/"
    if isinstance(url, str) and url.startswith(prefix):
        return f"https://{_FALLBACK_HOST}/" + url[len(prefix):]
    return None


def _wrap_http_fetch(original):
    """Wrap an _http_fetch-compatible function with the ESPN mirror fallback."""

    @functools.wraps(original)
    def fetch_with_mirror(url, headers=None, **kwargs):
        raw, err = original(url, headers=headers, **kwargs)
        if not err or err.get("status_code") not in _MIRROR_STATUS_CODES:
            return raw, err
        mirror = _mirror_url(url)
        if mirror is None:
            return raw, err
        mirror_headers = dict(headers or {})
        mirror_headers["User-Agent"] = _FALLBACK_USER_AGENT
        mirror_raw, mirror_err = original(mirror, headers=mirror_headers, **kwargs)
        if mirror_err is None:
            return mirror_raw, None
        return raw, err

    return fetch_with_mirror


def apply_shim() -> None:
    """Patch sports_skills HTTP fetch bindings to use the web mirror."""
    try:
        from sports_skills import _espn_base
    except ImportError:
        logger.warning("sports_skills not installed; shim not applied")
        return

    wrapped = _wrap_http_fetch(_espn_base._http_fetch)
    _espn_base._http_fetch = wrapped

    # Rebind every module that imported _http_fetch directly so their local
    # bindings (used by their own espn_request copies and direct fetches)
    # also go through the mirror. Modules not yet imported are patched when
    # imported later via the import hook below; patching here covers the
    # common case where the shim is imported before the sport connectors.
    for module_name in _FETCH_BINDING_MODULES:
        module = _import_if_loaded(module_name)
        if module is not None and getattr(module, "_http_fetch", None) is not wrapped:
            setattr(module, "_http_fetch", wrapped)

    for module_name in _PRIVATE_FETCH_MODULES:
        module = _import_if_loaded(module_name)
        if module is not None and not getattr(
            getattr(module, "_http_fetch", None), "_espn_mirror_wrapped", False
        ):
            private = getattr(module, "_http_fetch", None)
            if private is not None:
                setattr(module, "_http_fetch", _wrap_private_fetch(private))


def _import_if_loaded(module_name):
    """Return the imported module or None without triggering an import."""
    import sys

    module = sys.modules.get(module_name)
    if module is None:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - optional deps
            logger.debug("could not patch %s: %s", module_name, exc)
            return None
    return module


def _wrap_private_fetch(original):
    """Wrap football._connector._http_fetch, which returns (raw, err) too."""

    @functools.wraps(original)
    def fetch_with_mirror(url, headers=None, **kwargs):
        raw, err = original(url, headers=headers, **kwargs)
        if not err or err.get("status_code") not in _MIRROR_STATUS_CODES:
            return raw, err
        mirror = _mirror_url(url)
        if mirror is None:
            return raw, err
        mirror_headers = dict(headers or {})
        mirror_headers["User-Agent"] = _FALLBACK_USER_AGENT
        mirror_raw, mirror_err = original(mirror, headers=mirror_headers, **kwargs)
        if mirror_err is None:
            return mirror_raw, None
        return raw, err

    fetch_with_mirror._espn_mirror_wrapped = True  # type: ignore[attr-defined]
    return fetch_with_mirror


apply_shim()
