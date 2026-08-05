"""Regression tests for the sports_skills ESPN mirror shim.

The reviewer reproduction: after importing scripts.sports_skills_espn_shim,
the public sports_skills.nfl API must route through the site.web.api.espn.com
mirror when site.api.espn.com answers 403. These tests exercise the public
nfl.get_injuries() / nfl.get_depth_chart() entry points with urlopen mocked,
forcing a primary 403 and asserting the mirror host is called and its payload
is returned.
"""
import io
import json
import urllib.error
from unittest import mock

import pytest

sports_skills = pytest.importorskip("sports_skills")

from scripts import sports_skills_espn_shim  # noqa: F401,E402
from sports_skills import nfl  # noqa: E402

PRIMARY_PREFIX = "https://site.api.espn.com/"
MIRROR_PREFIX = "https://site.web.api.espn.com/"


def _http_error(code):
    return urllib.error.HTTPError(
        "https://x", code, "err", hdrs=None, fp=io.BytesIO(b"{}")  # type: ignore[arg-type]
    )


def _response(payload):
    resp = mock.MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.read.return_value = json.dumps(payload).encode()
    resp.headers = {}
    return resp


def _fake_urlopen(calls):
    def fake(request, timeout=0):
        calls.append(request.full_url)
        if request.full_url.startswith(PRIMARY_PREFIX):
            raise _http_error(403)
        return _response({"mirror": True, "items": [], "injuries": []})

    return fake


@pytest.fixture(autouse=True)
def _clear_espn_cache():
    from sports_skills import _espn_base

    _espn_base._cache.clear()  # type: ignore[attr-defined]
    yield
    _espn_base._cache.clear()  # type: ignore[attr-defined]


def test_shim_rebinds_nfl_connector_fetch():
    from sports_skills import _espn_base
    from sports_skills.nfl import _connector

    # nfl._connector binds _http_fetch at import time; the shim must have
    # rebound it to the wrapped mirror-aware fetch.
    assert _connector._http_fetch is _espn_base._http_fetch  # type: ignore[attr-defined]
    assert getattr(_espn_base._http_fetch, "__wrapped__", None) is not None  # type: ignore[attr-defined]


def test_nfl_get_injuries_uses_mirror_on_primary_403():
    calls = []
    with mock.patch(
        "urllib.request.urlopen", side_effect=_fake_urlopen(calls)
    ):
        result = nfl.get_injuries()

    assert not (isinstance(result, dict) and result.get("error")), result
    assert any(u.startswith(PRIMARY_PREFIX) for u in calls), calls
    assert any(u.startswith(MIRROR_PREFIX) for u in calls), calls


def test_nfl_get_depth_chart_uses_mirror_on_primary_403():
    calls = []
    with mock.patch(
        "urllib.request.urlopen", side_effect=_fake_urlopen(calls)
    ):
        result = nfl.get_depth_chart(team_id="12")

    assert not (isinstance(result, dict) and result.get("error")), result
    assert any(u.startswith(PRIMARY_PREFIX) for u in calls), calls
    assert any(u.startswith(MIRROR_PREFIX) for u in calls), calls
