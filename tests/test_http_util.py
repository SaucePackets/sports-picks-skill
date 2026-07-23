import io
import json
import unittest
import urllib.error
from unittest import mock

from scripts import http_util


def _response(payload):
    body = io.BytesIO(json.dumps(payload).encode())
    body.read1 = body.read  # not used, parity with http.client
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.read.return_value = json.dumps(payload).encode()
    return response


def _http_error(code):
    return urllib.error.HTTPError("https://example.test", code, "err", hdrs=None, fp=io.BytesIO(b"{}"))


class FetchJsonTests(unittest.TestCase):
    def test_returns_parsed_json_on_first_success(self):
        with mock.patch.object(http_util.urllib.request, "urlopen", return_value=_response({"ok": True})) as urlopen:
            result = http_util.fetch_json("https://example.test/x")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 1)

    def test_retries_on_5xx_with_exponential_backoff(self):
        calls = [_http_error(503), _http_error(502), _response({"ok": 1})]
        sleeps = []
        with mock.patch.object(http_util.urllib.request, "urlopen", side_effect=calls), \
                mock.patch.object(http_util.time, "sleep", side_effect=sleeps.append):
            result = http_util.fetch_json("https://example.test/x")

        self.assertEqual(result, {"ok": 1})
        self.assertEqual(sleeps, [1, 2])

    def test_retries_on_429_and_url_error_then_reraises(self):
        calls = [_http_error(429), urllib.error.URLError("boom"), _http_error(500)]
        with mock.patch.object(http_util.urllib.request, "urlopen", side_effect=calls), \
                mock.patch.object(http_util.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                http_util.fetch_json("https://example.test/x", attempts=3)

        self.assertEqual(ctx.exception.code, 500)

    def test_non_retryable_http_error_raises_immediately(self):
        with mock.patch.object(http_util.urllib.request, "urlopen", side_effect=_http_error(404)) as urlopen, \
                mock.patch.object(http_util.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                http_util.fetch_json("https://example.test/x")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_dict_data_is_json_encoded_with_content_type(self):
        with mock.patch.object(http_util.urllib.request, "urlopen", return_value=_response({})) as urlopen:
            http_util.fetch_json("https://example.test/x", method="POST", data={"a": 1})

        request = urlopen.call_args[0][0]
        self.assertEqual(request.data, b'{"a":1}')
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_empty_body_parses_as_empty_object(self):
        response = _response({})
        response.read.return_value = b""
        with mock.patch.object(http_util.urllib.request, "urlopen", return_value=response):
            self.assertEqual(http_util.fetch_json("https://example.test/x"), {})

    def test_single_attempt_never_sleeps(self):
        with mock.patch.object(http_util.urllib.request, "urlopen", side_effect=_http_error(503)), \
                mock.patch.object(http_util.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                http_util.fetch_json("https://example.test/x", attempts=1)

        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
