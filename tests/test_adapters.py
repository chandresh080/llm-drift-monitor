"""Tests for the OpenAI-compatible adapter retry behavior (mocked transport)."""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from monitor import OpenAICompatibleAdapter


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://test/v1/chat/completions", code, "Error", None, io.BytesIO(b'{"error": {}}')
    )


def _ok_response(content: str = "hello") -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    return ctx


class TestOpenAICompatibleAdapterRetry:
    def test_retries_on_503_then_succeeds(self):
        adapter = OpenAICompatibleAdapter(
            model_id="m", api_key="k", base_url="http://test/v1", max_retries=3
        )
        with patch("monitor.urlopen", side_effect=[_http_error(503), _ok_response("hello")]) as mock_urlopen, \
             patch("monitor.time.sleep") as mock_sleep:
            result = adapter.generate("hi")

        assert result == "hello"
        assert mock_urlopen.call_count == 2
        assert mock_sleep.called

    def test_does_not_retry_on_client_error(self):
        adapter = OpenAICompatibleAdapter(
            model_id="m", api_key="k", base_url="http://test/v1", max_retries=3
        )
        with patch("monitor.urlopen", side_effect=_http_error(400)) as mock_urlopen, \
             patch("monitor.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError):
                adapter.generate("hi")

        assert mock_urlopen.call_count == 1
        mock_sleep.assert_not_called

    def test_retries_on_timeout_then_raises(self):
        adapter = OpenAICompatibleAdapter(
            model_id="m", api_key="k", base_url="http://test/v1", max_retries=2
        )
        with patch("monitor.urlopen", side_effect=TimeoutError("timed out")) as mock_urlopen, \
             patch("monitor.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError):
                adapter.generate("hi")

        # max_retries=2 -> 3 attempts total
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_count == 2
