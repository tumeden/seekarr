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


def test_fetch_wanted_episodes_skips_cutoff_items_already_met(monkeypatch) -> None:
    client = ArrClient(
        name="sonarr",
        config=ArrConfig(enabled=True, url="http://example", api_key="abc"),
        timeout_seconds=5,
        verify_ssl=True,
        logger=logging.getLogger("test"),
    )

    missing_rows: list[dict] = []
    cutoff_rows = [
        {
            "id": 101,
            "seriesId": 1,
            "series": {"id": 1, "title": "Show", "tvdbId": 11, "monitored": True},
            "seasonNumber": 1,
            "episodeNumber": 1,
            "airDateUtc": "2025-01-01T00:00:00Z",
            "qualityCutoffNotMet": False,
            "customFormatCutoffNotMet": False,
            "hasFile": True,
        },
        {
            "id": 102,
            "seriesId": 1,
            "series": {"id": 1, "title": "Show", "tvdbId": 11, "monitored": True},
            "seasonNumber": 1,
            "episodeNumber": 2,
            "airDateUtc": "2025-01-01T00:00:00Z",
            "qualityCutoffNotMet": True,
            "hasFile": True,
        },
    ]

    def _fake_fetch(path):  # noqa: ANN001
        if path == "/api/v3/wanted/missing":
            return missing_rows
        if path == "/api/v3/wanted/cutoff":
            return cutoff_rows
        return []

    monkeypatch.setattr(client, "_fetch_paged_records", _fake_fetch)
    monkeypatch.setattr(client, "_fetch_series_lookup", lambda: {1: ("Show", 11, True)})

    out = client.fetch_wanted_episodes(search_missing=False, search_cutoff_unmet=True)
    assert [x.episode_id for x in out] == [102]


def test_fetch_wanted_movies_skips_cutoff_items_already_met(monkeypatch) -> None:
    client = ArrClient(
        name="radarr",
        config=ArrConfig(enabled=True, url="http://example", api_key="abc"),
        timeout_seconds=5,
        verify_ssl=True,
        logger=logging.getLogger("test"),
    )

    missing_rows: list[dict] = []
    cutoff_rows = [
        {
            "id": 201,
            "title": "Movie A",
            "year": 2024,
            "tmdbId": 1,
            "imdbId": "tt1",
            "qualityCutoffNotMet": False,
            "customFormatCutoffNotMet": False,
            "hasFile": True,
        },
        {
            "id": 202,
            "title": "Movie B",
            "year": 2024,
            "tmdbId": 2,
            "imdbId": "tt2",
            "qualityCutoffNotMet": True,
            "hasFile": True,
        },
    ]

    def _fake_fetch(path):  # noqa: ANN001
        if path == "/api/v3/wanted/missing":
            return missing_rows
        if path == "/api/v3/wanted/cutoff":
            return cutoff_rows
        return []

    monkeypatch.setattr(client, "_fetch_paged_records", _fake_fetch)
    monkeypatch.setattr(
        client,
        "_fetch_movie_meta_lookup",
        lambda: {
            201: {"monitored": True, "release_date_utc": None},
            202: {"monitored": True, "release_date_utc": None},
        },
    )

    out = client.fetch_wanted_movies(search_missing=False, search_cutoff_unmet=True)
    assert [x.movie_id for x in out] == [202]
