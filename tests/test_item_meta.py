from seekarr import item_meta


class _FakeImageResponse:
    headers = {"Content-Type": "image/jpeg"}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield b"fake image bytes"


def test_cache_cover_image_reuses_same_file_for_same_url(tmp_path, monkeypatch) -> None:
    calls = {"count": 0}

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        return _FakeImageResponse()

    monkeypatch.setattr(item_meta.requests, "get", fake_get)

    first = item_meta.cache_cover_image(
        str(tmp_path / "seekarr.db"),
        "HTTPS://IMG.EXAMPLE/poster.jpg#fragment",
        app_type="sonarr",
        instance_id=1,
        item_key="episode:1",
        timeout_seconds=5,
        verify_ssl=True,
    )
    second = item_meta.cache_cover_image(
        str(tmp_path / "seekarr.db"),
        "https://img.example/poster.jpg",
        app_type="sonarr",
        instance_id=1,
        item_key="episode:2",
        timeout_seconds=5,
        verify_ssl=True,
    )

    assert first == second
    assert calls["count"] == 1
    assert len(list((tmp_path / "media_cache").glob("*.jpg"))) == 1
