import logging

import pytest
import requests

from seekarr.arr import ArrClient, ArrRequestError
from seekarr.config import ArrConfig


class _FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, json_exc=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._json_exc = json_exc

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_data


def _client() -> ArrClient:
    return ArrClient(
        name="radarr",
        config=ArrConfig(enabled=True, url="http://example:7878", api_key="abc"),
        timeout_seconds=5,
        verify_ssl=True,
        logger=logging.getLogger("test"),
    )


def test_request_connection_error_raises_arr_request_error(monkeypatch) -> None:
    client = _client()

    def _fake_request(*args, **kwargs):  # noqa: ANN002, ANN003
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "request", _fake_request)

    with pytest.raises(ArrRequestError) as excinfo:
        client._request("GET", "/api/v3/ping")
    assert "Cannot connect" in excinfo.value.message
    assert "Check the instance URL/port" in excinfo.value.hint


def test_request_timeout_raises_arr_request_error(monkeypatch) -> None:
    client = _client()

    def _fake_request(*args, **kwargs):  # noqa: ANN002, ANN003
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(requests, "request", _fake_request)

    with pytest.raises(ArrRequestError) as excinfo:
        client._request("GET", "/api/v3/ping")
    assert "Request timed out after 5s" in excinfo.value.message


def test_request_http_error_includes_status_and_body_snippet(monkeypatch) -> None:
    client = _client()

    def _fake_request(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeResponse(status_code=401, text="unauthorized")

    monkeypatch.setattr(requests, "request", _fake_request)

    with pytest.raises(ArrRequestError) as excinfo:
        client._request("GET", "/api/v3/wanted/missing")
    assert "HTTP 401" in excinfo.value.message
    assert "unauthorized" in excinfo.value.message
    assert "API key permissions" in excinfo.value.hint


def test_request_invalid_json_raises_arr_request_error(monkeypatch) -> None:
    client = _client()

    def _fake_request(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeResponse(status_code=200, text="{not json}", json_exc=ValueError("bad json"))

    monkeypatch.setattr(requests, "request", _fake_request)

    with pytest.raises(ArrRequestError) as excinfo:
        client._request("GET", "/api/v3/ping")
    assert excinfo.value.message == "Invalid JSON response"


def test_request_empty_body_returns_empty_dict(monkeypatch) -> None:
    client = _client()

    def _fake_request(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeResponse(status_code=200, text="")

    monkeypatch.setattr(requests, "request", _fake_request)
    assert client._request("GET", "/api/v3/ping") == {}
