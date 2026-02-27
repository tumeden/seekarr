import logging

from seekarr.arr import ArrClient
from seekarr.config import ArrConfig


def test_fetch_series_season_inventory_tracks_unaired(monkeypatch) -> None:
    client = ArrClient(
        name="sonarr",
        config=ArrConfig(enabled=True, url="http://example", api_key="abc"),
        timeout_seconds=5,
        verify_ssl=True,
        logger=logging.getLogger("test"),
    )

    payload = [
        {"seasonNumber": 1, "airDateUtc": "2025-01-01T00:00:00Z", "hasFile": False},
        {"seasonNumber": 1, "airDateUtc": "2025-01-02T00:00:00Z", "hasFile": True},
        {"seasonNumber": 1, "airDateUtc": "2099-01-01T00:00:00Z", "hasFile": False},
    ]

    def _fake_request(method, path, params=None, json_data=None):  # noqa: ANN001
        assert method == "GET"
        assert path == "/api/v3/episode"
        return payload

    monkeypatch.setattr(client, "_request", _fake_request)

    inv = client.fetch_series_season_inventory(123)
    assert inv[1]["aired_total"] == 2
    assert inv[1]["aired_downloaded"] == 1
    assert inv[1]["unaired_total"] == 1


def test_fetch_queue_episode_ids(monkeypatch) -> None:
    client = ArrClient(
        name="sonarr",
        config=ArrConfig(enabled=True, url="http://example", api_key="abc"),
        timeout_seconds=5,
        verify_ssl=True,
        logger=logging.getLogger("test"),
    )

    rows = [
        {"episodeId": 10},
        {"episode": {"id": 11}},
        {"episodeId": 0},
        {},
    ]

    def _fake_fetch(path):  # noqa: ANN001
        assert path == "/api/v3/queue"
        return rows

    monkeypatch.setattr(client, "_fetch_paged_records", _fake_fetch)

    assert client.fetch_queue_episode_ids() == {10, 11}
